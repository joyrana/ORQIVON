"""Deployment connectors and the release review loop.

* Each connector (GitHub Actions, Jenkins, Azure Pipelines, Argo CD) is triggered and followed to
  success or failure against a fake service over HTTP.
* A full delivery deploys staging through the project's GitHub Actions target: the run waits while
  the pipeline runs, then verifies smoke checks (in the sandbox) and the environment's health URL.
* A failed pipeline escalates; a failed health check rolls back.
* "Request changes" at the release gate sends the change back to the Developer agent and a new
  release candidate returns to the gate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.integrations import deploy_targets
from agentic_sdlc.integrations.deploy_targets import DeployRequest
from agentic_sdlc.workflows.worker import drain
from tests.cicd_server import FakeCICD, serve_cicd
from tests.conftest import H
from tests.workflow.helpers import decide, pending, run, setup_project, start

needs_sandbox = pytest.mark.skipif(
    os.geteuid() != 0 or not shutil.which("unshare") or not Path(get_settings().sandbox_python).exists(),
    reason="needs the namespace sandbox (root, unshare, sandbox toolchain)",
)
SAMPLES = get_settings().fixtures_dir


@pytest.fixture
def cicd(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCICD]:
    with serve_cicd() as fake:
        monkeypatch.setenv("DEPLOY_TOKEN", "deploy-token")
        monkeypatch.setenv("JENKINS_USER", "deployer")
        monkeypatch.setenv("JENKINS_TOKEN", "jenkins-api-token")
        monkeypatch.setenv("ADO_PAT", "ado-pat")
        bearer = {"base_url": fake.url, "auth": "bearer", "token_env": "DEPLOY_TOKEN"}
        targets = [
            {
                "name": "gha",
                "kind": "github_actions",
                **bearer,
                "repo": "acme/payments",
                "workflow": "deploy.yml",
                "health_url": f"{fake.url}/healthz",
            },
            {
                "name": "jenkins",
                "kind": "jenkins",
                "base_url": fake.url,
                "auth": "basic",
                "username_env": "JENKINS_USER",
                "token_env": "JENKINS_TOKEN",
                "job": "payments/deploy-staging",
            },
            {
                "name": "ado",
                "kind": "azure_pipelines",
                "base_url": fake.url,
                "auth": "pat",
                "token_env": "ADO_PAT",
                "project": "Payments",
                "pipeline_id": 7,
            },
            {"name": "argo", "kind": "argocd", **bearer, "application": "payments-staging"},
        ]
        s = get_settings()
        monkeypatch.setattr(s, "integrations_insecure_http_hosts", ["127.0.0.1"])
        monkeypatch.setattr(s, "deploy_targets", json.dumps(targets))
        monkeypatch.setattr(s, "deploy_poll_s", 1)
        deploy_targets.load_targets.cache_clear()
        yield fake
        deploy_targets.load_targets.cache_clear()


REQ = DeployRequest(
    deployment_id="d-1", environment="staging", version="1.2.0", commit="a" * 40, artifact_sha256="b" * 64
)


@pytest.mark.parametrize("name", ["gha", "jenkins", "ado", "argo"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_each_connector_is_triggered_and_followed(cicd: FakeCICD, name: str, outcome: str) -> None:
    cicd.outcome = outcome
    target = deploy_targets.get_target(name, "staging")
    ref = target.trigger(REQ)
    states = []
    for _ in range(6):
        st = target.status(ref)
        states.append(st.state)
        if st.state != "running":
            break
    assert states[0] == "running"
    assert states[-1] == ("succeeded" if outcome == "success" else "failed"), (name, states)
    trigger = cicd.triggers[-1]
    if name == "argo":
        assert trigger["revision"] == REQ.commit
    else:
        params = {k.lower(): v for k, v in trigger.items()}
        assert params["release_version"] == "1.2.0" and params["commit"] == REQ.commit
        assert params["asw_deployment_id"] == "d-1" and params["artifact_sha256"] == REQ.artifact_sha256
    if name == "jenkins":
        assert trigger["job"] == "payments/job/deploy-staging"
    if outcome == "failure" and name == "argo":
        assert "hook failed" in st.detail


def test_targets_are_validated_and_scoped_to_environments(cicd: FakeCICD) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="repo, workflow"):
        deploy_targets.DeployTarget(name="x", kind="github_actions", auth="none")
    with pytest.raises(ValidationError, match="https"):
        deploy_targets.DeployTarget(name="x", kind="argocd", base_url="http://argo.internal", application="a")
    from agentic_sdlc.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        deploy_targets.get_target("gha", "production")  # declared for staging only


# --- full deliveries ------------------------------------------------------------------------------


def _bind(client: TestClient, pid: str, target: str) -> None:
    r = client.put(
        f"/api/v1/projects/{pid}/integrations", json={"deploy": {"staging": target}}, headers=H("owner")
    )
    assert r.status_code == 200, r.text


def _to_release_gate(client: TestClient, ctx: dict[str, Any]) -> str:
    pid = ctx["pid"]
    run_id = start(client, ctx)
    assert (
        decide(client, pid, pending(client, pid, "reviewer")[0]["id"], "approved", "reviewer").status_code
        == 200
    )
    drain()
    assert decide(client, pid, pending(client, pid)[0]["id"], "approved", "owner").status_code == 200
    drain()
    r = run(client, pid, run_id)
    assert r["current_stage"] == "release_staging_gate", r["outcome"].get("escalation")
    return run_id


def _approve_release(client: TestClient, pid: str) -> None:
    req = next(a for a in pending(client, pid, "approver") if a["gate"] == "release_staging")
    assert decide(client, pid, req["id"], "approved", "approver").status_code == 200
    drain()


def _until_done(client: TestClient, pid: str, run_id: str, limit: int = 15) -> dict[str, Any]:
    for _ in range(limit):
        r = run(client, pid, run_id)
        if r["status"] != "waiting_external":
            return r
        time.sleep(1.2)  # let the scheduled poll fall due
        drain()
    return run(client, pid, run_id)


@needs_sandbox
def test_staging_deploys_through_github_actions_and_is_verified(
    client: TestClient, project: dict, cicd: FakeCICD
) -> None:
    ctx = setup_project(client, project)
    pid = ctx["pid"]
    _bind(client, pid, "gha")
    run_id = _to_release_gate(client, ctx)
    _approve_release(client, pid)
    r = run(client, pid, run_id)
    assert r["status"] == "waiting_external"  # the pipeline is running; the run waits without a worker
    r = _until_done(client, pid, run_id)
    assert r["status"] == "completed", r["outcome"].get("escalation")
    rel = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()[0]
    assert rel["status"] == "deployed_staging"
    dep = client.get(f"/api/v1/projects/{pid}/releases/{rel['id']}", headers=H("approver")).json()[
        "deployments"
    ][0]
    assert dep["adapter"] == "github_actions:gha" and dep["status"] == "succeeded"
    assert dep["external"]["url"].endswith("/actions/runs/1")
    assert [c["ok"] for c in dep["smoke"]["checks"]] and all(c["ok"] for c in dep["smoke"]["checks"])
    assert dep["health"]["checks"] == [{"name": "health_url", "ok": True, "detail": "HTTP 200"}]
    assert (
        cicd.triggers[0]["commit"] == rel["commit"] and cicd.triggers[0]["release_version"] == rel["version"]
    )


@needs_sandbox
def test_failed_pipeline_escalates_and_failed_health_rolls_back(
    client: TestClient, project: dict, cicd: FakeCICD
) -> None:
    ctx = setup_project(client, project)
    pid = ctx["pid"]
    _bind(client, pid, "gha")
    cicd.outcome = "failure"
    run_id = _to_release_gate(client, ctx)
    _approve_release(client, pid)
    r = _until_done(client, pid, run_id)
    assert r["status"] == "escalated" and r["current_stage"] == "deploy_staging"
    assert "workflow failure" in r["outcome"]["escalation"]["reason"]

    cicd.outcome, cicd.health_status = "success", 503
    run2 = _to_release_gate(client, ctx)
    _approve_release(client, pid)
    r = _until_done(client, pid, run2)
    assert r["status"] == "escalated" and "rolled back" in r["outcome"]["escalation"]["reason"]
    rel = next(
        x
        for x in client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()
        if x["run_id"] == run2
    )
    dep = client.get(f"/api/v1/projects/{pid}/releases/{rel['id']}", headers=H("approver")).json()[
        "deployments"
    ][0]
    assert dep["status"] == "rolled_back"
    # No earlier successful release in staging: the rollback says so instead of pretending.
    assert "nothing to roll back to" in dep["rollback"]["log"][0]


# --- release review loop ---------------------------------------------------------------------------

DOCSTRING = '        """Create a task after validating its title (see FR-001, FR-002, FR-003)."""\n'


def _scenario_with_review_fix(tmp: Path) -> Path:
    repo = tmp / "repo"
    shutil.copytree(SAMPLES / "repositories" / "sample-task-service", repo)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    src = SAMPLES / "coding" / "sample-task-service"
    for patch in ("attempt-1.patch", "attempt-2.patch"):
        subprocess.run(["git", "apply", str(src / patch)], cwd=repo, check=True)
    subprocess.run([*g, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*g, "commit", "-qm", "a2"], cwd=repo, check=True)
    service = repo / "task_service" / "service.py"
    text = service.read_text()
    marker = "    def create_task("
    head, tail = text.split(marker, 1)
    sig, body = tail.split("\n", 1)
    service.write_text(head + marker + sig + "\n" + DOCSTRING + body)
    out = tmp / "scenario"
    out.mkdir()
    for patch in ("attempt-1.patch", "attempt-2.patch"):
        shutil.copy(src / patch, out / patch)
    (out / "attempt-3.patch").write_text(
        subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True, check=True).stdout
    )
    data = yaml.safe_load((src / "scenario.yaml").read_text())
    data["attempts"] = [*data["attempts"], {"patch": "attempt-3.patch", "summary": "document create_task"}]
    (out / "scenario.yaml").write_text(yaml.safe_dump(data))
    return out


@needs_sandbox
def test_release_changes_go_back_to_the_developer_and_return_as_a_new_candidate(
    client: TestClient, project: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario_with_review_fix(tmp_path)
    monkeypatch.setattr("agentic_sdlc.execution.coding_agents.scenario_dir", lambda repo: scenario)
    ctx = setup_project(client, project)
    pid = ctx["pid"]
    run_id = _to_release_gate(client, ctx)
    first = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()[0]
    req = next(a for a in pending(client, pid, "approver") if a["gate"] == "release_staging")
    comment = "Document create_task: name the requirements it implements."
    assert decide(client, pid, req["id"], "changes_requested", "approver", comment).status_code == 200
    drain()

    r = run(client, pid, run_id)
    assert r["current_stage"] == "release_staging_gate", r["outcome"].get("escalation")
    assert r["counters"]["attempt"] == 3
    releases = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()
    assert len(releases) == 2
    old = next(x for x in releases if x["id"] == first["id"])
    new = next(x for x in releases if x["id"] != first["id"])
    assert old["status"] == "changes_requested" and new["commit"] != old["commit"]
    assert new["readiness"]["eligible"] is True
    defects = client.get(f"/api/v1/projects/{pid}/defects?run_id={run_id}", headers=H("qa")).json()
    review = next(d for d in defects if d["title"].startswith("Release change requested"))
    assert review["actual"] == comment and review["status"] == "verified"
    # The developer was asked to make exactly that change.
    changes = client.get(
        f"/api/v1/projects/{pid}/code-changes?run_id={run_id}&include_diff=true", headers=H("reviewer")
    ).json()
    assert len(changes) == 3 and "Create a task after validating its title" in changes[-1]["diff"]

    _approve_release(client, pid)
    assert run(client, pid, run_id)["status"] == "completed"
