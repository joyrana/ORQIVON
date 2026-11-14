"""The model-backed path end to end, over real HTTP to an OpenAI-compatible server.

The repository is treated as a customer repository: no scripted coding scenario and no shipped
acceptance suite. The QA agent writes the acceptance suite from the criteria (before any code exists),
the Developer agent returns whole-file edits that the platform turns into a reviewed diff, and the
independent suite catches the first attempt's boundary defect, which the repair attempt fixes.

The "model" is a scripted HTTP server, so this proves the platform's handling of model output, not a
model's quality. Live quality is measured with `agentic-sdlc models check` and `make evals-live`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import agentic_sdlc.execution.coding_agents
import agentic_sdlc.workflows.stages_build  # noqa: F401 - imported before patching (it binds load_scenario)
from agentic_sdlc.core.db import session_scope
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.models.endpoints import parse_endpoints
from agentic_sdlc.models.registry import ModelRegistry, default_providers
from agentic_sdlc.models.types import ModelEntry
from agentic_sdlc.orm import TokenUsage
from agentic_sdlc.workflows.worker import drain
from tests.conftest import H
from tests.model_server import FakeOpenAI, serve, system_prompt, user_prompt
from tests.workflow.helpers import decide, pending, run, setup_project, start

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not shutil.which("unshare") or not Path(get_settings().sandbox_python).exists(),
    reason="needs the namespace sandbox (root, unshare, sandbox toolchain)",
)

SAMPLES = get_settings().fixtures_dir
CODER = "acme/coder-7b"


def _contents_after_patches(patches: list[str], tmp: Path) -> dict[str, str]:
    """Apply the sample's recorded patches in order and return every file they touch, as edits."""
    repo = tmp / "repo"
    shutil.copytree(SAMPLES / "repositories" / "sample-task-service", repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    files: set[str] = set()
    for name in patches:
        patch = (SAMPLES / "coding" / "sample-task-service" / name).read_text()
        subprocess.run(["git", "apply", "-"], cwd=repo, input=patch.encode(), check=True)
        files |= set(re.findall(r"^diff --git a/\S+ b/(\S+)$", patch, re.M))
    return {f: (repo / f).read_text() for f in sorted(files)}


@pytest.fixture
def model_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOpenAI]:
    attempts = [
        _contents_after_patches(["attempt-1.patch"], tmp_path / "a1"),
        _contents_after_patches(["attempt-1.patch", "attempt-2.patch"], tmp_path / "a2"),
    ]
    acceptance = next((SAMPLES / "acceptance" / "sample-task-service").glob("test_*.py"))
    calls = {"code": 0}

    def handler(body: dict[str, Any]) -> dict[str, Any]:
        system, user = system_prompt(body), user_prompt(body)
        if "independent QA engineer" in system:
            keys = sorted(set(re.findall(r'"key": "([A-Z]+-\d+-AC\d+)"', user)))
            return {
                "files": [{"path": "test_acceptance_generated.py", "content": acceptance.read_text()}],
                "coverage": [{"criterion_key": k, "tests": []} for k in keys],
                "notes": ["NFR-001 has no automatable threshold"],
            }
        if "careful software engineer" in system:
            tree = set(user.split("<tree>\n", 1)[1].split("\n</tree>", 1)[0].splitlines())
            content = attempts[min(calls["code"], 1)]
            calls["code"] += 1
            return {
                "summary": f"attempt {calls['code']}: title validation",
                "edits": [
                    {"path": p, "action": "modify" if p in tree else "create", "content": c}
                    for p, c in content.items()
                ],
                "notes": [],
            }
        raise AssertionError(f"unexpected prompt: {system[:80]}")

    fake = FakeOpenAI(models=[CODER], api_key="test-key", handler=handler)
    with serve(fake):
        monkeypatch.setenv("TEST_CODER_URL", fake.url)
        monkeypatch.setenv("TEST_CODER_KEY", "test-key")
        endpoints = parse_endpoints(
            [{"name": "coder", "base_url_env": "TEST_CODER_URL", "api_key_env": "TEST_CODER_KEY"}], None
        )
        entry = ModelEntry(
            model_id="acme-coder",
            provider="openai_compatible",
            endpoint="coder",
            served_model_name=CODER,
            revision="2026-11-01",
            family="acme",
            license="Apache-2.0",
            tokenizer="acme",
            context_window=65536,
            max_output_tokens=16384,
            tasks=["code_generation", "test_generation"],
            capabilities=["code", "structured_output", "instruction_following"],
            privacy="self_hosted",
            pricing={"input_per_1k_usd": 0.001, "output_per_1k_usd": 0.002},
        )
        registry = ModelRegistry([entry], default_providers(endpoints), endpoints)
        monkeypatch.setattr("agentic_sdlc.skills.runtime.get_registry", lambda: registry)
        # Treat the sample as a customer repository: nothing scripted, no shipped acceptance suite.
        monkeypatch.setattr("agentic_sdlc.execution.coding_agents.acceptance_suite_dir", lambda repo: None)
        monkeypatch.setattr("agentic_sdlc.execution.coding_agents.load_scenario", lambda repo: None)
        monkeypatch.setattr("agentic_sdlc.workflows.stages_build.load_scenario", lambda repo: None)
        yield fake


