"""Fake deployment services for tests: GitHub Actions, Jenkins, Azure Pipelines, Argo CD.

Each pipeline is "running" for `polls_to_finish` status reads and then finishes with `outcome`
(success or failure), so tests exercise the platform's waiting, polling and result handling.
Every trigger is recorded with its parameters.
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

TOKEN = "Bearer deploy-token"
JENKINS = "Basic " + base64.b64encode(b"deployer:jenkins-api-token").decode()


@dataclass
class FakeCICD:
    url: str = ""
    polls_to_finish: int = 2
    outcome: str = "success"  # success | failure
    health_status: int = 200
    triggers: list[dict[str, Any]] = field(default_factory=list)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _new(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        run = {"id": len(self.runs) + 1, "kind": kind, "params": params, "polls": 0}
        self.runs[str(run["id"])] = run
        self.triggers.append({"kind": kind, **params})
        return run

    def _tick(self, run: dict[str, Any]) -> bool:
        """Advance one status read; True when finished."""
        run["polls"] += 1
        return run["polls"] > self.polls_to_finish

    def _auth(self, request: Request) -> Response | None:
        if request.headers.get("authorization") not in (TOKEN, JENKINS):
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        return None

    # --- GitHub Actions ---
    async def gha_dispatch(self, request: Request) -> Response:
        if denied := self._auth(request):
            return denied
        body = await request.json()
        if body.get("ref") != "main" or "asw_deployment_id" not in body.get("inputs", {}):
            return JSONResponse({"message": "Unexpected inputs"}, status_code=422)
        self._new("github_actions", {"workflow": request.path_params["wf"], **body["inputs"]})
        return Response(status_code=204)

    async def gha_runs(self, request: Request) -> Response:
        if denied := self._auth(request):
            return denied
        assert request.query_params.get("event") == "workflow_dispatch"
        out = []
        for run in self.runs.values():
            if run["kind"] != "github_actions":
                continue
            done = self._tick(run)
            out.append(
                {
                    "id": run["id"],
                    "display_title": f"Deploy {run['params']['asw_deployment_id']}",
                    "status": "completed" if done else "in_progress",
                    "conclusion": self.outcome if done else None,
                    "html_url": f"{self.url}/actions/runs/{run['id']}",
                }
            )
        return JSONResponse({"total_count": len(out), "workflow_runs": out})

    # --- Jenkins ---
    async def jenkins(self, request: Request) -> Response:
        if denied := self._auth(request):
            return denied
        rest = request.path_params["rest"]
        if rest.endswith("/buildWithParameters") and request.method == "POST":
            run = self._new(
                "jenkins", {"job": rest.rsplit("/buildWithParameters", 1)[0], **dict(request.query_params)}
            )
            run["queued"] = 0
            return Response(status_code=201, headers={"Location": f"{self.url}/queue/item/{run['id']}/"})
        number = rest.rstrip("/").split("/")[-3]  # .../job/x/<n>/api/json
        run = self.runs[number]
        done = self._tick(run)
        return JSONResponse(
            {
                "building": not done,
                "result": ("SUCCESS" if self.outcome == "success" else "FAILURE") if done else None,
            }
        )

    async def jenkins_queue(self, request: Request) -> Response:
        run = self.runs[request.path_params["n"]]
        run["queued"] += 1
        if run["queued"] < 2:
            return JSONResponse({"why": "Waiting for next available executor"})
        job = run["params"]["job"]
        return JSONResponse(
            {"executable": {"number": run["id"], "url": f"{self.url}/job/{job}/{run['id']}/"}}
        )

    # --- Azure Pipelines ---
    async def ado_run(self, request: Request) -> Response:
        if request.method == "POST":
            body = await request.json()
            run = self._new(
                "azure_pipelines", {"pipeline": request.path_params["pid"], **body["templateParameters"]}
            )
            return JSONResponse(
                {
                    "id": run["id"],
                    "state": "inProgress",
                    "_links": {"web": {"href": f"{self.url}/build/{run['id']}"}},
                }
            )
        run = self.runs[request.path_params["rid"]]
        done = self._tick(run)
        return JSONResponse(
            {
                "id": run["id"],
                "state": "completed" if done else "inProgress",
                "result": ("succeeded" if self.outcome == "success" else "failed") if done else None,
            }
        )

    # --- Argo CD ---
    async def argo_sync(self, request: Request) -> Response:
        if denied := self._auth(request):
            return denied
        body = await request.json()
        self._new("argocd", {"app": request.path_params["app"], "revision": body["revision"]})
        return JSONResponse({"metadata": {"name": request.path_params["app"]}})

    async def argo_app(self, request: Request) -> Response:
        run = next(r for r in reversed(list(self.runs.values())) if r["kind"] == "argocd")
        done = self._tick(run)
        phase = ("Succeeded" if self.outcome == "success" else "Failed") if done else "Running"
        return JSONResponse(
            {
                "status": {
                    "operationState": {
                        "phase": phase,
                        "message": "sync failed: hook failed" if phase == "Failed" else "",
                    },
                    "sync": {
                        "status": "Synced" if done else "OutOfSync",
                        "revision": run["params"]["revision"],
                    },
                    "health": {"status": "Healthy" if done else "Progressing"},
                }
            }
        )

    async def health(self, request: Request) -> Response:
        return Response("ok", status_code=self.health_status)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route(
                    "/repos/{o}/{r}/actions/workflows/{wf}/dispatches", self.gha_dispatch, methods=["POST"]
                ),
                Route("/repos/{o}/{r}/actions/runs", self.gha_runs),
                Route("/queue/item/{n}/api/json", self.jenkins_queue),
                Route("/job/{rest:path}", self.jenkins, methods=["GET", "POST"]),
                Route("/{project}/_apis/pipelines/{pid}/runs", self.ado_run, methods=["POST"]),
                Route("/{project}/_apis/pipelines/{pid}/runs/{rid}", self.ado_run),
                Route("/api/v1/applications/{app}/sync", self.argo_sync, methods=["POST"]),
                Route("/api/v1/applications/{app}", self.argo_app),
                Route("/healthz", self.health),
            ]
        )


@contextmanager
def serve_cicd() -> Iterator[FakeCICD]:
    fake = FakeCICD()
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
