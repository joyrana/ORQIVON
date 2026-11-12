"""Delivery on a Go repository, end to end, with a model over real HTTP.

The architecture proposal, the QA acceptance suite (Go tests named after the criteria) and the two
coding attempts come from a scripted OpenAI-compatible server. Everything else is real: the Go
toolchain in the sandbox, `go test -json` evidence, the independent suite catching the first
attempt's off-by-one, the repair, regression and the release readiness decision.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import agentic_sdlc.execution.coding_agents
import agentic_sdlc.workflows.stages_build  # noqa: F401 - imported before patching
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.models.endpoints import parse_endpoints
from agentic_sdlc.models.registry import ModelRegistry, default_providers
from agentic_sdlc.models.types import ModelEntry
from agentic_sdlc.workflows.worker import drain
from tests.conftest import H
from tests.integration.test_documents_requirements import upload_sample_brd
from tests.model_server import FakeOpenAI, serve, system_prompt, user_prompt
from tests.workflow.helpers import decide, pending, run

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0
    or not shutil.which("unshare")
    or not shutil.which("go")
    or not Path(get_settings().sandbox_python).exists(),
    reason="needs the namespace sandbox and a Go toolchain",
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "go_task_service"
MODEL = "acme/coder-32b"


def _design(user: str) -> dict[str, Any]:
    keys = sorted(set(re.findall(r'"key": "((?:FR|NFR|BR|REQ)-\d+)"', user)))
    files = ["tasks/errors.go", "tasks/service.go"]
    return {
        "title": "Validate titles in Service.CreateTask",
        "context": "Go module example.com/taskservice; tests use the standard testing package.",
        "options": [
            {
                "name": "Validate at the service entry point",
                "summary": "Trim and validate in Service.CreateTask; ValidationError carries the field.",
                "pros": ["Smallest change"],
                "cons": ["Tasks built directly bypass validation"],
                "risks": ["None significant"],
                "files": files,
            },
            {
                "name": "Validate in a constructor for Task",
                "summary": "NewTask validates; CreateTask uses it.",
                "pros": ["Every construction path checked"],
                "cons": ["Larger change"],
                "risks": ["Callers constructing Task literals"],
                "files": files,
            },
        ],
        "recommended_option": "Validate at the service entry point",
        "decision_rationale": "Matches how the service already guards its operations.",
        "components": [{"name": "Service.CreateTask", "file": "tasks/service.go", "change": "validate"}],
        "data_model_changes": [],
        "api_contracts": [],
        "failure_modes": ["invalid title returns *ValidationError"],
        "security": ["no new inputs beyond the title"],
        "testing_strategy": ["go test with table cases for the boundaries"],
        "deployment": ["no migration"],
        "open_risks": [],
        "diagram_mermaid": "flowchart LR\n  caller --> create[Service.CreateTask]",
        "traceability": [
            {"requirement_key": k, "component": "Service.CreateTask", "approach": "validate the title"}
            for k in keys
        ],
    }


@pytest.fixture
def go_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOpenAI]:
    read = lambda name: (FIXTURES / name).read_text()  # noqa: E731
    attempts = [
        {
            "tasks/errors.go": read("errors.go"),
            "tasks/service.go": read("service_attempt1.go"),
            "tasks/title_validation_test.go": read("title_validation_test.go"),
        },
        {"tasks/service.go": read("service_attempt2.go")},
    ]
    calls = {"code": 0}

    def handler(body: dict[str, Any]) -> dict[str, Any]:
        system, user = system_prompt(body), user_prompt(body)
        if "software architect" in system:
            return _design(user)
        if "independent QA engineer" in system:
            profile = json.loads(user.split("<test_profile>\n", 1)[1].split("\n</test_profile>", 1)[0])
            assert profile["name"] == "go" and profile["language"] == "go"
            return {
                "files": [
                    {
                        "path": "tasks/asw_acceptance_title_test.go",
                        "content": read("asw_acceptance_title_test.go"),
                    }
                ],
                "coverage": [],
                "notes": ["NFR-001 has no automatable threshold"],
            }
        if "careful software engineer" in system:
            tree = set(user.split("<tree>\n", 1)[1].split("\n</tree>", 1)[0].splitlines())
            content = attempts[min(calls["code"], 1)]
            calls["code"] += 1
            return {
                "summary": f"attempt {calls['code']}",
                "edits": [
                    {"path": p, "action": "modify" if p in tree else "create", "content": c}
                    for p, c in content.items()
                ],
                "notes": [],
            }
        raise AssertionError(f"unexpected prompt {system[:60]}")

    fake = FakeOpenAI(models=[MODEL], handler=handler)
    with serve(fake):
        eps = parse_endpoints([{"name": "lab", "base_url": fake.url}], None)
        entry = ModelEntry(
            model_id="acme-coder-32b",
            provider="openai_compatible",
            endpoint="lab",
            served_model_name=MODEL,
            revision="r1",
            family="acme",
            license="Apache-2.0",
            tokenizer="t",
            context_window=65536,
            max_output_tokens=16384,
            tasks=["architecture", "code_generation", "test_generation"],
            capabilities=["code", "structured_output", "instruction_following"],
            privacy="self_hosted",
        )
        registry = ModelRegistry([entry], default_providers(eps), eps)
        monkeypatch.setattr("agentic_sdlc.skills.runtime.get_registry", lambda: registry)
        monkeypatch.setattr("agentic_sdlc.backlog.service.get_registry", lambda: registry)
        yield fake


def test_go_repository_from_brd_to_release_readiness(
    client: TestClient, project: dict, go_model: FakeOpenAI
) -> None:
    pid = project["id"]
    upload_sample_brd(client, pid)
    repo = client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"name": "tasks-go", "kind": "fixture", "location": "sample-task-service-go"},
        headers=H("owner"),
    ).json()
    profile = client.get(f"/api/v1/projects/{pid}/repositories/{repo['id']}/test-profile", headers=H("owner"))
    assert profile.json()["profile"]["name"] == "go"
    run_id = client.post(
        f"/api/v1/projects/{pid}/runs", json={"repository_id": repo["id"]}, headers=H("engineer")
    ).json()["id"]
    drain()
    arch = pending(client, pid, "reviewer")[0]
    assert decide(client, pid, arch["id"], "approved", "reviewer").status_code == 200
    drain()
    assert decide(client, pid, pending(client, pid)[0]["id"], "approved", "owner").status_code == 200
    drain()

    r = run(client, pid, run_id)
    assert r["status"] == "awaiting_approval", r["outcome"].get("escalation")
    assert r["current_stage"] == "release_staging_gate"
    assert r["counters"]["attempt"] == 2
    assert r["outcome"]["acceptance_suite"]["profile"] == "go"

    tests = client.get(f"/api/v1/projects/{pid}/test-runs?run_id={run_id}", headers=H("qa")).json()
    first = next(t for t in tests if t["suite"] == "acceptance" and t["attempt"] == 1)
    assert first["status"] == "failed"
    failing = [c for c in first["cases"] if c["outcome"] == "failed"]
    assert [c["criteria"] for c in failing] == [["FR-001-AC2"]]
    assert "rejected" in failing[0]["message"]
    regression = next(t for t in tests if t["suite"] == "regression")
    assert regression["status"] == "passed" and regression["totals"]["total"] >= 7

    defects = client.get(f"/api/v1/projects/{pid}/defects?run_id={run_id}", headers=H("qa")).json()
    assert [(d["criterion_key"], d["status"]) for d in defects] == [("FR-001-AC2", "verified")]
    verification = client.get(f"/api/v1/projects/{pid}/verification?run_id={run_id}", headers=H("qa")).json()
    latest = {v["criterion_key"]: v["status"] for v in verification if v["attempt"] == 2}
    assert latest["FR-001-AC1"] == "verified" and latest["NFR-002-AC1"] == "verified"
    rel = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()[0]
    assert rel["readiness"]["eligible"] is True
