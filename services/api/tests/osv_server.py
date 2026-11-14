"""A minimal OSV API for tests: /v1/querybatch and /v1/vulns/{id}, recording what was asked."""

from __future__ import annotations

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

from agentic_sdlc.security.dependencies import Package, affected
from tests.model_server import _free_port


@dataclass
class FakeOsv:
    records: list[dict[str, Any]]
    url: str = ""
    queries: list[dict[str, Any]] = field(default_factory=list)

    async def batch(self, request: Request) -> Response:
        body = await request.json()
        results = []
        for q in body["queries"]:
            self.queries.append(q)
            pkg = Package(q["package"]["ecosystem"], q["package"]["name"], q["version"], "")
            vulns = [
                {"id": r["id"], "modified": "2026-01-01T00:00:00Z"}
                for r in self.records
                if affected(r, pkg)[0]
            ]
            results.append({"vulns": vulns} if vulns else {})
        return JSONResponse({"results": results})

    async def vuln(self, request: Request) -> Response:
        vid = request.path_params["vid"]
        rec = next((r for r in self.records if r["id"] == vid), None)
        return JSONResponse(rec) if rec else JSONResponse({"code": 5}, status_code=404)


@contextmanager
def serve_osv(records: list[dict[str, Any]]) -> Iterator[FakeOsv]:
    osv = FakeOsv(records)
    app = Starlette(
        routes=[Route("/v1/querybatch", osv.batch, methods=["POST"]), Route("/v1/vulns/{vid}", osv.vuln)]
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    osv.url = f"http://127.0.0.1:{port}"
    try:
        yield osv
    finally:
        server.should_exit = True
        thread.join(timeout=10)
