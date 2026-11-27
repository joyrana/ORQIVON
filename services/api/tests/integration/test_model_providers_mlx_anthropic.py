"""The local mlx-lm endpoint (schema in the prompt) and the Anthropic Messages API provider, each over
a real HTTP socket: request shape, structured output, usage, failure classification and routing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentic_sdlc.models.endpoints import EndpointConfig, parse_endpoints
from agentic_sdlc.models.providers import AnthropicProvider, OpenAICompatibleProvider
from agentic_sdlc.models.registry import ModelRegistry, default_providers
from agentic_sdlc.models.router import RoutingRequest, route
from agentic_sdlc.models.types import Message, ModelEntry, ModelError, ModelRequest
from tests.model_server import FakeOpenAI, Reply, serve, system_prompt

SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


def entry(**kw: Any) -> ModelEntry:
    base: dict[str, Any] = dict(
        model_id="m",
        provider="openai_compatible",
        served_model_name="mlx-community/Qwen3-4B-Instruct-2507-4bit",
        revision="r1",
        family="f",
        license="Apache-2.0",
        tokenizer="t",
        context_window=32768,
        max_output_tokens=4096,
        tasks=["extraction"],
        capabilities=["structured_output"],
        privacy="self_hosted",
    )
    base.update(kw)
    return ModelEntry(**base)


def claude(**kw: Any) -> ModelEntry:
    return entry(
        **{
            "model_id": "claude-opus-5-5",
            "provider": "anthropic",
            "endpoint": "anthropic",
            "served_model_name": "claude-opus-5-5",
            "privacy": "external",
            "max_output_tokens": 16000,
            "deployment": {"effort": "high"},
            **kw,
        }
    )


def request(schema: dict[str, Any] | None = SCHEMA) -> ModelRequest:
    return ModelRequest(
        messages=[Message(role="system", content="sys"), Message(role="user", content="hello")],
        json_schema=schema,
        timeout_s=10,
    )


# ---------------------------------------------------------------- mlx-lm (prompt mode)


def test_prompt_mode_sends_schema_in_system_message_without_response_format() -> None:
    with serve(FakeOpenAI(models=[entry().served_model_name], handler=lambda b: {"answer": "x"})) as fake:
        provider = OpenAICompatibleProvider(fake.url, None, auth="none", schema_mode="prompt")
        resp = provider.generate(entry(), request())
    body = fake.requests[0]
    assert "response_format" not in body
    assert system_prompt(body).startswith("sys\n\nReturn one JSON object")
    assert json.dumps(SCHEMA, separators=(",", ":")) in system_prompt(body)
    assert json.loads(resp.text) == {"answer": "x"}
    assert provider.schema_mode == "prompt"
    assert "authorization" not in fake.headers_seen[0]


def test_trailing_end_of_turn_token_is_stripped() -> None:
    # mlx_lm.server 0.29 leaves Qwen's end-of-turn token at the end of the content.
    reply = '{"answer": "x"}<|im_end|>'
    with serve(FakeOpenAI(models=[entry().served_model_name], handler=lambda b: reply)) as fake:
        resp = OpenAICompatibleProvider(fake.url, None, schema_mode="prompt").generate(entry(), request())
    assert resp.text == '{"answer": "x"}'


def test_streaming_assembles_content_and_usage() -> None:
    reply = json.dumps({"answer": "streamed across several chunks"}) + "<|im_end|>"
    with serve(FakeOpenAI(models=[entry().served_model_name], handler=lambda b: reply)) as fake:
        provider = OpenAICompatibleProvider(fake.url, None, schema_mode="prompt", stream=True)
        resp = provider.generate(entry(), request())
    assert fake.requests[0]["stream"] is True
    assert json.loads(resp.text) == {"answer": "streamed across several chunks"}
    assert resp.usage_estimated is False


def test_streaming_reports_truncation_and_server_errors() -> None:
    fake_args = dict(models=[entry().served_model_name], handler=lambda b: {"answer": "x"})
    with serve(FakeOpenAI(**fake_args, finish_reason="length")) as fake, pytest.raises(ModelError) as exc:
        OpenAICompatibleProvider(fake.url, None, stream=True).generate(entry(), request())
    assert "truncated" in str(exc.value)

    def busy(b: dict[str, Any]) -> Any:
        raise Reply(503, {"error": {"message": "busy"}})

    with (
        serve(FakeOpenAI(models=[entry().served_model_name], handler=busy)) as fake,
        pytest.raises(ModelError) as exc,
    ):
        OpenAICompatibleProvider(fake.url, None, stream=True).generate(entry(), request())
    assert "503" in str(exc.value) and exc.value.retryable


def test_local_mlx_endpoint_gets_prompt_mode_from_config() -> None:
    eps = parse_endpoints(
        [{"name": "local-mlx", "base_url_env": "MLX_LM_URL", "auth": "none", "structured_output": "prompt"}],
        None,
    )
    mlx = next(e for e in eps if e.name == "local-mlx")
    assert mlx.is_configured({}) is False
    assert mlx.is_configured({"MLX_LM_URL": "http://127.0.0.1:8001/v1"}) is True


# ---------------------------------------------------------------- Anthropic


@dataclass
class FakeAnthropic:
    """Scripted Messages API: `handler(body)` returns the text, or a (status, error body) tuple."""

    handler: Any = lambda body: json.dumps({"answer": "x"})
    stop_reason: str = "end_turn"
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)
    url: str = ""

    async def _messages(self, request: Request) -> Response:
        self.headers_seen.append(dict(request.headers))
        body = await request.json()
        self.requests.append(body)
        # SDK 2.x sends the key as a bearer token; older releases used x-api-key.
        sent = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix(
            "Bearer "
        )
        if sent != "sk-test":
            return JSONResponse(
                {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
                status_code=401,
            )
        out = self.handler(body)
        if isinstance(out, tuple):
            status, err = out
            return JSONResponse({"type": "error", "error": err}, status_code=status)
        return JSONResponse(
            {
                "id": f"msg_{len(self.requests)}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": out},
                ],
                "stop_reason": self.stop_reason,
                "stop_sequence": None,
                "stop_details": {"type": "refusal", "category": "cyber", "explanation": None}
                if self.stop_reason == "refusal"
                else None,
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                },
            }
        )

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/messages", self._messages, methods=["POST"])])


def anthropic_for(fake: FakeAnthropic, key: str = "sk-test") -> AnthropicProvider:
    return AnthropicProvider(key, fake.url.removesuffix("/v1"), max_retries=0)


def test_anthropic_structured_output_request_and_usage() -> None:
    with serve(FakeAnthropic()) as fake:  # type: ignore[arg-type]
        resp = anthropic_for(fake).generate(claude(), request())
    body = fake.requests[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["fallbacks"] == "default"
    assert "temperature" not in body
    assert AnthropicProvider.FALLBACK_BETA in fake.headers_seen[0]["anthropic-beta"]
    assert json.loads(resp.text) == {"answer": "x"}  # thinking block skipped
    assert (resp.input_tokens, resp.output_tokens, resp.usage_estimated) == (16, 5, False)


def test_anthropic_schema_rejection_falls_back_to_prompt_once() -> None:
    def handler(body: dict[str, Any]) -> Any:
        if "format" in (body.get("output_config") or {}):
            return 400, {
                "type": "invalid_request_error",
                "message": "output_config.format: schema unsupported",
            }
        return json.dumps({"answer": "y"})

    with serve(FakeAnthropic(handler=handler)) as fake:  # type: ignore[arg-type]
        provider = anthropic_for(fake)
        assert json.loads(provider.generate(claude(), request()).text) == {"answer": "y"}
        provider.generate(claude(), request())
    assert provider.schema_mode == "prompt"
    assert len(fake.requests) == 3  # rejected, retried, then prompt mode straight away
    assert "Return one JSON object" in fake.requests[1]["system"]
    assert "format" not in fake.requests[2]["output_config"]


@pytest.mark.parametrize(
    ("fake_kw", "key", "message", "retryable"),
    [
        ({}, "wrong", "refused credentials", False),
        ({"stop_reason": "refusal"}, "sk-test", "declined", False),
        ({"stop_reason": "max_tokens"}, "sk-test", "truncated", False),
        (
            {"handler": lambda b: (529, {"type": "overloaded_error", "message": "busy"})},
            "sk-test",
            "529",
            True,
        ),
        (
            {"handler": lambda b: (400, {"type": "invalid_request_error", "message": "bad"})},
            "sk-test",
            "400",
            False,
        ),
    ],
)
def test_anthropic_failure_classification(
    fake_kw: dict[str, Any], key: str, message: str, retryable: bool
) -> None:
    with serve(FakeAnthropic(**fake_kw)) as fake, pytest.raises(ModelError) as exc:  # type: ignore[arg-type]
        anthropic_for(fake, key).generate(claude(), request())
    assert message in str(exc.value)
    assert exc.value.retryable is retryable


# ---------------------------------------------------------------- registry and routing


def test_anthropic_endpoint_configured_by_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = EndpointConfig(
        name="anthropic", api="anthropic", api_key_env="ANTHROPIC_API_KEY", privacy="external"
    )
    assert ep.is_configured({}) is False
    assert ep.is_configured({"ANTHROPIC_API_KEY": "k"}) is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(default_providers([ep])["anthropic:anthropic"], AnthropicProvider)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.chdir("/")  # no .env to fall back to
    assert "anthropic:anthropic" not in default_providers([ep])


def test_registry_rejects_provider_endpoint_mismatch() -> None:
    eps = [EndpointConfig(name="anthropic", api="anthropic", privacy="external")]
    with pytest.raises(ValueError, match="serves api 'anthropic'"):
        ModelRegistry([entry(endpoint="anthropic", privacy="external")], {}, eps)


class _Up:
    def available(self, entry: ModelEntry) -> bool:
        return True

    def generate(self, entry: ModelEntry, request: ModelRequest) -> Any:
        raise NotImplementedError


def test_local_model_is_default_and_claude_needs_privacy_any() -> None:
    eps = [
        EndpointConfig(name="local-mlx", base_url="http://127.0.0.1:8001/v1", structured_output="prompt"),
        EndpointConfig(name="anthropic", api="anthropic", privacy="external"),
    ]
    reg = ModelRegistry(
        [entry(model_id="qwen3", endpoint="local-mlx", priority=70), claude(priority=40)],
        {"openai_compatible:local-mlx": _Up(), "anthropic:anthropic": _Up()},
        eps,
    )
    assert route(reg, RoutingRequest(task="extraction")).model_id == "qwen3"
    assert route(reg, RoutingRequest(task="extraction", privacy="any")).model_id == "qwen3"
    # When the local model is down (or already failed this task), Claude takes over if allowed.
    fallback = RoutingRequest(task="extraction", privacy="any", excluded=["qwen3"])
    assert route(reg, fallback).model_id == "claude-opus-5-5"
    blocked = route(reg, RoutingRequest(task="extraction", excluded=["qwen3"]))
    assert blocked.model_id is None
