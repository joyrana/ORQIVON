"""The OpenAI-compatible provider over real HTTP: endpoints, auth, structured-output negotiation,
usage accounting and failure classification."""

from __future__ import annotations

from typing import Any

import pytest

from agentic_sdlc.models.endpoints import EndpointConfig, parse_endpoints
from agentic_sdlc.models.providers import MockProvider, OpenAICompatibleProvider
from agentic_sdlc.models.registry import ModelRegistry, default_providers
from agentic_sdlc.models.router import RoutingRequest, route
from agentic_sdlc.models.types import Message, ModelEntry, ModelError, ModelRequest
from agentic_sdlc.skills.base import ModelCall
from tests.model_server import FakeOpenAI, Reply, serve

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
        served_model_name="org/model-7b",
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


def request(schema: dict[str, Any] | None = SCHEMA) -> ModelRequest:
    return ModelRequest(
        messages=[Message(role="system", content="sys"), Message(role="user", content="hello")],
        json_schema=schema,
        timeout_s=10,
    )


def test_bearer_auth_models_listing_and_actual_usage() -> None:
    with serve(FakeOpenAI(models=["org/model-7b"], api_key="k1", handler=lambda b: {"answer": "x"})) as fake:
        good = OpenAICompatibleProvider(fake.url, "k1")
        assert good.available(entry()) is True
        assert OpenAICompatibleProvider(fake.url, "wrong").available(entry()) is False
        resp = good.generate(entry(), request())
        assert resp.text == '{"answer": "x"}'
        assert resp.usage_estimated is False
        sent = fake.requests[-1]
        assert sent["response_format"]["type"] == "json_schema"
        assert sent["response_format"]["json_schema"]["schema"] == SCHEMA
        assert fake.headers_seen[-1]["authorization"] == "Bearer k1"
        with pytest.raises(ModelError) as exc:
            OpenAICompatibleProvider(fake.url, "wrong").generate(entry(), request())
        assert exc.value.retryable is False and "credentials" in str(exc.value)


def test_reported_usage_replaces_the_estimate() -> None:
    with serve(FakeOpenAI(models=["org/model-7b"], handler=lambda b: {"answer": "abc"})) as fake:
        resp = OpenAICompatibleProvider(fake.url).generate(entry(), request())
        body = fake.requests[-1]
        prompt_chars = sum(len(m["content"]) for m in body["messages"])
        assert resp.usage_estimated is False
        assert resp.input_tokens == prompt_chars // 3 + 7
        assert resp.output_tokens == len(resp.text) // 3 + 1


def test_api_key_header_and_query_parameters_for_azure_style_endpoints() -> None:
    fake = FakeOpenAI(
        models=["gpt-x"], api_key="az", auth="api_key_header", handler=lambda b: {"answer": "y"}
    )
    with serve(fake):
        p = OpenAICompatibleProvider(
            fake.url, "az", auth="api_key_header", query={"api-version": "2025-04-01-preview"}
        )
        p.generate(entry(served_model_name="gpt-x"), request())
        assert fake.headers_seen[-1]["api-key"] == "az" and "authorization" not in fake.headers_seen[-1]
        assert fake.query_seen[-1] == {"api-version": "2025-04-01-preview"}


def test_falls_back_to_json_object_mode_once_and_remembers() -> None:
    fake = FakeOpenAI(models=["org/model-7b"], reject_json_schema=True, handler=lambda b: {"answer": "z"})
    with serve(fake):
        p = OpenAICompatibleProvider(fake.url)
        resp = p.generate(entry(), request())
        assert resp.text == '{"answer": "z"}' and p.schema_mode == "json_object"
        retried = fake.requests[-1]
        assert retried["response_format"] == {"type": "json_object"}
        assert '"answer"' in retried["messages"][0]["content"]  # schema moved into the system prompt
        n = len(fake.requests)
        p.generate(entry(), request())
        assert len(fake.requests) == n + 1  # no second json_schema attempt


