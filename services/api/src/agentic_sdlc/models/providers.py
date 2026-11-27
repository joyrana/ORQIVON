"""Provider implementations behind a single interface."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from agentic_sdlc.models.tokens import estimate_messages, estimate_tokens
from agentic_sdlc.models.types import ModelEntry, ModelError, ModelRequest, ModelResponse


class ModelProvider(Protocol):
    def generate(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse: ...

    def available(self, entry: ModelEntry) -> bool: ...


# End-of-turn tokens some servers leave in the text (mlx_lm.server 0.29 with Qwen: "<|im_end|>").
_END_OF_TURN = re.compile(r"(?:\s*<\|(?:im_end|endoftext|eot_id|end)\|>)+\s*$")


class OpenAICompatibleProvider:
    """Chat Completions over HTTP: vLLM, TGI, llama.cpp server, Ollama, Azure OpenAI, OpenRouter, ...

    Structured output is requested with `response_format: json_schema`. Servers that reject that
    (HTTP 400 naming `response_format`/`json_schema`) are retried once in `json_object` mode with the
    schema placed in the system message, and the endpoint remembers the downgrade. An endpoint can
    start in `prompt` mode (schema in the system message, no `response_format`) for servers that
    ignore `response_format`, such as mlx_lm.server. The caller always validates the result: servers
    differ in how strictly they enforce any mode.

    With `stream=True` the response is read as server-sent events and the call's time limit is
    enforced across the whole stream. Abandoning a call closes the connection, which makes servers
    such as mlx_lm.server stop generating rather than finish in the background.
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
        schema_mode: str = "json_schema",
        stream: bool = False,
    ) -> None:
        self.name = name
        self.stream = stream
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.auth = auth
        self.extra_headers = dict(headers or {})
        self.query = dict(query or {})
        self.client = client or httpx.Client()
        self._served: tuple[float, set[str]] | None = None
        self.schema_mode: str = schema_mode  # json_schema | json_object | prompt

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
            if mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            _schema_in_system(messages, request.json_schema)
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

    def _post_stream(
        self, body: dict[str, object], timeout_s: float
    ) -> tuple[int, str, dict[str, Any] | None]:
        """Stream a completion; returns (status, error text, assembled response in non-stream shape)."""
        deadline = time.monotonic() + timeout_s
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        parts: list[str] = []
        finish: str | None = None
        usage: dict[str, Any] = {}
        try:
            with self.client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
                params=self.query,
                timeout=timeout_s,
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    return r.status_code, r.text, None
                for line in r.iter_lines():
                    if time.monotonic() > deadline:
                        # Leaving the `with` closes the connection: the server stops generating.
                        raise ModelError("model request timed out")
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            parts.append(content)
                        finish = choice.get("finish_reason") or finish
        except httpx.TimeoutException as exc:
            raise ModelError("model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"model request failed: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise ModelError("malformed model response") from exc
        message = {"content": "".join(parts)}
        return 200, "", {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}

    def _call(self, body: dict[str, object], timeout_s: float) -> tuple[int, str, dict[str, Any] | None]:
        if self.stream:
            return self._post_stream(body, timeout_s)
        r = self._post(body, timeout_s)
        if r.status_code >= 400:
            return r.status_code, r.text, None
        try:
            return r.status_code, r.text, r.json()
        except ValueError as exc:
            raise ModelError("malformed model response") from exc

    def generate(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        start = time.perf_counter()
        status, error_text, data = self._call(self._body(entry, request, self.schema_mode), request.timeout_s)
        if (
            status == 400
            and request.json_schema is not None
            and self.schema_mode == "json_schema"
            and re.search(r"response_format|json_schema|guided", error_text[:2000], re.I)
        ):
            self.schema_mode = "json_object"
            status, error_text, data = self._call(
                self._body(entry, request, self.schema_mode), request.timeout_s
            )
        if status >= 500 or status == 429:
            raise ModelError(f"model server returned {status}")
        if status in (401, 403):
            raise ModelError(f"model server refused credentials ({status})", retryable=False)
        if status >= 400 or data is None:
            raise ModelError(f"model server rejected request ({status})", retryable=False)
        try:
            choice = data["choices"][0]
            text = _END_OF_TURN.sub("", choice["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
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


def _schema_note(schema: dict[str, object]) -> str:
    return "Return one JSON object that validates against this JSON Schema:\n" + json.dumps(
        schema, separators=(",", ":")
    )


def _schema_in_system(messages: list[dict[str, str]], schema: dict[str, object]) -> None:
    note = _schema_note(schema)
    if messages and messages[0]["role"] == "system":
        messages[0] = {**messages[0], "content": messages[0]["content"] + "\n\n" + note}
    else:
        messages.insert(0, {"role": "system", "content": note})


class AnthropicProvider:
    """Claude through the Anthropic Messages API, using the official SDK.

    Structured output uses `output_config.format` (JSON Schema). If the API rejects a schema (HTTP 400
    naming the format or schema, for features structured outputs does not support), the request is
    retried once with the schema in the system prompt instead, and the endpoint remembers that. Refusal
    fallbacks are on (`fallbacks: "default"`): if a model declines, the API re-runs the request on a
    fallback model within the same call. `temperature` is never sent: API version 2026-10-21 rejects it.
    Thinking is left at the model's adaptive default; `deployment.effort` on the registry entry sets
    `output_config.effort`.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        name: str = "anthropic",
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        import anthropic

        self.name = name
        self.schema_mode: str = "json_schema"  # json_schema | prompt
        self.client = client or anthropic.Anthropic(
            api_key=api_key, base_url=base_url, max_retries=max_retries
        )

    def available(self, entry: ModelEntry) -> bool:
        return True  # configured means a key is set; the API serves every listed model id

    def _params(self, entry: ModelEntry, request: ModelRequest, mode: str) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages = [{"role": m.role, "content": m.content} for m in request.messages if m.role != "system"]
        output_config: dict[str, Any] = {}
        if effort := entry.deployment.get("effort"):
            output_config["effort"] = effort
        if request.json_schema is not None:
            if mode == "json_schema":
                output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
            else:
                system = (system + "\n\n" if system else "") + _schema_note(request.json_schema)
        params: dict[str, Any] = {
            "model": entry.served_model_name,
            "max_tokens": min(request.max_output_tokens, entry.max_output_tokens),
            "messages": messages,
            "betas": [self.FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            params["system"] = system
        if output_config:
            params["output_config"] = output_config
        return params

    def _create(self, params: dict[str, Any], timeout_s: float) -> Any:
        import anthropic

        try:
            return self.client.beta.messages.create(**params, timeout=timeout_s)
        except anthropic.APITimeoutError as exc:
            raise ModelError("model request timed out") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ModelError(
                f"Anthropic API refused credentials ({exc.status_code})", retryable=False
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ModelError("Anthropic API rate limit (429)") from exc
        except anthropic.BadRequestError:
            raise  # the caller decides whether to downgrade the schema mode
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in (408, 409)
            raise ModelError(f"Anthropic API returned {exc.status_code}", retryable=retryable) from exc
        except anthropic.APIConnectionError as exc:
            raise ModelError(f"model request failed: {exc.__class__.__name__}") from exc

    def generate(self, entry: ModelEntry, request: ModelRequest) -> ModelResponse:
        import anthropic

        start = time.perf_counter()
        try:
            try:
                msg = self._create(self._params(entry, request, self.schema_mode), request.timeout_s)
            except anthropic.BadRequestError as exc:
                schema_problem = re.search(r"output_config|format|schema", str(exc.message)[:2000], re.I)
                if request.json_schema is None or self.schema_mode != "json_schema" or not schema_problem:
                    raise
                self.schema_mode = "prompt"
                msg = self._create(self._params(entry, request, self.schema_mode), request.timeout_s)
        except anthropic.BadRequestError as exc:
            raise ModelError(
                f"Anthropic API rejected the request (400): {exc.message}", retryable=False
            ) from exc
        if msg.stop_reason == "refusal":
            category = getattr(msg.stop_details, "category", None) if msg.stop_details else None
            raise ModelError(f"model declined the request (refusal: {category})", retryable=False)
        if msg.stop_reason == "max_tokens":
            raise ModelError("model output was truncated at the output token limit", retryable=False)
        text = "".join(b.text for b in msg.content if b.type == "text")
        usage = msg.usage
        input_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        return ModelResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
            usage_estimated=False,
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
