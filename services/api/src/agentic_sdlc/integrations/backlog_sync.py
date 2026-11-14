"""Export the generated backlog to Jira or Azure Boards, and keep it updated.

One-way and idempotent: each backlog item maps to one external issue (stored as an ExternalLink).
A sync creates issues for new items, updates issues whose content changed (title, description,
acceptance criteria, labels, points), and leaves unchanged ones alone. Parents are created
before children so epics and stories keep their hierarchy. Workflow status in the tool belongs
to the team and is never overwritten.
"""

from __future__ import annotations

import hashlib
import html
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.core.db import utcnow
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.integrations.worktools import IntegrationError, WorkToolConnection, get_connection
from agentic_sdlc.orm import (
    AcceptanceCriterion,
    BacklogItem,
    ExternalLink,
    Project,
    Requirement,
    backlog_item_criteria,
    backlog_item_requirements,
)

JIRA_TYPES = {
    "epic": "Epic",
    "feature": "Epic",
    "story": "Story",
    "task": "Task",
    "bug": "Bug",
    "spike": "Task",
}
# Jira's standard hierarchy: epics (1) above stories, tasks and bugs (0). A parent is set only one level
# up; deeper workspace levels (an epic above feature-epics) are recorded in the description instead.
# Jira Premium hierarchies can be described per project with `hierarchy_levels`.
JIRA_LEVELS = {"Epic": 1}
ADO_TYPES = {
    "epic": "Epic",
    "feature": "Feature",
    "story": "User Story",
    "task": "Task",
    "bug": "Bug",
    "spike": "Task",
}


@dataclass
class ItemContent:
    item: BacklogItem
    title: str
    description: str
    criteria: list[tuple[str, str]]
    requirements: list[str]
    labels: list[str]
    points: float | None
    link: str
    parent_note: str = ""

    def digest(self, issue_type: str, parent: str | None) -> str:
        data = [
            self.title,
            self.description,
            self.criteria,
            self.requirements,
            self.labels,
            self.points,
            issue_type,
            parent,
        ]
        return hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()


@dataclass
class SyncReport:
    connection: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # parent not exported yet
    failed: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deferred": self.deferred,
            "failed": self.failed,
        }


def _content(db: Session, project: Project, item: BacklogItem) -> ItemContent:
    criteria = db.execute(
        select(AcceptanceCriterion.key, AcceptanceCriterion.text)
        .join(
            backlog_item_criteria, backlog_item_criteria.c.acceptance_criterion_id == AcceptanceCriterion.id
        )
        .where(backlog_item_criteria.c.backlog_item_id == item.id)
        .order_by(AcceptanceCriterion.key)
    ).all()
    reqs = db.scalars(
        select(Requirement.key)
        .join(backlog_item_requirements, backlog_item_requirements.c.requirement_id == Requirement.id)
        .where(backlog_item_requirements.c.backlog_item_id == item.id)
        .order_by(Requirement.key)
    ).all()
    web = get_settings().web_base_url.rstrip("/")
    return ItemContent(
        item=item,
        title=f"{item.title} [{item.key}]"[:250],
        description=item.description or "",
        criteria=[(k, t) for k, t in criteria],
        requirements=list(reqs),
        labels=sorted({"asw", *[str(x).replace(" ", "-") for x in item.labels or []]}),
        points=item.story_points,
        link=f"{web}/projects/{project.id}/backlog?item={item.key}",
    )


# --- Jira ----------------------------------------------------------------------------------------


def _adf(c: ItemContent) -> dict[str, Any]:
    def para(text: str) -> dict[str, Any]:
        return (
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            if text
            else {"type": "paragraph"}
        )

    content: list[dict[str, Any]] = [para(line) for line in c.description.split("\n") if line.strip()]
    if c.parent_note:
        content.append(para(c.parent_note))
    if c.criteria:
        content.append(
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": "Acceptance criteria"}],
            }
        )
        content.append(
            {
                "type": "bulletList",
                "content": [{"type": "listItem", "content": [para(f"{k}: {t}")]} for k, t in c.criteria],
            }
        )
    if c.requirements:
        content.append(para("Requirements: " + ", ".join(c.requirements)))
    content.append(
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"Managed by ORQIVON ({c.item.key}). "},
                {
                    "type": "text",
                    "text": "Open in workspace",
                    "marks": [{"type": "link", "attrs": {"href": c.link}}],
                },
            ],
        }
    )
    return {"type": "doc", "version": 1, "content": content}