def test_failures_are_classified() -> None:
    state = {"status": 500}

    def handler(body: dict[str, Any]) -> dict[str, Any]:
        raise Reply(state["status"], {"error": {"message": "boom"}})

    with serve(FakeOpenAI(models=["org/model-7b"], handler=handler)) as fake:
        p = OpenAICompatibleProvider(fake.url)
        for status, retryable in [(500, True), (429, True), (422, False)]:
            state["status"] = status
            with pytest.raises(ModelError) as exc:
                p.generate(entry(), request())
            assert exc.value.retryable is retryable, status
        with pytest.raises(ModelError, match="rejected"):
            p.generate(entry(served_model_name="not-served"), request())


def test_truncated_output_is_not_retried() -> None:
    with serve(
        FakeOpenAI(models=["org/model-7b"], finish_reason="length", handler=lambda b: '{"answ')
    ) as fake:
        with pytest.raises(ModelError, match="truncated") as exc:
            OpenAICompatibleProvider(fake.url).generate(entry(), request())
        assert exc.value.retryable is False


def test_unreachable_endpoint_is_a_retryable_error() -> None:
    p = OpenAICompatibleProvider("http://127.0.0.1:9/v1")
    assert p.available(entry()) is False
    with pytest.raises(ModelError) as exc:
        p.generate(entry(), request())
    assert exc.value.retryable is True


def test_model_call_accepts_fenced_json() -> None:
    fenced = '```json\n{"answer": "fenced"}\n```'
    with serve(FakeOpenAI(models=["org/model-7b"], handler=lambda b: fenced)) as fake:
        e = entry()
        reg = ModelRegistry([e], {"openai_compatible": OpenAICompatibleProvider(fake.url)})
        usage: list[tuple[Any, ...]] = []
        call = ModelCall(
            entry=e,
            registry=reg,
            on_usage=lambda *a: usage.append(a),
            budget=None,
            max_output_tokens=100,
            timeout_s=10,
        )
        assert call.structured("sys", "user", SCHEMA) == {"answer": "fenced"}
        assert usage and usage[0][2] is False  # usage reported, not estimated


# --- endpoint configuration ----------------------------------------------------------------------------


def test_endpoint_config_never_holds_credentials_in_headers() -> None:
    with pytest.raises(ValueError, match="credential"):
        EndpointConfig(name="x", base_url="https://h/v1", headers={"Authorization": "Bearer s"})
    with pytest.raises(ValueError, match="not both"):
        EndpointConfig(name="x", base_url="https://h/v1", base_url_env="URL")
    with pytest.raises(ValueError, match="lowercase"):
        EndpointConfig(name="Bad Name")


def test_public_view_hides_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY_FOR_TEST", "s3cr3t")
    ep = EndpointConfig(name="x", base_url="https://h/v1", api_key_env="SECRET_KEY_FOR_TEST")
    view = ep.public()
    assert view["api_key_set"] is True and "s3cr3t" not in str(view)


def test_default_endpoint_is_always_defined() -> None:
    eps = parse_endpoints([{"name": "other", "base_url": "https://o/v1", "privacy": "external"}], None)
    assert [e.name for e in eps] == ["default", "other"]
    with pytest.raises(ValueError, match="duplicate"):
        parse_endpoints([{"name": "a"}, {"name": "a"}], None)


def test_registry_rejects_unknown_endpoints_and_mislabelled_privacy() -> None:
    eps = parse_endpoints([{"name": "hosted", "base_url": "https://o/v1", "privacy": "external"}], None)
    with pytest.raises(ValueError, match="unknown endpoint"):
        ModelRegistry([entry(endpoint="nope")], {}, eps)
    with pytest.raises(ValueError, match="privacy: external"):
        ModelRegistry([entry(endpoint="hosted")], {}, eps)
    ModelRegistry([entry(endpoint="hosted", privacy="external")], {}, eps)


