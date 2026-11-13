"""Usage metering: a monthly report a customer can send to the vendor for licence true-up.

Computed from records the platform keeps anyway (audit trail, workflow runs, token usage,
deployments), so it needs no extra collection and cannot drift from what happened. It contains
counts and totals only: no names, e-mail addresses, project content or prompts. People are counted,
not listed. The report carries a SHA-256 of its canonical JSON so both sides can refer to exactly
the same figures; it is not a signature (the customer produces it), and the vendor can ask for the
audit evidence behind any number.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from agentic_sdlc.core import licensing


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """'2026-11' → [2026-11-01, 2026-12-01) in UTC."""
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("month must be YYYY-MM") from None
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def report(db: Session, month: str) -> dict[str, Any]:
    from agentic_sdlc.orm import AuditEvent, Deployment, Project, TokenUsage, User, WorkflowRun

    start, end = month_bounds(month)

    def between(col: Any) -> Any:
        return (col >= start) & (col < end)

    service_ids = select(User.id).where(User.is_service_account.is_(True))
    active_people = db.scalar(
        select(func.count(distinct(AuditEvent.actor_id))).where(
            between(AuditEvent.ts),
            AuditEvent.actor_type == "human",
            AuditEvent.actor_id.is_not(None),
            AuditEvent.actor_id.not_in(service_ids),
        )
    )
    runs = dict(
        db.execute(
            select(WorkflowRun.status, func.count())
            .where(between(WorkflowRun.created_at), WorkflowRun.definition == "delivery")
            .group_by(WorkflowRun.status)
        ).all()
    )
    projects_with_runs = db.scalar(
        select(func.count(distinct(WorkflowRun.project_id))).where(between(WorkflowRun.created_at))
    )
    models = [
        {
            "model_id": m,
            "input_tokens": int(i or 0),
            "output_tokens": int(o or 0),
            "cost_usd": round(float(c or 0), 4),
            "estimated": bool(e),
        }
        for m, e, i, o, c in db.execute(
            select(
                TokenUsage.model_id,
                TokenUsage.is_estimated,
                func.sum(TokenUsage.input_tokens),
                func.sum(TokenUsage.output_tokens),
                func.sum(TokenUsage.cost_usd),
            )
            .where(between(TokenUsage.created_at))
            .group_by(TokenUsage.model_id, TokenUsage.is_estimated)
            .order_by(TokenUsage.model_id, TokenUsage.is_estimated)
        ).all()
    ]
    deployments = dict(
        db.execute(
            select(Deployment.environment, func.count())
            .where(between(Deployment.created_at), Deployment.status == "succeeded")
            .group_by(Deployment.environment)
        ).all()
    )
    st = licensing.status(db)
    body: dict[str, Any] = {
        "report": "agentic-sdlc-usage",
        "version": 1,
        "month": month,
        "licence_id": st.licence.licence_id if st.licence else None,
        "licence_state": st.state,
        "seats_licensed": st.licence.seats if st.licence else None,
        "people": {
            "active_accounts_now": licensing.seats_used(db),
            "people_active_in_month": int(active_people or 0),
        },
        "projects": {
            "total": int(db.scalar(select(func.count()).select_from(Project)) or 0),
            "with_delivery_runs_in_month": int(projects_with_runs or 0),
        },
        "delivery_runs": {
            "started": sum(int(n) for n in runs.values()),
            "by_status": {k: int(v) for k, v in sorted(runs.items())},
        },
        "deployments_succeeded": {k: int(v) for k, v in sorted(deployments.items())},
        "model_usage": models,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {**body, "sha256": hashlib.sha256(canonical).hexdigest()}


def verify(doc: dict[str, Any]) -> bool:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest() == doc.get("sha256")
