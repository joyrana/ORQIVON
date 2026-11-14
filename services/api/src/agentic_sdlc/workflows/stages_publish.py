"""Publishing a verified change: push the branch, open the pull request, wait for external CI.

These stages run after the independent regression run passed. They are idempotent: the branch push
is skipped when the remote already has the commit, and an existing pull request for the branch is
reused. The platform never merges; merging stays with people and the host's branch protection.

Sample (fixture) repositories have no remote: the change stays on the platform's branch, and the
stages record that instead of pretending to publish.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.audit import service as audit
from agentic_sdlc.core.db import utcnow
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.events import service as events
from agentic_sdlc.integrations.git_hosting import GitHostingError, HostingClient
from agentic_sdlc.orm import (
    AcceptanceCriterion,
    CodeChange,
    Defect,
    Project,
    TestRun,
    VerificationResult,
    WorkflowRun,
)
from agentic_sdlc.projects.repositories import publish_branch, remote
from agentic_sdlc.workflows import jobs
from agentic_sdlc.workflows.delivery import DeliveryState, Escalate, node, stage
from agentic_sdlc.workflows.stages_build import _attempt, _repo, scope_criteria


def published_branch(run: WorkflowRun) -> str:
    return f"asw/run-{str(run.id)[:8]}"


def _latest_change(db: Session, run: WorkflowRun) -> CodeChange:
    change = db.scalar(
        select(CodeChange)
        .where(CodeChange.run_id == run.id, CodeChange.status == "applied")
        .order_by(CodeChange.attempt.desc())
        .limit(1)
    )
    if change is None:
        raise Escalate("no applied change to publish")
    return change


def pull_request_body(db: Session, run: WorkflowRun) -> str:
    """Reviewer-facing summary: what changed, why, and the evidence. No secrets, no prompts."""
    project = db.get(Project, run.project_id)
    attempt = _attempt(run)
    criteria = scope_criteria(db, run)
    verdicts = {
        c.key: v.status
        for v, c in db.execute(
            select(VerificationResult, AcceptanceCriterion)
            .join(AcceptanceCriterion, AcceptanceCriterion.id == VerificationResult.acceptance_criterion_id)
            .where(VerificationResult.run_id == run.id, VerificationResult.attempt == attempt)
        ).all()
    }
    regression = db.scalar(
        select(TestRun).where(
            TestRun.run_id == run.id, TestRun.suite == "regression", TestRun.attempt == attempt
        )
    )
    defects = db.scalars(select(Defect).where(Defect.run_id == run.id)).all()
    name = project.name if project else str(run.project_id)
    lines = [
        f"Delivered by the Agentic SDLC Workspace for project **{name}** (run `{run.id}`).",
        "",
        "Backlog items: " + ", ".join(f"`{k}`" for k in run.outcome.get("delivered_items", [])),
        "",
        "### Acceptance criteria",
        "",
        "| Criterion | Priority | Verification |",
        "|---|---|---|",
        *[f"| `{c['key']}` | {c['priority']} | {verdicts.get(c['key'], 'missing')} |" for c in criteria],
        "",
        "### Evidence",
        "",
    ]
    if regression is not None:
        t = regression.totals
        lines.append(
            f"- Regression suite on `{(regression.commit or '')[:12]}`: **{regression.status}** "
            f"({t.get('passed', 0)}/{t.get('total', 0)} passed), run in the platform sandbox."
        )
    lines.append(f"- Attempts: {attempt}; defects found and repaired: {sum(1 for d in defects)}.")
    suite = run.outcome.get("acceptance_suite")
    if suite:
        lines.append(
            "- Acceptance tests were written independently by the QA agent "
            f"({'mock' if suite.get('is_mock') else suite.get('model_ref')}) and are not part of this change."
        )
    lines += [
        "",
        "Architecture and scope were approved by people before implementation. "
        "This pull request still needs your review; the platform does not merge.",
    ]
    return "\n".join(lines)


@node("publish_change")
def publish_change(state: DeliveryState) -> DeliveryState:
    with stage(state["run_id"], "publish_change") as (db, run, _):
        repo = _repo(db, run)
        change = _latest_change(db, run)
        head = run.outcome["head_commit"]
        if repo.kind != "git":
            run.outcome = {
                **run.outcome,
                "publication": {
                    "remote": False,
                    "detail": f"sample repository: the change stays on platform branch {change.branch}",
                },
            }
            return {"outcome": "ok"}
        conn, ref = remote(repo)
        branch = published_branch(run)
        pr_state: dict[str, Any] = dict(change.pull_request or {})
        try:
            if pr_state.get("pushed_sha") != head:
                publish_branch(repo, branch, head)
                pr_state.update(branch=branch, pushed_sha=head, pushed_at=utcnow().isoformat())
                audit.record(
                    db,
                    action="change.branch_published",
                    actor_id=None,
                    actor_type="system",
                    project_id=run.project_id,
                    run_id=run.id,
                    target_type="code_change",
                    target_id=change.id,
                    details={"branch": branch, "commit": head, "remote": ref.url},
                )
            if conn.allows("pull_request") and conn.provider != "generic":
                project = db.get(Project, run.project_id)
                title = f"[{project.key if project else 'ASW'}] " + ", ".join(
                    run.outcome.get("delivered_items", [])[:5]
                )
                pr = HostingClient(conn, ref).open_pull_request(
                    branch=branch,
                    base=repo.default_branch,
                    title=title[:200],
                    body=pull_request_body(db, run),
                )
                pr_state.update(pr.as_dict(), provider=conn.provider)
                change.pull_request_url = pr.url
                if pr.created:
                    audit.record(
                        db,
                        action="change.pull_request_opened",
                        actor_id=None,
                        actor_type="system",
                        project_id=run.project_id,
                        run_id=run.id,
                        target_type="code_change",
                        target_id=change.id,
                        details={"url": pr.url, "number": pr.number, "branch": branch},
                    )
        except GitHostingError as exc:
            change.pull_request = pr_state
            raise Escalate(f"could not publish the change: {exc}") from exc
        change.pull_request = pr_state
        run.outcome = {
            **run.outcome,
            "publication": {
                "remote": True,
                "branch": branch,
                "commit": head,
                "pull_request": pr_state.get("url"),
                "detail": "pull request open"
                if pr_state.get("url")
                else "branch pushed (no pull-request API)",
            },
        }
        events.emit(
            db,
            type="change.published",
            project_id=run.project_id,
            run_id=run.id,
            payload={"branch": branch, "commit": head, "pull_request": pr_state.get("url")},
        )
    return {"outcome": "ok"}


def _require_ci(run: WorkflowRun) -> bool:
    return bool(run.config.get("limits", {}).get("require_external_ci", True))


@node("external_ci")
def external_ci(state: DeliveryState) -> DeliveryState:
    """Check the host's CI on the published commit: ok when green (or not configured), escalate when
    red or timed out, otherwise schedule the next poll and hand over to `external_ci_wait`."""
    settings = get_settings()
    with stage(state["run_id"], "external_ci") as (db, run, _):
        repo = _repo(db, run)
        head = run.outcome["head_commit"]
        if repo.kind != "git":
            run.outcome = {**run.outcome, "external_ci": {"state": "not_configured", "detail": "no remote"}}
            return {"outcome": "ok"}
        conn, ref = remote(repo)
        if not conn.allows("checks") or conn.provider == "generic":
            run.outcome = {
                **run.outcome,
                "external_ci": {
                    "state": "not_configured",
                    "detail": f"connection '{conn.name}' cannot read CI",
                },
            }
            return {"outcome": "ok"}
        started = run.outcome.get("external_ci", {}).get("started_at") or utcnow().isoformat()
        try:
            status = HostingClient(conn, ref).ci_status(head)
        except GitHostingError as exc:
            raise Escalate(f"could not read external CI status: {exc}") from exc
        elapsed = (utcnow() - datetime.fromisoformat(started)).total_seconds()
        record = {
            **status.as_dict(),
            "commit": head,
            "started_at": started,
            "checked_at": utcnow().isoformat(),
            "required": _require_ci(run),
        }
        grace = min(300, settings.external_ci_timeout_s)
        waiting = status.state == "pending" or (status.state == "none" and elapsed < grace)
        if status.state == "failure":
            run.outcome = {**run.outcome, "external_ci": record}
            failed = [c["name"] for c in status.checks if c["state"] == "failure"]
            if _require_ci(run):
                raise Escalate(f"external CI failed on the pull request: {', '.join(failed)}")
            return {"outcome": "ok"}
        if waiting and elapsed < settings.external_ci_timeout_s:
            polls = int(run.counters.get("external_ci_polls", 0)) + 1
            run.counters = {**run.counters, "external_ci_polls": polls}
            run.outcome = {**run.outcome, "external_ci": record}
            run.status = "waiting_external"
            jobs.enqueue(
                db,
                kind="delivery.resume",
                dedup_key=f"external_ci:{run.id}:{polls}",
                run_id=run.id,
                payload={"run_id": str(run.id), "reason": "external_ci"},
                delay=timedelta(seconds=settings.external_ci_poll_s),
            )
            return {"outcome": "wait"}
        elif waiting:
            run.outcome = {**run.outcome, "external_ci": {**record, "state": "timed_out"}}
            if _require_ci(run):
                raise Escalate(f"external CI did not finish within {settings.external_ci_timeout_s} s")
            return {"outcome": "ok"}
        run.outcome = {**run.outcome, "external_ci": record}
    return {"outcome": "ok"}


@node("external_ci_wait")
def external_ci_wait(state: DeliveryState) -> DeliveryState:
    """Suspend until the scheduled poll resumes the run.

    Kept separate from `external_ci` because LangGraph re-executes an interrupted node from the top
    on resume: the check (and the poll it schedules) must not run twice per cycle.
    """
    interrupt({"gate": "external_ci"})
    return {"outcome": "ok"}