def test_models_on_different_endpoints_route_through_their_own_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = FakeOpenAI(models=["local/coder"], handler=lambda b: {"answer": "local"})
    b = FakeOpenAI(models=["hosted/big"], api_key="hk", handler=lambda b: {"answer": "hosted"})
    with serve(a), serve(b):
        monkeypatch.setenv("TEST_HOSTED_URL", b.url)
        monkeypatch.setenv("TEST_HOSTED_KEY", "hk")
        eps = parse_endpoints(
            [
                {"name": "local", "base_url": a.url},
                {
                    "name": "hosted",
                    "base_url_env": "TEST_HOSTED_URL",
                    "api_key_env": "TEST_HOSTED_KEY",
                    "privacy": "external",
                },
                {"name": "unset", "base_url_env": "TEST_UNSET_URL_XYZ"},
            ],
            None,
        )
        providers = default_providers(eps)
        assert set(providers) == {"mock", "openai_compatible:local", "openai_compatible:hosted"}
        local = entry(model_id="local-coder", served_model_name="local/coder", endpoint="local")
        hosted = entry(
            model_id="hosted-big",
            served_model_name="hosted/big",
            endpoint="hosted",
            privacy="external",
            priority=100,
        )
        unset = entry(model_id="unset-model", served_model_name="x", endpoint="unset")
        reg = ModelRegistry([local, hosted, unset], providers, eps)

        # Default policy keeps project content on self-hosted models even when a hosted one scores higher.
        d = route(reg, RoutingRequest(task="extraction", capabilities=["structured_output"]))
        assert d.model_id == "local-coder"
        reasons = {c.model_id: c.reasons for c in d.candidates}
        assert reasons["hosted-big"] == ["external provider not allowed by privacy policy"]
        assert reasons["unset-model"] == ["provider not configured or model not served"]

        d = route(reg, RoutingRequest(task="extraction", capabilities=["structured_output"], privacy="any"))
        assert d.model_id == "hosted-big"
        resp = reg.provider_for(hosted).generate(hosted, request())  # type: ignore[union-attr]
        assert resp.text == '{"answer": "hosted"}' and b.headers_seen[-1]["authorization"] == "Bearer hk"
        assert a.requests == []


def test_mock_provider_is_still_keyed_by_bare_name() -> None:
    reg = ModelRegistry([entry(provider="mock", is_mock=True)], {"mock": MockProvider()})
    assert isinstance(reg.provider_for(reg.get("m")), MockProvider)


# --- live check command --------------------------------------------------------------------------------


def test_models_check_reports_per_model_results() -> None:
    from agentic_sdlc.models.check import check_models

    good = {"requirement_ids": ["REQ-1", "REQ-2"], "has_ambiguity": True}
    fake = FakeOpenAI(models=["org/good", "org/bad"], reject_json_schema=True)
    fake.handler = lambda b: good if b["model"] == "org/good" else {"ids": "nope"}
    with serve(fake):
        eps = parse_endpoints([{"name": "lab", "base_url": fake.url}], None)
        reg = ModelRegistry(
            [
                entry(model_id="good", served_model_name="org/good", endpoint="lab"),
                entry(model_id="bad", served_model_name="org/bad", endpoint="lab"),
                entry(model_id="absent", served_model_name="org/absent", endpoint="lab"),
                entry(model_id="nowhere", served_model_name="x", endpoint="default"),
            ],
            default_providers(eps),
            eps,
        )
        rows = {r["model_id"]: r for r in check_models(reg, timeout_s=10)}
    assert rows["good"]["ok"] and rows["good"]["answer_correct"] is True
    assert rows["good"]["structured_output_mode"] == "json_object" and rows["good"]["usage_reported"]
    assert rows["bad"]["ok"] is False and "schema" in rows["bad"]["error"]
    assert rows["absent"]["served"] is False and "not in the endpoint" in rows["absent"]["error"]
    assert "not configured" in rows["nowhere"]["error"]
    sent = [m["content"] for b in fake.requests for m in b["messages"]]
    assert all("REQ-1" in c or "JSON" in c or "requirement" in c for c in sent)  # synthetic probe only


def test_model_endpoints_api_hides_keys(client: Any) -> None:
    from tests.conftest import H

    r = client.get("/api/v1/model-endpoints", headers=H("auditor"))
    assert r.status_code == 200
    body = r.json()
    assert body["privacy_policy"] == "self_hosted_only"
    default = next(e for e in body["endpoints"] if e["name"] == "default")
    assert set(default) >= {"configured", "api_key_set", "privacy", "models"}
    assert "api_key" not in default
