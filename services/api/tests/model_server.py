"""A real OpenAI-compatible HTTP server for tests (no inference: responses are scripted per test).

It runs uvicorn on a free localhost port in a background thread, so the platform's HTTP provider,
endpoint configuration, auth headers, structured-output negotiation and usage accounting are all
exercised over a real socket. What it returns is decided by a `handler(request_json) -> dict` that
produces the assistant message content (a JSON object, serialized here) or raises `Reply` to send an
arbitrary HTTP response.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


class Reply(Exception):
    def __init__(self, status: int, body: dict[str, Any] | str) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


@dataclass
class FakeOpenAI:
    models: list[str]
    handler: Callable[[dict[str, Any]], dict[str, Any] | str] = lambda req: {}
    api_key: str | None = None
    auth: str = "bearer"  # bearer | api_key_header
    reject_json_schema: bool = False
    finish_reason: str = "stop"
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)
    query_seen: list[dict[str, str]] = field(default_factory=list)
    url: str = ""

    def _authorized(self, request: Request) -> bool:
        if self.api_key is None:
            return True
        if self.auth == "bearer":
            return request.headers.get("authorization") == f"Bearer {self.api_key}"
        return request.headers.get("api-key") == self.api_key

    async def _models(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": {"message": "bad key"}}, status_code=401)
        return JSONResponse({"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})

    async def _chat(self, request: Request) -> Response:
        self.headers_seen.append(dict(request.headers))
        self.query_seen.append(dict(request.query_params))
        if not self._authorized(request):
            return JSONResponse({"error": {"message": "bad key"}}, status_code=401)
        body = await request.json()
        self.requests.append(body)
        if body.get("model") not in self.models:
            return JSONResponse({"error": {"message": "model not found"}}, status_code=404)
        rf = body.get("response_format") or {}
        if self.reject_json_schema and rf.get("type") == "json_schema":
            return JSONResponse(
                {"error": {"message": "response_format json_schema is not supported by this server"}},
                status_code=400,
            )
        try:
            content = self.handler(body)
        except Reply as r:
            if isinstance(r.body, str):
                return Response(r.body, status_code=r.status)
            return JSONResponse(r.body, status_code=r.status)
        text = content if isinstance(content, str) else json.dumps(content)
        prompt_chars = sum(len(m.get("content") or "") for m in body["messages"])
        return JSONResponse(
            {
                "id": f"chatcmpl-{len(self.requests)}",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": self.finish_reason,
                    }
                ],
                # Deterministic "actual" usage, deliberately different from the chars/4 estimate.
                "usage": {
                    "prompt_tokens": prompt_chars // 3 + 7,
                    "completion_tokens": len(text) // 3 + 1,
                    "total_tokens": prompt_chars // 3 + len(text) // 3 + 8,
                },
            }
        )

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/models", self._models, methods=["GET"]),
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
            ]
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def serve(fake: FakeOpenAI) -> Iterator[FakeOpenAI]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake model server did not start")
        time.sleep(0.02)
    fake.url = f"http://127.0.0.1:{port}/v1"
    try:
        yield fake
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def system_prompt(body: dict[str, Any]) -> str:
    return next((m["content"] for m in body["messages"] if m["role"] == "system"), "")


def user_prompt(body: dict[str, Any]) -> str:
    return next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
