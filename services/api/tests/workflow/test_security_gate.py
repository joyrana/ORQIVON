"""The security gate in a real delivery: a change that adds dynamic code execution passes its
tests, is blocked by the Security agent, goes back to the Developer agent as a defect, and the
repaired change is released. Risk acceptance and the policy API are checked alongside."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import agentic_sdlc.execution.coding_agents  # noqa: F401
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.workflows.worker import drain
from tests.conftest import H
from tests.workflow.helpers import decide, pending, run, setup_project, start

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not shutil.which("unshare") or not Path(get_settings().sandbox_python).exists(),
    reason="needs the namespace sandbox (root, unshare, sandbox toolchain)",
)

SAMPLES = get_settings().fixtures_dir
RISKY = "\n\ndef debug_eval(expression: str) -> object:\n    return eval(expression)\n"


def _git(repo: Path, *args: str) -> str:
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    return subprocess.run([*g, *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _scenario(tmp: Path, *, fix: bool) -> Path:
    """Attempt 1 as shipped; attempt 2 fixes the boundary but adds eval(); attempt 3 removes it."""
    repo = tmp / "repo"
    shutil.copytree(SAMPLES / "repositories" / "sample-task-service", repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    src = SAMPLES / "coding" / "sample-task-service"
    out = tmp / "scenario"
    out.mkdir()
    shutil.copy(src / "attempt-1.patch", out / "attempt-1.patch")
    _git(repo, "apply", str(src / "attempt-1.patch"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a1")
    _git(repo, "apply", str(src / "attempt-2.patch"))
    service = repo / "task_service" / "service.py"
    service.write_text(service.read_text() + RISKY)
    _git(repo, "add", "-A")
    (out / "attempt-2.patch").write_text(_git(repo, "diff", "--cached"))
    _git(repo, "commit", "-qm", "a2")
    attempts = [
        {"patch": "attempt-1.patch", "summary": "title rules"},
        {"patch": "attempt-2.patch", "summary": "boundary fix, with a debug helper"},
    ]
    if fix:
        service.write_text(service.read_text().replace(RISKY, "\n"))
        (out / "attempt-3.patch").write_text(_git(repo, "diff"))
        attempts.append({"patch": "attempt-3.patch", "summary": "remove the debug helper"})
    data = yaml.safe_load((src / "scenario.yaml").read_text())
    (out / "scenario.yaml").write_text(yaml.safe_dump({**data, "attempts": attempts}))
    return out


def _through_scope(client: TestClient, ctx: dict[str, Any]) -> str:
    pid = ctx["pid"]
    run_id = start(client, ctx)
    assert (
        decide(client, pid, pending(client, pid, "reviewer")[0]["id"], "approved", "reviewer").status_code
        == 200
    )
    drain()
    assert decide(client, pid, pending(client, pid)[0]["id"], "approved", "owner").status_code == 200
    drain()
    return run_id


def test_security_defect_is_repaired_before_release(
    client: TestClient, project: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario(tmp_path, fix=True)
    monkeypatch.setattr("agentic_sdlc.execution.coding_agents.scenario_dir", lambda repo: scenario)
    ctx = setup_project(client, project)
    pid = ctx["pid"]
    run_id = _through_scope(client, ctx)

    r = run(client, pid, run_id)
    assert r["status"] == "awaiting_approval", r["outcome"].get("escalation")
    assert r["current_stage"] == "release_staging_gate"
    assert r["counters"]["attempt"] == 3

    scans = client.get(f"/api/v1/projects/{pid}/security-scans?run_id={run_id}", headers=H("qa")).json()
    assert [(s["attempt"], s["status"]) for s in scans] == [(3, "passed"), (2, "blocked")]
    blocked = scans[1]
    finding = next(f for f in blocked["findings"] if f["fingerprint"] in blocked["blocking"])
    assert (finding["rule_id"], finding["path"], finding["introduced"]) == (
        "py-eval",
        "task_service/service.py",
        True,
    )
    assert scans[0]["head_commit"] == r["outcome"]["head_commit"]
    assert scans[0]["base_commit"] == r["outcome"]["base_commit"]  # cumulative change, not the last step

    defects = client.get(f"/api/v1/projects/{pid}/defects?run_id={run_id}", headers=H("qa")).json()
    security = [d for d in defects if d["title"].startswith("Security:")]
    assert len(security) == 1 and security[0]["status"] == "verified"
    assert security[0]["severity"] == "major"

    rel = client.get(f"/api/v1/projects/{pid}/releases", headers=H("approver")).json()[0]
    checks = {c["name"]: c["status"] for c in rel["readiness"]["checks"]}
    assert checks["security scan passed on the release commit"] == "pass"
    agents = {a["agent"] for a in r["agent_runs"]}
    assert "security" in agents


def test_blocking_finding_escalates_at_the_limit_and_can_be_accepted(
    client: TestClient, project: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario(tmp_path, fix=False)
    monkeypatch.setattr("agentic_sdlc.execution.coding_agents.scenario_dir", lambda repo: scenario)
    ctx = setup_project(client, project, limits={"max_repair_attempts": 1})
    pid = ctx["pid"]
    run_id = _through_scope(client, ctx)
    r = run(client, pid, run_id)
    assert r["status"] == "escalated" and r["current_stage"] == "security_scan"
    assert "security finding(s) still block" in r["outcome"]["escalation"]["reason"]
    assert r["outcome"].get("publication") is None  # nothing was published

    scan = client.get(f"/api/v1/projects/{pid}/security-scans?run_id={run_id}", headers=H("qa")).json()[0]
    fp = scan["blocking"][0]
    url = f"/api/v1/projects/{pid}/security-acceptances"
    reason = "Debug helper is only reachable from the admin CLI; tracked in SEC-42."
    # Engineers and QA cannot accept risk; reasons are required; unknown findings are refused.
    assert (
        client.post(url, json={"fingerprint": fp, "reason": reason}, headers=H("engineer")).status_code == 403
    )
    assert (
        client.post(url, json={"fingerprint": fp, "reason": "ok"}, headers=H("approver")).status_code == 422
    )
    assert (
        client.post(url, json={"fingerprint": "0" * 32, "reason": reason}, headers=H("approver")).status_code
        == 404
    )
    made = client.post(url, json={"fingerprint": fp, "reason": reason}, headers=H("approver"))
    assert made.status_code == 201, made.text
    assert client.post(url, json={"fingerprint": fp, "reason": reason}, headers=H("owner")).status_code == 409
    listed = client.get(f"/api/v1/projects/{pid}/security-scans?run_id={run_id}", headers=H("qa")).json()[0]
    assert next(f for f in listed["findings"] if f["fingerprint"] == fp)["accepted"] is True
    audit = client.get(f"/api/v1/projects/{pid}/audit?limit=200", headers=H("owner")).json()["items"]
    entry = next(a for a in audit if a["action"] == "security.risk_accepted")
    assert entry["details"]["reason"] == reason

    # A new run honours the acceptance: the same change is no longer blocked.
    run2 = start(client, ctx)
    assert (
        decide(client, pid, pending(client, pid, "reviewer")[0]["id"], "approved", "reviewer").status_code
        == 200
    )
    drain()
    assert decide(client, pid, pending(client, pid)[0]["id"], "approved", "owner").status_code == 200
    drain()
    r2 = run(client, pid, run2)
    assert r2["current_stage"] == "release_staging_gate", r2["outcome"].get("escalation")
    assert r2["outcome"]["security"]["status"] == "passed"

    acc_id = made.json()["id"]
    assert client.delete(f"{url}/{acc_id}", headers=H("engineer")).status_code == 403
    assert client.delete(f"{url}/{acc_id}", headers=H("approver")).status_code == 204


def test_security_config_api(client: TestClient, project: dict) -> None:
    pid = project["id"]
    url = f"/api/v1/projects/{pid}/security-config"
    assert client.get(url, headers=H("qa")).json()["block_severity"] == "high"
    cfg = {
        "block_severity": "medium",
        "require_dependency_scan": True,
        "tools": [
            {
                "name": "semgrep",
                "command": ["semgrep", "scan", "--sarif", "-o", ".asw/s.sarif"],
                "sarif": ".asw/s.sarif",
            }
        ],
    }
    assert client.put(url, json=cfg, headers=H("engineer")).status_code == 403
    r = client.put(url, json=cfg, headers=H("owner"))
    assert r.status_code == 200, r.text
    assert r.json()["tools"][0]["timeout_s"] == 600
    bad = {**cfg, "tools": [{"name": "x", "command": ["t"], "sarif": "../out.sarif"}]}
    assert client.put(url, json=bad, headers=H("owner")).status_code == 422
    assert client.put(url, json={"block_severity": "urgent"}, headers=H("owner")).status_code == 422
    assert re.match(r"^medium$", client.get(url, headers=H("auditor")).json()["block_severity"])
