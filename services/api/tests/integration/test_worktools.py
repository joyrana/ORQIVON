"""Work-tool integrations over real HTTP: backlog export to Jira and Azure Boards, Confluence
import, Slack and Teams notifications, and the connection safety rules."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentic_sdlc.core.db import session_scope
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.events import service as events
from agentic_sdlc.integrations import worktools
from agentic_sdlc.workflows.worker import drain
from tests.conftest import H
from tests.integration.test_documents_requirements import upload_sample_brd
from tests.worktools_server import FakeWorkTools, serve_worktools


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorkTools]:
    with serve_worktools() as fake:
        monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
        monkeypatch.setenv("ADO_PAT", "ado-pat")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{fake.url}/hooks/slack")
        monkeypatch.setenv("TEAMS_WEBHOOK_URL", f"{fake.url}/hooks/teams")
        jira = {
            "kind": "jira",
            "base_url": fake.url,
            "auth": "basic",
            "username_env": "JIRA_EMAIL",
            "token_env": "JIRA_API_TOKEN",
        }
        conns = [
            {"name": "jira", **jira},
            {"name": "jira-dc", **jira, "api_version": 2},
            {
                "name": "boards",
                "kind": "azure_boards",
                "base_url": fake.url,
                "auth": "pat",
                "token_env": "ADO_PAT",
            },
            {"name": "wiki", **jira, "kind": "confluence", "base_url": f"{fake.url}/wiki"},
            {"name": "eng-slack", "kind": "slack", "webhook_url_env": "SLACK_WEBHOOK_URL"},
            {"name": "eng-teams", "kind": "teams", "webhook_url_env": "TEAMS_WEBHOOK_URL"},
        ]
        s = get_settings()
        monkeypatch.setattr(s, "integrations_insecure_http_hosts", ["127.0.0.1"])
        monkeypatch.setattr(s, "integrations", json.dumps(conns))
        worktools.load_connections.cache_clear()
        yield fake
        worktools.load_connections.cache_clear()


def _backlog(client: TestClient, pid: str) -> list[dict[str, Any]]:
    upload_sample_brd(client, pid)
    assert client.post(f"/api/v1/projects/{pid}/backlog/generate", headers=H("engineer")).status_code == 202
    drain()
    return client.get(f"/api/v1/projects/{pid}/backlog", headers=H("owner")).json()["items"]


def _bind(client: TestClient, pid: str, **config: Any) -> Any:
    return client.put(f"/api/v1/projects/{pid}/integrations", json=config, headers=H("owner"))


def _sync(client: TestClient, pid: str) -> dict[str, Any]:
    r = client.post(f"/api/v1/projects/{pid}/backlog/sync", headers=H("engineer"))
    assert r.status_code == 202, r.text
    drain()
    return client.get(f"/api/v1/projects/{pid}/integrations", headers=H("owner")).json()["last_backlog_sync"]


def test_backlog_exports_to_jira_with_hierarchy_and_updates_idempotently(
    client: TestClient, project: dict, tools: FakeWorkTools
) -> None:
    pid = project["id"]
    items = _backlog(client, pid)
    assert _bind(client, pid, backlog={"connection": "jira", "project_key": "PAY"}).status_code == 200
    first = _sync(client, pid)
    assert (first["created"], first["updated"], first["failed"]) == (len(items), 0, 0)
    by_summary = {i["fields"]["summary"]: (k, i) for k, i in tools.issues.items()}
    # Workspace epic → feature → story; Jira has epics above stories, so features become epics
    # whose stories hang below them, and the top epic is named in their description.
    top = next(i for i in items if i["kind"] == "epic")
    feature = next(i for i in items if i["kind"] == "feature")
    story = next(i for i in items if i["kind"] == "story" and i["parent_id"] == feature["id"])
    _, top_issue = by_summary[f"{top['title']} [{top['key']}]"]
    feature_key, feature_issue = by_summary[f"{feature['title']} [{feature['key']}]"]
    _, story_issue = by_summary[f"{story['title']} [{story['key']}]"]
    assert top_issue["fields"]["issuetype"] == {"name": "Epic"} and "parent" not in top_issue["fields"]
    assert (
        feature_issue["fields"]["issuetype"] == {"name": "Epic"} and "parent" not in feature_issue["fields"]
    )
    assert f"Part of {top['key']}" in json.dumps(feature_issue["fields"]["description"])
    assert story_issue["fields"]["parent"] == {"key": feature_key}
    adf = json.dumps(story_issue["fields"]["description"])
    assert "Acceptance criteria" in adf and "-AC1" in adf and f"/projects/{pid}/backlog" in adf
    assert "asw" in story_issue["fields"]["labels"]

    links = client.get(f"/api/v1/projects/{pid}/backlog/external-links", headers=H("qa")).json()
    assert len(links) == len(items) and all(x["url"].startswith(f"{tools.url}/browse/PAY-") for x in links)

    # Nothing changed: nothing is sent.
    calls = len(tools.requests)
    again = _sync(client, pid)
    assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, len(items))
    assert len(tools.requests) == calls

    # A change in the workspace updates the same issue; nothing is duplicated.
    item = client.get(f"/api/v1/projects/{pid}/backlog", headers=H("owner")).json()["items"][-1]
    r = client.patch(
        f"/api/v1/projects/{pid}/backlog/items/{item['id']}",
        json={"title": "Reworded story title", "expected_version": item["version"]},
        headers=H("engineer"),
    )
    assert r.status_code == 200, r.text
    third = _sync(client, pid)
    assert (third["created"], third["updated"]) == (0, 1)
    assert sum(1 for i in tools.issues.values() if i["updates"]) == 1
    assert len(tools.issues) == len(items)
    audit = client.get(f"/api/v1/projects/{pid}/audit?limit=200", headers=H("owner")).json()["items"]
    assert sum(1 for a in audit if a["action"] == "backlog.synced") == 3


def test_jira_data_center_gets_text_descriptions_and_parent_links(
    client: TestClient, project: dict, tools: FakeWorkTools
) -> None:
    pid = project["id"]
    items = _backlog(client, pid)
    _bind(client, pid, backlog={"connection": "jira-dc", "project_key": "PAY", "parent_link_type": "Relates"})
    assert _sync(client, pid)["created"] == len(items)
    assert all(isinstance(i["fields"]["description"], str) for i in tools.issues.values())
    assert len(tools.links) == sum(1 for i in items if i["parent_id"])
    assert all(link["type"] == {"name": "Relates"} for link in tools.links)


def test_backlog_exports_to_azure_boards_and_recovers_from_a_failure(
    client: TestClient, project: dict, tools: FakeWorkTools
) -> None:
    pid = project["id"]
    items = _backlog(client, pid)
    _bind(client, pid, backlog={"connection": "boards", "project": "Payments", "area_path": "Payments\\Core"})
    tools.fail_next["ado_create"] = 1  # the service is briefly unavailable when the epic is created
    first = _sync(client, pid)
    # Children wait for their parent rather than being created detached from it.
    assert (first["created"], first["failed"], first["deferred"]) == (0, 1, len(items) - 1)
    assert "HTTP 503" in next(iter(first["failed_items"].values()))
    second = _sync(client, pid)
    assert second["created"] == len(items) and second["failed"] == 0
    assert sorted({w["type"] for w in tools.work_items.values()}) == ["Epic", "Feature", "User Story"]
    assert all(w["relations"] for w in tools.work_items.values() if w["type"] != "Epic")
    story = next(w for w in tools.work_items.values() if w["type"] == "User Story")
    assert "-AC1" in story["fields"]["Microsoft.VSTS.Common.AcceptanceCriteria"]
    assert story["fields"]["System.AreaPath"] == "Payments\\Core"
    assert story["relations"][0]["rel"] == "System.LinkTypes.Hierarchy-Reverse"
    assert all(w["project"] == "Payments" for w in tools.work_items.values())


def test_missing_credentials_fail_cleanly(
    client: TestClient, project: dict, tools: FakeWorkTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = project["id"]
    _backlog(client, pid)
    _bind(client, pid, backlog={"connection": "jira", "project_key": "PAY"})
    monkeypatch.delenv("JIRA_API_TOKEN")
    report = _sync(client, pid)
    assert report["created"] == 0 and report["failed"] == 1  # stops at the first item, not one error per item
    assert "credentials are not configured" in next(iter(report["failed_items"].values()))
    conns = client.get(f"/api/v1/projects/{pid}/integrations", headers=H("owner")).json()["connections"]
    jira = next(c for c in conns if c["name"] == "jira")
    assert jira["credential_available"] is False and "token_env" not in jira


def test_integration_config_is_validated_and_owner_only(
    client: TestClient, project: dict, tools: FakeWorkTools
) -> None:
    pid = project["id"]
    assert _bind(client, pid, backlog={"connection": "nope", "project_key": "PAY"}).status_code == 422
    assert _bind(client, pid, backlog={"connection": "jira"}).status_code == 422  # no project_key
    assert _bind(client, pid, backlog={"connection": "eng-slack", "project_key": "PAY"}).status_code == 422
    bad_events = {"notifications": [{"connection": "eng-slack", "events": ["everything"]}]}
    assert _bind(client, pid, **bad_events).status_code == 422
    ok = {"notifications": [{"connection": "eng-slack", "events": ["approval.requested"]}]}
    assert (
        client.put(f"/api/v1/projects/{pid}/integrations", json=ok, headers=H("engineer")).status_code == 403
    )
    assert _bind(client, pid, **ok).status_code == 200
    assert client.post(f"/api/v1/projects/{pid}/backlog/sync", headers=H("engineer")).status_code == 422


def test_notifications_reach_slack_and_teams_with_links_only(
    client: TestClient, project: dict, tools: FakeWorkTools
) -> None:
    pid = project["id"]
    _bind(
        client,
        pid,
        notifications=[
            {"connection": "eng-slack", "events": ["approval.requested", "run.escalated"]},
            {"connection": "eng-teams", "events": ["release.candidate"]},
        ],
    )
    import uuid

    with session_scope() as db:
        events.emit(
            db,
            type="approval.requested",
            project_id=uuid.UUID(pid),
            payload={"request_id": "r1", "gate": "release_staging", "required_roles": ["release_approver"]},
        )
        events.emit(
            db,
            type="release.candidate",
            project_id=uuid.UUID(pid),
            payload={"version": "1.4.0", "eligible": True},
        )
        events.emit(db, type="run.completed", project_id=uuid.UUID(pid), payload={})  # not subscribed
    drain()
    assert len(tools.webhooks["slack"]) == 1 and len(tools.webhooks["teams"]) == 1
    slack = tools.webhooks["slack"][0]
    assert "Approval needed" in slack["text"] and "release approver" in slack["text"]
    button = slack["blocks"][1]["elements"][0]
    assert button["url"].endswith(f"/projects/{pid}/releases")
    card = tools.webhooks["teams"][0]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and "1.4.0" in json.dumps(card)

    r = client.post(
        f"/api/v1/projects/{pid}/integrations/test-notification",
        json={"connection": "eng-teams"},
        headers=H("owner"),
    )
    assert r.status_code == 200 and len(tools.webhooks["teams"]) == 2


CONFLUENCE_BODY = """
<h1>Refund rules</h1>
<p>This page describes refunds for the payments service.</p>
<h2>3 Functional requirements</h2>
<table><tbody>
<tr><th>ID</th><th>Requirement</th></tr>
<tr><td>FR-001</td><td><p>The system shall refund a payment within 30 days of capture.</p></td></tr>
</tbody></table>
<p>FR-002: The system shall record every refund in the ledger.</p>
<ul><li>Partial refunds are allowed.</li>
<li>Refunds above <strong>10,000 USD</strong> need approval.</li></ul>
<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Note</ac:parameter>
<ac:rich-text-body><p>NFR-001: Refunds must complete within two seconds.</p></ac:rich-text-body>
</ac:structured-macro>
"""


def test_confluence_page_imports_as_a_versioned_source_document(
    client: TestClient, project: dict, tools: FakeWorkTools
) -> None:
    pid = project["id"]
    tools.pages["4242"] = {"title": "Refunds BRD", "version": 7, "body": CONFLUENCE_BODY}
    url = f"/api/v1/projects/{pid}/documents/import"
    body = {"connection": "wiki", "page_id": "4242", "kind": "brd"}
    assert client.post(url, json=body, headers=H("qa")).status_code == 403
    r = client.post(url, json=body, headers=H("engineer"))
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "created" and r.json()["source_url"].endswith("/spaces/ENG/pages/4242")
    drain()
    doc = client.get(f"/api/v1/projects/{pid}/documents/{r.json()['document_id']}", headers=H("owner")).json()
    assert doc["title"] == "Refunds BRD" and doc["versions"][0]["status"] == "processed"
    keys = sorted(doc["versions"][0]["extraction_summary"]["created"])
    assert keys == ["FR-001", "FR-002", "NFR-001"]
    blocks = client.get(
        f"/api/v1/projects/{pid}/documents/{r.json()['document_id']}/versions/1/blocks", headers=H("owner")
    ).json()
    texts = [b["text"] for b in blocks]
    assert "Refunds above 10,000 USD need approval." in texts
    assert not any(t == "Note" for t in texts)  # macro parameters are not content

    assert client.post(url, json=body, headers=H("engineer")).json()["status"] == "unchanged"
    tools.pages["4242"] = {
        **tools.pages["4242"],
        "version": 8,
        "body": CONFLUENCE_BODY.replace("30 days", "60 days"),
    }
    again = client.post(url, json=body, headers=H("engineer")).json()
    assert (again["status"], again["version"], again["document_id"]) == (
        "updated",
        2,
        r.json()["document_id"],
    )
    missing = client.post(url, json={**body, "page_id": "999"}, headers=H("engineer"))
    assert missing.status_code == 422 and "HTTP 404" in missing.text


def test_connection_safety_rules(tools: FakeWorkTools, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="https"):
        worktools.WorkToolConnection(name="j", kind="jira", base_url="http://jira.internal", auth="none")
    with pytest.raises(ValidationError, match="username_env"):
        worktools.WorkToolConnection(
            name="j", kind="jira", base_url="https://x.atlassian.net", auth="basic", token_env="T"
        )
    conn = worktools.get_connection("jira", kinds=("jira",))
    with pytest.raises(worktools.IntegrationError, match="another host"):
        conn.request("GET", "https://evil.example.com/steal")
    with pytest.raises(worktools.IntegrationError) as exc:
        conn.request("GET", "/rest/api/3/nothing-here")
    assert exc.value.status in (404, 405) and "jira-token" not in str(exc.value)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://hooks.example.com/x")
    with pytest.raises(worktools.IntegrationError, match="https"):
        worktools.get_connection("eng-slack", kinds=("slack",)).post_webhook({"text": "x"})