def _plain(c: ItemContent) -> str:
    parts = [c.description, c.parent_note]
    if c.criteria:
        parts.append("Acceptance criteria:\n" + "\n".join(f"* {k}: {t}" for k, t in c.criteria))
    if c.requirements:
        parts.append("Requirements: " + ", ".join(c.requirements))
    parts.append(f"Managed by ORQIVON ({c.item.key}): {c.link}")
    return "\n\n".join(p for p in parts if p)


def _jira_push(
    conn: WorkToolConnection,
    binding: dict[str, Any],
    c: ItemContent,
    link: ExternalLink | None,
    parent: ExternalLink | None,
) -> tuple[str, str, str]:
    types = {**JIRA_TYPES, **(binding.get("issue_types") or {})}
    fields: dict[str, Any] = {
        "summary": c.title,
        "description": _adf(c) if conn.api_version == 3 else _plain(c),
        "labels": c.labels,
    }
    points_field = binding.get("story_points_field")
    if points_field and c.points is not None:
        fields[points_field] = c.points
    api = f"/rest/api/{conn.api_version}"
    levels = {**JIRA_LEVELS, **(binding.get("hierarchy_levels") or {})}
    if link is None:
        fields["project"] = {"key": binding["project_key"]}
        fields["issuetype"] = {"name": types.get(c.item.kind, "Task")}
        if parent is not None and conn.api_version == 3:
            mine = levels.get(fields["issuetype"]["name"], 0)
            theirs = levels.get(str(parent.state.get("type")), 0)
            if theirs == mine + 1:
                fields["parent"] = {"key": parent.external_key}
        out = conn.request("POST", f"{api}/issue", json={"fields": fields})
        key = out["key"]
        if parent is not None and conn.api_version == 2:
            conn.request(
                "POST",
                f"{api}/issueLink",
                json={
                    "type": {"name": binding.get("parent_link_type", "Relates")},
                    "inwardIssue": {"key": key},
                    "outwardIssue": {"key": parent.external_key},
                },
            )
        return str(out["id"]), key, f"{conn.base_url}/browse/{key}"
    fields.pop("project", None)
    conn.request("PUT", f"{api}/issue/{link.external_key}", json={"fields": fields})
    return link.external_id, link.external_key or "", link.url or ""


# --- Azure Boards -------------------------------------------------------------------------------


def _ado_html(c: ItemContent) -> tuple[str, str]:
    desc = "".join(f"<p>{html.escape(line)}</p>" for line in c.description.split("\n") if line.strip())
    if c.requirements:
        desc += f"<p>Requirements: {html.escape(', '.join(c.requirements))}</p>"
    desc += (
        f"<p>Managed by ORQIVON ({html.escape(c.item.key)}). "
        f'<a href="{html.escape(c.link)}">Open in workspace</a></p>'
    )
    ac = "<ul>" + "".join(f"<li>{html.escape(k)}: {html.escape(t)}</li>" for k, t in c.criteria) + "</ul>"
    return desc, ac if c.criteria else ""


def _ado_push(
    conn: WorkToolConnection,
    binding: dict[str, Any],
    c: ItemContent,
    link: ExternalLink | None,
    parent: ExternalLink | None,
) -> tuple[str, str, str]:
    types = {**ADO_TYPES, **(binding.get("work_item_types") or {})}
    desc, ac = _ado_html(c)
    op = "add" if link is None else "replace"
    ops: list[dict[str, Any]] = [
        {"op": op, "path": "/fields/System.Title", "value": c.title},
        {"op": op, "path": "/fields/System.Description", "value": desc},
        {"op": op, "path": "/fields/System.Tags", "value": "; ".join(c.labels)},
        {"op": op, "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": ac},
    ]
    if c.points is not None and c.item.kind == "story":
        ops.append({"op": op, "path": "/fields/Microsoft.VSTS.Scheduling.StoryPoints", "value": c.points})
    if binding.get("area_path"):
        ops.append({"op": op, "path": "/fields/System.AreaPath", "value": binding["area_path"]})
    headers = {"Content-Type": "application/json-patch+json"}
    project = binding["project"]
    if link is None:
        if parent is not None:
            ops.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
                        "url": f"{conn.base_url}/_apis/wit/workItems/{parent.external_id}",
                    },
                }
            )
        wtype = types.get(c.item.kind, "Task")
        out = conn.request(
            "POST",
            f"/{project}/_apis/wit/workitems/${wtype}?api-version=7.1",
            content=json.dumps(ops),
            headers=headers,
        )
    else:
        out = conn.request(
            "PATCH",
            f"/_apis/wit/workitems/{link.external_id}?api-version=7.1",
            content=json.dumps(ops),
            headers=headers,
        )
    wid = str(out["id"])
    url = ((out.get("_links") or {}).get("html") or {}).get(
        "href"
    ) or f"{conn.base_url}/{project}/_workitems/edit/{wid}"
    return wid, wid, url


