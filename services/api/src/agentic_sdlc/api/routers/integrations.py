"""Work-tool integrations: per-project bindings, backlog export, Confluence import, notifications."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from agentic_sdlc.api.deps import DB, CurrentPrincipal, project_permission
from agentic_sdlc.audit import service as audit
from agentic_sdlc.core.errors import Conflict, ValidationFailed
from agentic_sdlc.documents import service as documents
from agentic_sdlc.integrations import confluence
from agentic_sdlc.integrations.notify import NOTIFIABLE, message, slack_body, teams_body
from agentic_sdlc.integrations.worktools import IntegrationError, get_connection, load_connections
from agentic_sdlc.orm import BacklogItem, ExternalLink, Project, SourceDocument, WorkflowEvent
from agentic_sdlc.workflows import jobs

router = APIRouter(tags=["integrations"])
Read = Annotated[Project, Depends(project_permission("project.read"))]


class BacklogBinding(BaseModel):
    connection: str
    project_key: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,19}$")  # Jira
    project: str | None = Field(default=None, min_length=1, max_length=128)  # Azure DevOps project
    area_path: str | None = Field(default=None, max_length=256)
    issue_types: dict[str, str] = Field(default_factory=dict)
    work_item_types: dict[str, str] = Field(default_factory=dict)
    story_points_field: str | None = Field(default=None, pattern=r"^customfield_\d{1,10}$")
    parent_link_type: str | None = Field(default=None, max_length=60)


class NotificationBinding(BaseModel):
    connection: str
    events: list[str] = Field(min_length=1)


class IntegrationsConfig(BaseModel):
    backlog: BacklogBinding | None = None
    notifications: list[NotificationBinding] = Field(default_factory=list, max_length=10)


class ImportIn(BaseModel):
    connection: str
    page_id: str = Field(pattern=r"^\d{1,20}$")
    kind: Literal["brd", "spec", "transcript"] = "brd"


class TestIn(BaseModel):
    connection: str


@router.get("/projects/{project_id}/integrations")
def get_integrations(project: Read, db: DB) -> dict[str, Any]:
    last = db.scalar(
        select(WorkflowEvent)
        .where(WorkflowEvent.project_id == project.id, WorkflowEvent.type == "backlog.synced")
        .order_by(WorkflowEvent.seq.desc())
        .limit(1)
    )
    return {
        "config": IntegrationsConfig.model_validate(project.integrations or {}).model_dump(exclude_none=True),
        "connections": [c.public() for c in load_connections().values()],
        "notifiable_events": sorted(NOTIFIABLE),
        "last_backlog_sync": {**last.payload, "at": last.ts.isoformat()} if last else None,
    }


@router.put("/projects/{project_id}/integrations")
def put_integrations(
    body: IntegrationsConfig,
    db: DB,
    principal: CurrentPrincipal,
    project: Annotated[Project, Depends(project_permission("project.update"))],
) -> dict[str, Any]:
    if body.backlog:
        conn = get_connection(body.backlog.connection, kinds=("jira", "azure_boards"))
        if conn.kind == "jira" and not body.backlog.project_key:
            raise ValidationFailed("a Jira backlog binding needs project_key")
        if conn.kind == "azure_boards" and not body.backlog.project:
            raise ValidationFailed("an Azure Boards backlog binding needs project")
    for n in body.notifications:
        get_connection(n.connection, kinds=("slack", "teams"))
        unknown = sorted(set(n.events) - set(NOTIFIABLE))
        if unknown:
            raise ValidationFailed(
                "unknown event types", details={"unknown": unknown, "allowed": sorted(NOTIFIABLE)}
            )
    project.integrations = body.model_dump(exclude_none=True)
    audit.record(
        db,
        action="project.integrations.updated",
        actor_id=principal.user_id,
        actor_type=principal.actor_type,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        details=project.integrations,
    )
    db.commit()
    return project.integrations


@router.post("/projects/{project_id}/backlog/sync", status_code=202)
def sync_backlog(
    db: DB,
    principal: CurrentPrincipal,
    project: Annotated[Project, Depends(project_permission("backlog.edit"))],
) -> dict[str, Any]:
    binding = (project.integrations or {}).get("backlog")
    if not binding:
        raise ValidationFailed("no backlog integration is configured for this project")
    get_connection(binding["connection"], kinds=("jira", "azure_boards"))
    job = jobs.enqueue(
        db,
        kind="backlog.sync",
        dedup_key=f"backlog.sync:{project.id}:{uuid.uuid4()}",
        payload={"project_id": str(project.id), "requested_by": str(principal.user_id)},
    )
    db.commit()
    return {"job_id": str(job.id), "connection": binding["connection"]}


@router.get("/projects/{project_id}/backlog/external-links")
def external_links(project: Read, db: DB) -> list[dict[str, Any]]:
    rows = db.execute(
        select(ExternalLink, BacklogItem.key)
        .join(BacklogItem, BacklogItem.id == ExternalLink.entity_id)
        .where(ExternalLink.project_id == project.id, ExternalLink.entity_type == "backlog_item")
        .order_by(BacklogItem.key)
    ).all()
    return [
        {
            "item_key": key,
            "connection": link.connection,
            "external_key": link.external_key,
            "url": link.url,
            "synced_at": link.synced_at.isoformat() if link.synced_at else None,
            "error": link.state.get("error"),
        }
        for link, key in rows
    ]


@router.post("/projects/{project_id}/documents/import", status_code=202)
def import_confluence(
    body: ImportIn,
    db: DB,
    principal: CurrentPrincipal,
    project: Annotated[Project, Depends(project_permission("document.upload"))],
) -> dict[str, Any]:
    conn = get_connection(body.connection, kinds=("confluence",))
    try:
        page = confluence.fetch_page(conn, body.page_id)
    except IntegrationError as exc:
        raise ValidationFailed(f"could not read the Confluence page: {exc}") from None
    link = db.scalar(
        select(ExternalLink).where(
            ExternalLink.project_id == project.id,
            ExternalLink.connection == conn.name,
            ExternalLink.entity_type == "source_document",
            ExternalLink.external_id == page["id"],
        )
    )
    existing = db.get(SourceDocument, link.entity_id) if link is not None else None
    doc_id = existing.id if existing is not None else None
    filename = f"{page['title'][:120].replace('/', '-')}.md"
    try:
        doc, version = documents.upload(
            db,
            principal,
            project,
            filename=filename,
            data=page["text"].encode(),
            kind=body.kind,
            title=page["title"],
            document_id=doc_id,
        )
    except ValidationFailed as exc:
        if "identical" in str(exc):
            return {"status": "unchanged", "document_id": str(doc_id), "page_version": page["version"]}
        raise
    if link is None:
        link = ExternalLink(
            project_id=project.id,
            connection=conn.name,
            entity_type="source_document",
            entity_id=doc.id,
            external_id=page["id"],
            external_key=page["title"][:200],
        )
        db.add(link)
    link.entity_id = doc.id
    link.url = page["url"]
    link.state = {"page_version": page["version"]}
    from agentic_sdlc.core.db import utcnow

    link.synced_at = utcnow()
    db.commit()
    return {
        "status": "created" if version.version == 1 else "updated",
        "document_id": str(doc.id),
        "version": version.version,
        "page_version": page["version"],
        "source_url": page["url"],
    }


@router.post("/projects/{project_id}/integrations/test-notification")
def test_notification(
    body: TestIn,
    project: Annotated[Project, Depends(project_permission("project.update"))],
) -> dict[str, str]:
    conn = get_connection(body.connection, kinds=("slack", "teams"))
    ev = WorkflowEvent(type="run.completed", project_id=project.id, payload={})
    m = message(ev, project)
    m["title"] = f"Test from Agentic SDLC Workspace: {project.name}"
    m["text"] = "Notifications for this project will arrive here."
    try:
        conn.post_webhook(slack_body(m) if conn.kind == "slack" else teams_body(m))
    except IntegrationError as exc:
        raise Conflict(f"the webhook did not accept the message: {exc}") from None
    return {"status": "sent"}
