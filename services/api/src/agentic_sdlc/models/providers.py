"""Provider implementations behind a single interface."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

import httpx

from agentic_sdlc.models.tokens import estimate_messages, estimate_tokens
from agentic_sdlc.models.types import ModelEntry, ModelError, ModelRequest, ModelResponse


class ModelProvider(Protocol):
    def generate(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse: ...

    def available(self, entry: ModelEntry) -> bool: ...


class OpenAICompatibleProvider:
    """Chat Completions over HTTP: vLLM, TGI, llama.cpp server, Ollama, Azure OpenAI, OpenRouter, ...

    Structured output is requested with `response_format: json_schema`. Servers that reject that
    (HTTP 400 naming `response_format`/`json_schema`) are retried once in `json_object` mode with the
    schema placed in the system message, and the endpoint remembers the downgrade. The caller always
    validates the result: servers differ in how strictly they enforce either mode.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        *,
        auth: str = "bearer",
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        name: str = "default",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.auth = auth
        self.extra_headers = dict(headers or {})
        self.query = dict(query or {})
        self.client = client or httpx.Client()
        self._served: tuple[float, set[str]] | None = None
        self.schema_mode: str = "json_schema"  # json_schema | json_object

    def _headers(self) -> dict[str, str]:
        h = dict(self.extra_headers)
        if self.api_key and self.auth == "bearer":
            h["Authorization"] = f"Bearer {self.api_key}"
        elif self.api_key and self.auth == "api_key_header":
            h["api-key"] = self.api_key
        return h

    def served_models(self, *, refresh: bool = False) -> set[str]:
        now = time.monotonic()
        if refresh or self._served is None or now - self._served[0] > 60:
            try:
                r = self.client.get(
                    f"{self.base_url}/models", headers=self._headers(), params=self.query, timeout=5
                )
                r.raise_for_status()
                names = {m.get("id", "") for m in r.json().get("data", [])}
            except (httpx.HTTPError, ValueError, AttributeError):
                names = set()
            self._served = (now, names)
        return self._served[1]

    def available(self, entry: ModelEntry) -> bool:
        return entry.served_model_name in self.served_models()

    def _body(self, entry: ModelEntry, request: ModelRequest, mode: str) -> dict[str, object]:
        messages = [m.model_dump() for m in request.messages]
        body: dict[str, object] = {
            "model": entry.served_model_name,
            "messages": messages,
            "max_tokens": min(request.max_output_tokens, entry.max_output_tokens),
            "temperature": request.temperature,
        }
        if request.json_schema is None:
            return body
        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": request.json_schema, "strict": True},
            }
        else:
            body["response_format"] = {"type": "json_object"}
            schema_note = (
                "\n\nReturn one JSON object that validates against this JSON Schema:\n"
                + json.dumps(request.json_schema, separators=(",", ":"))
            )
            if messages and messages[0]["role"] == "system":
                messages[0] = {**messages[0], "content": messages[0]["content"] + schema_note}
            else:
                messages.insert(0, {"role": "system", "content": schema_note.strip()})
        return body

    def _post(self, body: dict[str, object], timeout_s: float) -> httpx.Response:
        try:
            return self.client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
                params=self.query,
                timeout=timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ModelError("model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"model request failed: {exc.__class__.__name__}") from exc

    def generate(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        start = time.perf_counter()
        r = self._post(self._body(entry, request, self.schema_mode), request.timeout_s)
        if (
            r.status_code == 400
            and request.json_schema is not None
            and self.schema_mode == "json_schema"
            and re.search(r"response_format|json_schema|guided", r.text[:2000], re.I)
        ):
            self.schema_mode = "json_object"
            r = self._post(self._body(entry, request, self.schema_mode), request.timeout_s)
        if r.status_code >= 500 or r.status_code == 429:
            raise ModelError(f"model server returned {r.status_code}")
        if r.status_code in (401, 403):
            raise ModelError(f"model server refused credentials ({r.status_code})", retryable=False)
        if r.status_code >= 400:
            raise ModelError(f"model server rejected request ({r.status_code})", retryable=False)
        try:
            data = r.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("malformed model response") from exc
        if choice.get("finish_reason") == "length":
            raise ModelError("model output was truncated at the output token limit", retryable=False)
        usage = data.get("usage") or {}
        reported = "prompt_tokens" in usage and "completion_tokens" in usage
        return ModelResponse(
            text=text,
            input_tokens=int(usage["prompt_tokens"])
            if reported
            else estimate_messages([m.content for m in request.messages]),
            output_tokens=int(usage["completion_tokens"]) if reported else estimate_tokens(text),
            usage_estimated=not reported,
            latency_ms=int((time.perf_counter() - start) * 1000),
            model_ref=entry.ref,
        )


class MockProvider:
    """Deterministic in-process provider for tests and explicitly enabled demos. Never real inference.

    Responses come from a queue (scripted per test) or a responder function. Raising behaviors
    (timeouts, malformed output) can be scripted as exceptions in the queue.
    """

    def __init__(self, responder: Callable[[ModelRequest], str] | None = None) -> None:
        self.queue: deque[str | Exception] = deque()
        self.responder = responder
        self.calls: list[ModelRequest] = []

    def script(self, *responses: str | Exception) -> MockProvider:
        self.queue.extend(responses)
        return self

    def available(self, entry: ModelEntry) -> bool:
        return True

    def generate(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self.queue:
            item = self.queue.popleft()
            if isinstance(item, Exception):
                raise item
            text = item
        elif self.responder is not None:
            text = self.responder(request)
        else:
            raise ModelError("mock provider has no scripted response", retryable=False)
        return ModelResponse(
            text=text,
            input_tokens=estimate_messages([m.content for m in request.messages]),
            output_tokens=estimate_tokens(text),
            usage_estimated=True,
            latency_ms=0,
            model_ref=entry.ref,
            is_mock=True,
        )
