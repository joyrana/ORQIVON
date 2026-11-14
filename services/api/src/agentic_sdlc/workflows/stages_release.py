"""Release stages: release_readiness → release_gate → deploy_staging (→ deploy_wait)* →
post_deploy_verification.

"Request changes" at the release gate is a repair, not a dead end: the approver's comment becomes a
defect for the Developer agent, the change goes through tests, verification, the security gate and
publication again, and a new release candidate comes back to the gate (bounded by the repair limit).
External deployment targets are followed with scheduled polls while the run waits.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.backlog.service import next_key as next_backlog_key
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.events import service as events
from agentic_sdlc.orm import BacklogItem, CodeChange, Defect, Deployment, Project, Release, WorkflowRun
from agentic_sdlc.releases import service as releases
from agentic_sdlc.skills.runtime import SkillRuntime
from agentic_sdlc.workflows import jobs
from agentic_sdlc.workflows.delivery import (
    DeliveryState,
    Escalate,
    _gate,
    agent_ctx,
    escalate_now,
    node,
    stage,
)
from agentic_sdlc.workflows.stages_build import scope_criteria


def _release(db: Session, run: WorkflowRun) -> Release:
    rel = db.get(Release, uuid.UUID(run.outcome["release_id"]))
    assert rel is not None
    return rel


@node("release_readiness")
def release_readiness(state: DeliveryState) -> DeliveryState:
    with stage(state["run_id"], "release_readiness") as (db, run, budget):
        if "release_id" not in run.outcome:
            facts = releases.gather_facts(db, run, scope_criteria(db, run))
            report = (
                SkillRuntime()
                .execute(
                    "release-readiness",
                    {"facts": facts},
                    agent_ctx(db, run, "shipping", "release_readiness", budget),
                )
                .output
            )
            release = releases.create_release(db, run, report)
            run.outcome = {**run.outcome, "release_id": str(release.id)}
        release = _release(db, run)
        if not release.readiness["eligible"]:
            escalate_now(
                db,
                run,
                "release_readiness",
                "release is not eligible: " + "; ".join(release.readiness["blockers"]),
            )
            return {"halt": True}
    return {"outcome": "ok"}


@node("release_gate")
def release_gate(state: DeliveryState) -> DeliveryState:
    def approved(db: Session, run: WorkflowRun) -> None:
        _release(db, run).status = "approved"

    def changes(db: Session, run: WorkflowRun, comment: str) -> str:
        return _release_changes(db, run, comment)

    return _gate(
        state,
        gate="release_staging",
        artifact_type="release",
        artifact_id_key="release_id",
        action="Approve deployment of this release candidate to staging.",
        on_approved=approved,
        on_changes=changes,
    )


@node("deploy_staging")
def deploy_staging(state: DeliveryState) -> DeliveryState:
    with stage(state["run_id"], "deploy_staging") as (db, run, _):
        release = _release(db, run)
        dep = db.scalar(
            select(Deployment).where(Deployment.release_id == release.id, Deployment.environment == "staging")
        )
        if dep is None:
            dep = releases.deploy(db, release, "staging")
            run.outcome = {**run.outcome, "staging_deployment_id": str(dep.id)}
        elif dep.status == "running":
            releases.poll_deployment(db, dep)
        if dep.status == "running":
            polls = int(run.counters.get("deploy_polls", 0)) + 1
            run.counters = {**run.counters, "deploy_polls": polls}
            run.status = "waiting_external"
            jobs.enqueue(
                db,
                kind="delivery.resume",
                dedup_key=f"deploy:{run.id}:{polls}",
                run_id=run.id,
                payload={"run_id": str(run.id), "reason": "deployment"},
                delay=timedelta(seconds=get_settings().deploy_poll_s),
            )
            return {"outcome": "wait"}
        if dep.status != "succeeded":
            escalate_now(db, run, "deploy_staging", f"staging deployment {dep.status}: {dep.log[-300:]}")
            return {"halt": True}
    return {"outcome": "ok"}


@node("deploy_wait")
def deploy_wait(state: DeliveryState) -> DeliveryState:
    """Suspend until the scheduled poll resumes the run (separate node: see external_ci_wait)."""
    interrupt({"gate": "deployment"})
    return {"outcome": "ok"}


def _release_changes(db: Session, run: WorkflowRun, comment: str) -> str:
    """Turn the approver's requested change into a defect and send the change back for repair."""
    attempt = int(run.counters.get("attempt", 1))
    limit = int(run.config["limits"]["max_repair_attempts"])
    if attempt - 1 >= limit:
        raise Escalate(f"release changes requested, but the repair limit ({limit}) is reached: {comment}")
    release = _release(db, run)
    release.status = "changes_requested"
    change = db.scalar(select(CodeChange).where(CodeChange.run_id == run.id, CodeChange.attempt == attempt))
    assert change is not None
    project = db.get(Project, run.project_id)
    assert project is not None
    bug = BacklogItem(
        project_id=run.project_id,
        run_id=run.id,
        parent_id=change.backlog_item_id,
        key=next(next_backlog_key(db, project)),
        kind="bug",
        title=f"Release change requested: {comment}"[:300],
        description=comment[:4000],
        priority=1,
        labels=["defect", "release-review"],
        origin="human",
        assigned_agent="developer",
    )
    db.add(bug)
    db.flush()
    db.add(
        Defect(
            project_id=run.project_id,
            run_id=run.id,
            dedup_key=f"release-changes:{release.id}",
            backlog_item_id=bug.id,
            source_item_id=change.backlog_item_id,
            code_change_id=change.id,
            severity="major",
            title=bug.title,
            reproduction_steps=[
                f"Review of release candidate {release.version_label} ({release.commit[:12]})"
            ],
            expected="The change the release approver asked for is made, and everything still passes.",
            actual=comment,
            evidence={"release_id": str(release.id), "source": "release_gate"},
            status="open",
            found_in_attempt=attempt,
        )
    )
    events.emit(
        db,
        type="defect.created",
        project_id=run.project_id,
        run_id=run.id,
        payload={"title": bug.title, "severity": "major", "attempt": attempt, "release_review": True},
    )
    # A new candidate will be built from the repaired change and published again.
    outcome = {k: v for k, v in run.outcome.items() if k not in ("release_id", "external_ci")}
    run.outcome = outcome
    run.counters = {
        **run.counters,
        "attempt": attempt + 1,
        "repairs": int(run.counters.get("repairs", 0)) + 1,
        "external_ci_polls": 0,
    }
    return "changes"


