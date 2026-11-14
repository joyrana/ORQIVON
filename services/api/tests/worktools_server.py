"""A fake Jira (Cloud v3 and Data Center v2), Azure Boards, Confluence, Slack and Teams for tests.

It checks what the real services check that matters to us (authentication, required fields,
content types, parents that exist) and records every request, so tests can assert on payloads.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.model_server import _free_port

JIRA_AUTH = "Basic " + base64.b64encode(b"bot@example.com:jira-token").decode()
ADO_AUTH = "Basic " + base64.b64encode(b":ado-pat").decode()
CONF_AUTH = JIRA_AUTH


@dataclass
class FakeWorkTools:
    url: str = ""
    jira_projects: set[str] = field(default_factory=lambda: {"PAY"})
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    work_items: dict[int, dict[str, Any]] = field(default_factory=dict)
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    webhooks: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"slack": [], "teams": []})
    requests: list[tuple[str, str]] = field(default_factory=list)
    fail_next: dict[str, int] = field(default_factory=dict)  # route -> times to answer 503

    def _fail(self, route: str) -> Response | None:
        if self.fail_next.get(route, 0) > 0:
            self.fail_next[route] -= 1
            return JSONResponse({"error": "busy"}, status_code=503)
        return None

    # --- Jira ---
    async def jira_create(self, request: Request) -> Response:
        version = request.path_params["v"]
        self.requests.append(("POST", request.url.path))
        if request.headers.get("authorization") != JIRA_AUTH:
            return JSONResponse({"errorMessages": ["unauthorized"]}, status_code=401)
        fields = (await request.json())["fields"]
        errors = {}
        if fields.get("project", {}).get("key") not in self.jira_projects:
            errors["project"] = "valid project is required"
        if not fields.get("issuetype", {}).get("name"):
            errors["issuetype"] = "issue type is required"
        desc = fields.get("description")
        if version == "3" and not (isinstance(desc, dict) and desc.get("type") == "doc"):
            errors["description"] = "Operation value must be an Atlassian Document"
        if version == "2" and not isinstance(desc, str):
            errors["description"] = "must be a string"
        parent = fields.get("parent", {}).get("key")
        if parent and parent not in self.issues:
            errors["parent"] = "parent does not exist"
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        key = f"{fields['project']['key']}-{len(self.issues) + 1}"
        self.issues[key] = {"id": str(10000 + len(self.issues)), "fields": fields, "updates": 0}
        return JSONResponse(
            {"id": self.issues[key]["id"], "key": key, "self": f"{self.url}/rest/api/{version}/issue/{key}"},
            status_code=201,
        )

    async def jira_update(self, request: Request) -> Response:
        key = request.path_params["key"]
        if request.headers.get("authorization") != JIRA_AUTH:
            return JSONResponse({}, status_code=401)
        if key not in self.issues:
            return JSONResponse({"errorMessages": ["Issue does not exist"]}, status_code=404)
        self.issues[key]["fields"].update((await request.json())["fields"])
        self.issues[key]["updates"] += 1
        return Response(status_code=204)

    async def jira_link(self, request: Request) -> Response:
        self.links.append(await request.json())
        return Response(status_code=201)

    # --- Azure Boards ---
    async def ado_create(self, request: Request) -> Response:
        if failed := self._fail("ado_create"):
            return failed
        if request.headers.get("authorization") != ADO_AUTH:
            return Response("unauthorized", status_code=401)
        if request.headers.get("content-type") != "application/json-patch+json":
            return JSONResponse({"message": "wrong content type"}, status_code=415)
        wtype = request.path_params["wtype"].lstrip("$")
        ops = await request.json()
        fields = {
            o["path"].split("/fields/", 1)[1]: o["value"] for o in ops if o["path"].startswith("/fields/")
        }
        rels = [o["value"] for o in ops if o["path"] == "/relations/-"]
        for r in rels:
            pid = int(r["url"].rsplit("/", 1)[1])
            if pid not in self.work_items:
                return JSONResponse({"message": "parent not found"}, status_code=400)
        wid = 100 + len(self.work_items)
        self.work_items[wid] = {
            "type": wtype,
            "project": request.path_params["project"],
            "fields": fields,
            "relations": rels,
            "updates": 0,
        }
        return JSONResponse(
            {
                "id": wid,
                "url": f"{self.url}/_apis/wit/workItems/{wid}",
                "_links": {
                    "html": {"href": f"{self.url}/{request.path_params['project']}/_workitems/edit/{wid}"}
                },
            }
        )

    async def ado_update(self, request: Request) -> Response:
        wid = int(request.path_params["wid"])
        if wid not in self.work_items:
            return JSONResponse({"message": "not found"}, status_code=404)
        for o in await request.json():
            if o["path"].startswith("/fields/"):
                self.work_items[wid]["fields"][o["path"].split("/fields/", 1)[1]] = o["value"]
        self.work_items[wid]["updates"] += 1
        return JSONResponse({"id": wid, "_links": {"html": {"href": f"{self.url}/x/_workitems/edit/{wid}"}}})

    # --- Confluence ---
    async def page(self, request: Request) -> Response:
        if request.headers.get("authorization") != CONF_AUTH:
            return JSONResponse({}, status_code=401)
        page = self.pages.get(request.path_params["pid"])
        if page is None:
            return JSONResponse({"message": "No content found"}, status_code=404)
        return JSONResponse(
            {
                "id": request.path_params["pid"],
                "title": page["title"],
                "version": {"number": page["version"]},
                "body": {"storage": {"value": page["body"], "representation": "storage"}},
                "_links": {
                    "webui": f"/spaces/ENG/pages/{request.path_params['pid']}",
                    "base": f"{self.url}/wiki",
                },
            }
        )

    # --- chat webhooks ---
    async def hook(self, request: Request) -> Response:
        name = request.path_params["name"]
        if failed := self._fail(name):
            return failed
        self.webhooks[name].append(await request.json())
        return Response("ok" if name == "slack" else "", status_code=200 if name == "slack" else 202)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/rest/api/{v}/issue", self.jira_create, methods=["POST"]),
                Route("/rest/api/{v}/issue/{key}", self.jira_update, methods=["PUT"]),
                Route("/rest/api/{v}/issueLink", self.jira_link, methods=["POST"]),
                Route("/{project}/_apis/wit/workitems/{wtype}", self.ado_create, methods=["POST"]),
                Route("/_apis/wit/workitems/{wid}", self.ado_update, methods=["PATCH"]),
                Route("/wiki/rest/api/content/{pid}", self.page),
                Route("/hooks/{name}", self.hook, methods=["POST"]),
            ]
        )


@contextmanager
def serve_worktools() -> Iterator[FakeWorkTools]:
    fake = FakeWorkTools()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    fake.url = f"http://127.0.0.1:{port}"
    try:
        yield fake
    finally:
        server.should_exit = True
        thread.join(timeout=10)