# --- driver -------------------------------------------------------------------------------------


def _depth(item: BacklogItem, by_id: dict[uuid.UUID, BacklogItem]) -> int:
    d, cur = 0, item
    while cur.parent_id and cur.parent_id in by_id and d < 20:
        cur, d = by_id[cur.parent_id], d + 1
    return d


def sync_backlog(db: Session, project: Project, *, item_ids: list[uuid.UUID] | None = None) -> SyncReport:
    binding = (project.integrations or {}).get("backlog") or {}
    conn = get_connection(binding.get("connection"), kinds=("jira", "azure_boards"))
    push = _jira_push if conn.kind == "jira" else _ado_push
    report = SyncReport(connection=conn.name)
    types_for = {
        **(JIRA_TYPES if conn.kind == "jira" else ADO_TYPES),
        **(binding.get("issue_types") or binding.get("work_item_types") or {}),
    }
    stmt = select(BacklogItem).where(BacklogItem.project_id == project.id)
    items = list(db.scalars(stmt))
    by_id = {i.id: i for i in items}
    wanted = [i for i in items if item_ids is None or i.id in item_ids]
    links = {
        link.entity_id: link
        for link in db.scalars(
            select(ExternalLink).where(
                ExternalLink.project_id == project.id,
                ExternalLink.connection == conn.name,
                ExternalLink.entity_type == "backlog_item",
            )
        )
    }
    for item in sorted(wanted, key=lambda i: (_depth(i, by_id), i.key)):
        parent = links.get(item.parent_id) if item.parent_id else None
        if item.parent_id in by_id and parent is None:
            report.deferred.append(item.key)  # never create a child detached from its parent
            continue
        c = _content(db, project, item)
        if item.parent_id in by_id:
            p = by_id[item.parent_id]
            c.parent_note = f"Part of {p.key}: {p.title}" + (f" ({parent.external_key})" if parent else "")
        issue_type = types_for.get(item.kind, "Task")
        digest = c.digest(issue_type, parent.external_id if parent else None)
        link = links.get(item.id)
        if link is not None and link.content_hash == digest:
            report.unchanged.append(item.key)
            continue
        try:
            try:
                ext_id, ext_key, url = push(conn, binding, c, link, parent)
            except IntegrationError as first:
                # Retry only "rate limited": the request was not processed, so a retry cannot create
                # a duplicate. Other failures are reported; the next sync picks the item up.
                if first.status != 429:
                    raise
                time.sleep(1.0)
                ext_id, ext_key, url = push(conn, binding, c, link, parent)
        except IntegrationError as exc:
            report.failed[item.key] = str(exc)
            if link is not None:
                link.state = {**link.state, "error": str(exc)[:500]}
            if exc.status in (401, 403) or "credentials" in str(exc):
                break  # the same failure would repeat for every item
            continue
        if link is None:
            link = ExternalLink(
                project_id=project.id,
                connection=conn.name,
                entity_type="backlog_item",
                entity_id=item.id,
                external_id=ext_id,
                external_key=ext_key,
                url=url,
            )
            db.add(link)
            links[item.id] = link
            report.created.append(item.key)
        else:
            report.updated.append(item.key)
        link.content_hash = digest
        link.synced_at = utcnow()
        link.state = {"type": issue_type}
        db.flush()
    return report
