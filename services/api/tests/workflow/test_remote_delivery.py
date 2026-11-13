"""Delivery on a remote GitHub-style repository, end to end over real HTTP.

The repository lives on a fake host served by `git http-backend`; the code model is a scripted
OpenAI-compatible server. The run syncs the mirror, delivers the change through the independent
QA suite and a repair, pushes `asw/run-<id>`, opens a pull request with the evidence, waits for the
host's CI on that commit, and only then evaluates release readiness.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.workflows.worker import drain
from tests.conftest import H
from tests.git_server import FakeHost
from tests.integration.test_documents_requirements import upload_sample_brd
from tests.integration.test_remote_git import connections, github, host  # noqa: F401 - fixtures
from tests.model_server import FakeOpenAI, system_prompt
from tests.workflow.helpers import decide, pending, run
from tests.workflow.test_llm_delivery import model_server  # noqa: F401 - fixture

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not shutil.which("unshare") or not Path(get_settings().sandbox_python).exists(),
    reason="needs the namespace sandbox",
)


def _sample_files() -> dict[str, str]:
    root = get_settings().fixtures_dir / "repositories" / "sample-task-service"
    return {
        str(p.relative_to(root)): p.read_text()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def poll() -> None:
    """Let the next scheduled CI poll fall due, then run it."""
    time.sleep(1.2)
    drain()


@pytest.fixture
def fast_ci() -> Any:
    s = get_settings()
    saved = s.external_ci_poll_s
    s.external_ci_poll_s = 1  # > 0, or drain() would spin on immediately-due polls
    yield
    s.external_ci_poll_s = saved


def _deliver_to_ci(
    client: TestClient, project: dict, fake: FakeHost, configure: Any, monkeypatch: Any
) -> tuple[str, str, dict]:
    monkeypatch.setenv("TEST_GH_TOKEN", fake.token)
    configure(github(fake))
    fake.create_repo("acme/tasks", _sample_files())
    pid = project["id"]
    upload_sample_brd(client, pid)
    repo = client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"name": "tasks", "kind": "git", "location": f"{fake.url}/acme/tasks", "connection": "gh"},
        headers=H("owner"),
    ).json()
    drain()
    run_id = client.post(
        f"/api/v1/projects/{pid}/runs", json={"repository_id": repo["id"]}, headers=H("engineer")
    ).json()["id"]
    drain()
    assert (
        decide(client, pid, pending(client, pid, "reviewer")[0]["id"], "approved", "reviewer").status_code
        == 200
    )
    drain()
    assert decide(client, pid, pending(client, pid)[0]["id"], "approved", "owner").status_code == 200
    drain()
    return pid, run_id, repo


def test_remote_repository_delivery_waits_for_external_ci_then_becomes_eligible(
    client: TestClient,
    project: dict,
    host: FakeHost,  # noqa: F811
    connections: Any,  # noqa: F811
    model_server: FakeOpenAI,  # noqa: F811
    fast_ci: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, run_id, _ = _deliver_to_ci(client, project, host, connections, monkeypatch)
    r = run(client, pid, run_id)
    assert r["status"] == "waiting_external", r["outcome"].get("escalation")
    assert r["current_stage"] == "external_ci"
    head = r["outcome"]["head_commit"]
    assert r["counters"]["attempt"] == 2  # QA's suite caught the boundary defect; repaired

    # Published: the verified commit on asw/run-<id>, and one pull request carrying the evidence.
    branch = f"asw/run-{run_id[:8]}"
    assert host.head("acme/tasks", branch) == head
    assert host.head("acme/tasks", "main") == r["outcome"]["base_commit"]  # main untouched
    assert len(host.pulls) == 1
    pr = host.pulls[0]
    assert pr["head"] == branch and pr["base"] == "main"
    assert "| `FR-001-AC2` | must | verified |" in pr["body"]
    assert "does not merge" in pr["body"]
    assert r["outcome"]["publication"]["pull_request"].endswith("/pull/1")

    # CI still running: nothing moves.
    host.checks[head] = [{"name": "ci/build", "status": "in_progress"}]
    poll()
    assert run(client, pid, run_id)["status"] == "waiting_external"
    # CI green on exactly the released commit: readiness is evaluated and the release gate opens.
    host.checks[head] = [{"name": "ci/build", "status": "completed", "conclusion": "success"}]
    poll()
    r = run(client, pid, run_id)
    assert r["status"] == "awaiting_approval" and r["current_stage"] == "release_staging_gate"
    assert r["outcome"]["external_ci"]["state"] == "success"
    rel = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()[0]
    checks = {c["name"]: c for c in rel["readiness"]["checks"]}
    assert checks["change published for review"]["status"] == "pass"
    assert checks["external CI passed on the release commit"]["status"] == "pass"
    assert rel["readiness"]["eligible"] is True

    changes = client.get(f"/api/v1/projects/{pid}/code-changes?run_id={run_id}", headers=H("reviewer")).json()
    assert changes[-1]["pull_request_url"].endswith("/pull/1")
    audit = client.get(
        f"/api/v1/projects/{pid}/audit?action=change.pull_request_opened", headers=H("auditor")
    )
    assert audit.json()["items"][0]["details"]["branch"] == branch
    # No credential ever reached the model or a test run.
    assert all(host.token not in str(b) for b in model_server.requests)
    assert all(
        "careful software engineer" in system_prompt(b) or "QA" in system_prompt(b)
        for b in model_server.requests
    )


def test_red_external_ci_blocks_the_release(
    client: TestClient,
    project: dict,
    host: FakeHost,  # noqa: F811
    connections: Any,  # noqa: F811
    model_server: FakeOpenAI,  # noqa: F811
    fast_ci: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, run_id, _ = _deliver_to_ci(client, project, host, connections, monkeypatch)
    head = run(client, pid, run_id)["outcome"]["head_commit"]
    host.checks[head] = [
        {"name": "ci/build", "status": "completed", "conclusion": "success"},
        {"name": "ci/security", "status": "completed", "conclusion": "failure"},
    ]
    poll()
    r = run(client, pid, run_id)
    assert r["status"] == "escalated" and r["current_stage"] == "external_ci"
    assert "external CI failed on the pull request: ci/security" in r["outcome"]["escalation"]["reason"]