@node("post_deploy_verification")
def post_deploy_verification(state: DeliveryState) -> DeliveryState:
    with stage(state["run_id"], "post_deploy_verification") as (db, run, budget):
        release = _release(db, run)
        dep = db.get(Deployment, uuid.UUID(run.outcome["staging_deployment_id"]))
        assert dep is not None
        if dep.status == "succeeded" and not dep.smoke:
            results = releases.verify_deployment(db, dep, release)
            verdict = (
                SkillRuntime()
                .execute(
                    "deployment-validation",
                    {"results": results},
                    agent_ctx(db, run, "shipping", "post_deploy_verification", budget),
                )
                .output
            )
            dep.health = {**dep.health, "verdict": verdict}
            if verdict["recommendation"] == "rollback":
                releases.rollback(
                    db, dep, "post-deployment checks failed: " + "; ".join(verdict["failed_checks"])
                )
                escalate_now(
                    db,
                    run,
                    "post_deploy_verification",
                    "staging deployment rolled back: " + "; ".join(verdict["failed_checks"]),
                )
                return {"halt": True}
        run.outcome = {
            **run.outcome,
            "production": (
                "eligible for a production promotion request"
                if get_settings().production_deploy_enabled
                else "production deployment is disabled by policy; release stops at staging"
            ),
        }
    return {"outcome": "ok"}