def test_model_backed_delivery_with_independent_acceptance_suite(
    client: TestClient, project: dict, model_server: FakeOpenAI
) -> None:
    ctx = setup_project(client, project)
    pid = ctx["pid"]
    run_id = start(client, ctx)
    arch = pending(client, pid, "reviewer")[0]
    assert decide(client, pid, arch["id"], "approved", "reviewer").status_code == 200
    drain()
    scope = pending(client, pid)[0]
    assert decide(client, pid, scope["id"], "approved", "owner").status_code == 200
    drain()

    r = run(client, pid, run_id)
    assert r["status"] == "awaiting_approval", r["outcome"].get("escalation")
    assert r["current_stage"] == "release_staging_gate"
    assert r["counters"]["attempt"] == 2 and r["counters"]["repairs"] == 1

    # The QA agent wrote the suite from the criteria, against the base commit, with the model.
    suite = r["outcome"]["acceptance_suite"]
    assert suite["model_ref"] == "acme-coder@2026-11-01" and suite["is_mock"] is False
    assert suite["base_commit"] == r["outcome"]["base_commit"]
    assert list(suite["files"]) == ["test_acceptance_generated.py"]

    # The developer never saw the acceptance suite's source; the first attempt saw no test names either.
    code_requests = [b for b in model_server.requests if "careful software engineer" in system_prompt(b)]
    assert len(code_requests) == 2
    assert all('create_task("a" * 120)' not in user_prompt(b) for b in code_requests)
    assert "exactly_120_characters" not in user_prompt(code_requests[0])
    # The repair request carried the defect report (failing test and message), as a bug report would.
    repair = user_prompt(code_requests[1]).split("<defects>", 1)[1].split("</defects>", 1)[0]
    assert "120" in repair

    changes = client.get(
        f"/api/v1/projects/{pid}/code-changes?run_id={run_id}&include_diff=true", headers=H("reviewer")
    ).json()
    assert [c["coding_agent"]["strategy"] for c in changes] == ["llm", "llm"]
    assert all(c["coding_agent"]["model_ref"] == "acme-coder@2026-11-01" for c in changes)
    assert all(c["coding_agent"]["is_mock"] is False for c in changes)
    assert all(c["review"]["ok"] for c in changes)
    assert "tests/test_title_validation.py" in changes[0]["files"]

    defects = client.get(f"/api/v1/projects/{pid}/defects?run_id={run_id}", headers=H("qa")).json()
    assert [(d["criterion_key"], d["status"]) for d in defects] == [("FR-001-AC2", "verified")]
    rel = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()[0]
    assert rel["readiness"]["eligible"] is True

    # Usage reported by the server is recorded as actual, not estimated, and priced.
    with session_scope() as db:
        usage = db.scalars(select(TokenUsage).where(TokenUsage.model_id == "acme-coder")).all()
        mine = [u for u in usage if str(u.run_id) == run_id]
        assert len(mine) == 3  # one acceptance-suite call, two coding calls
        assert all(u.is_estimated is False for u in mine)
        assert all(float(u.cost_usd) > 0 for u in mine)
    assert all(h["authorization"] == "Bearer test-key" for h in model_server.headers_seen)


def test_no_model_means_no_generated_suite_and_an_honest_block(
    client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a test-generation model the run is not faked: criteria stay unverifiable."""
    monkeypatch.setattr("agentic_sdlc.execution.coding_agents.acceptance_suite_dir", lambda repo: None)
    ctx = setup_project(client, project)
    pid = ctx["pid"]
    run_id = start(client, ctx)
    arch = pending(client, pid, "reviewer")[0]
    decide(client, pid, arch["id"], "approved", "reviewer")
    drain()
    decide(client, pid, pending(client, pid)[0]["id"], "approved", "owner")
    drain()
    r = run(client, pid, run_id)
    assert "acceptance_suite" not in r["outcome"]
    assert "no runnable strategy" in r["outcome"]["acceptance_suite_unavailable"]
    assert r["status"] == "escalated" and r["current_stage"] == "release_readiness"
    assert "must-have criteria verified" in r["outcome"]["escalation"]["reason"]
