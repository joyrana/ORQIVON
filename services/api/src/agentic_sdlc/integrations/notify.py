"""Slack and Microsoft Teams notifications for the moments a person is needed.

A project subscribes a declared webhook connection to some event types. When such an event is
recorded, a job is queued in the same transaction (so a rolled-back event never notifies) and the
worker posts the message, retrying on transient failures. Messages carry titles and links only:
no document text, code, or findings detail leaves the workspace through chat.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.integrations.worktools import get_connection
from agentic_sdlc.orm import Project, WorkflowEvent

NOTIFIABLE = {
    "approval.requested": "Approval needed",
    "run.escalated": "Run needs a person",
    "run.failed": "Run failed",
    "run.completed": "Run completed",
    "release.candidate": "Release candidate ready",
    "security.scanned": "Security gate blocked a change",
}
GATES = {
    "architecture": ("architecture", "Review the architecture proposal"),
    "implementation_scope": ("approvals", "Approve the implementation scope"),
    "release_staging": ("releases", "Approve the release to staging"),
    "clarification": ("requirements", "Answer open questions"),
}


def maybe_enqueue(db: Session, ev: WorkflowEvent) -> None:
    """Called by events.emit for every event; cheap unless the type is notifiable."""
    if ev.type not in NOTIFIABLE:
        return
    if ev.type == "security.scanned" and (ev.payload or {}).get("status") != "blocked":
        return
    project = db.get(Project, ev.project_id)
    bindings = ((project.integrations if project else None) or {}).get("notifications") or []
    from agentic_sdlc.workflows import jobs

    for b in bindings:
        if ev.type in (b.get("events") or []):
            jobs.enqueue(
                db,
                kind="integrations.notify",
                dedup_key=f"notify:{ev.event_id}:{b['connection']}",
                payload={"event_id": str(ev.event_id), "connection": b["connection"]},
                max_attempts=5,
            )


def message(ev: WorkflowEvent, project: Project) -> dict[str, str]:
    """Title, text and a link back into the web app. Bounded content only."""
    web = get_settings().web_base_url.rstrip("/")
    base = f"{web}/projects/{project.id}"
    p = ev.payload or {}
    title = f"{NOTIFIABLE[ev.type]}: {project.name}"
    if ev.type == "approval.requested":
        page, action = GATES.get(str(p.get("gate")), ("approvals", f"Decide the {p.get('gate')} gate"))
        roles = ", ".join(str(r).replace("_", " ") for r in p.get("required_roles") or [])
        return {
            "title": title,
            "text": f"{action}. Who can decide: {roles or 'see the request'}.",
            "url": f"{base}/{page}",
        }
    if ev.type == "run.escalated":
        return {
            "title": title,
            "text": f"Stopped at {str(p.get('stage', '')).replace('_', ' ')}: "
            f"{str(p.get('reason', ''))[:300]}",
            "url": f"{base}/activity",
        }
    if ev.type == "release.candidate":
        state = "eligible for approval" if p.get("eligible") else "not eligible (see readiness blockers)"
        return {"title": title, "text": f"Release {p.get('version')} is {state}.", "url": f"{base}/releases"}
    if ev.type == "security.scanned":
        return {
            "title": title,
            "text": f"{p.get('blocking', 0)} finding(s) introduced by attempt {p.get('attempt')} "
            "block the change; the Developer agent is repairing it.",
            "url": f"{base}/quality",
        }
    return {"title": title, "text": NOTIFIABLE[ev.type] + ".", "url": f"{base}/activity"}


def slack_body(m: dict[str, str]) -> dict[str, Any]:
    return {
        "text": f"{m['title']} — {m['text']}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{_slack(m['title'])}*\n{_slack(m['text'])}"},
            },
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Open"}, "url": m["url"]}
                ],
            },
        ],
    }


def _slack(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def teams_body(m: dict[str, str]) -> dict[str, Any]:
    """An Adaptive Card, accepted by Teams Workflows webhooks."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": m["title"], "weight": "Bolder", "wrap": True},
                        {"type": "TextBlock", "text": m["text"], "wrap": True},
                    ],
                    "actions": [{"type": "Action.OpenUrl", "title": "Open", "url": m["url"]}],
                },
            }
        ],
    }


def deliver(db: Session, event_id: str, connection: str) -> None:
    ev = db.scalar(select(WorkflowEvent).where(WorkflowEvent.event_id == uuid.UUID(event_id)))
    if ev is None:
        return
    project = db.get(Project, ev.project_id)
    if project is None:
        return
    conn = get_connection(connection, kinds=("slack", "teams"))
    m = message(ev, project)
    conn.post_webhook(slack_body(m) if conn.kind == "slack" else teams_body(m))
