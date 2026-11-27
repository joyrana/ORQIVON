"""Model registry loaded from configuration and mirrored into the database with observed statistics."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.models.endpoints import DEFAULT_ENDPOINT, EndpointConfig, parse_endpoints
from agentic_sdlc.models.providers import (
    AnthropicProvider,
    MockProvider,
    ModelProvider,
    OpenAICompatibleProvider,
)
from agentic_sdlc.models.types import ModelEntry
from agentic_sdlc.orm import ModelRegistryEntry


class ModelRegistry:
    """Models the router may select, the endpoints that serve them, and their providers.

    Providers are keyed `"<provider>:<endpoint>"` (e.g. `openai_compatible:inhouse-vllm`); a bare
    provider key (`openai_compatible`) is accepted as a fallback for every endpoint, which keeps tests
    and single-endpoint setups simple.
    """

    def __init__(
        self,
        entries: list[ModelEntry],
        providers: dict[str, ModelProvider],
        endpoints: list[EndpointConfig] | None = None,
    ) -> None:
        ids = [e.model_id for e in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate model_id in registry")
        self.endpoints = {e.name: e for e in (endpoints or [])}
        for e in entries:
            if e.provider == "mock" or not self.endpoints:
                continue
            ep = self.endpoints.get(e.endpoint)
            if ep is None:
                raise ValueError(f"model '{e.model_id}' names unknown endpoint '{e.endpoint}'")
            if ep.api != e.provider:
                raise ValueError(
                    f"model '{e.model_id}' uses provider '{e.provider}' but endpoint '{ep.name}' "
                    f"serves api '{ep.api}'"
                )
            if ep.privacy == "external" and e.privacy != "external":
                raise ValueError(
                    f"model '{e.model_id}' is served by external endpoint '{ep.name}' and must declare "
                    "privacy: external"
                )
        self.entries = {e.model_id: e for e in entries}
        self.providers = providers

    @classmethod
    def from_file(cls, path: Path, providers: dict[str, ModelProvider] | None = None) -> ModelRegistry:
        data = yaml.safe_load(path.read_text()) or {}
        entries = [ModelEntry.model_validate(m) for m in data.get("models", [])]
        endpoints = parse_endpoints(data.get("endpoints"), get_settings().model_endpoint_url)
        return cls(entries, providers or default_providers(endpoints), endpoints)

    def get(self, model_id: str) -> ModelEntry:
        return self.entries[model_id]

    def provider_for(self, entry: ModelEntry) -> ModelProvider | None:
        return self.providers.get(f"{entry.provider}:{entry.endpoint}") or self.providers.get(entry.provider)

    def timeout_scale(self, entry: ModelEntry) -> float:
        ep = self.endpoints.get(entry.endpoint)
        return ep.timeout_scale if ep else 1.0

    def is_available(self, entry: ModelEntry) -> bool:
        provider = self.provider_for(entry)
        if provider is None:
            return False
        if entry.is_mock and not get_settings().allow_mock_models:
            return False
        return provider.available(entry)

    def sync_to_db(self, db: Session) -> None:
        for entry in self.entries.values():
            row = db.scalar(select(ModelRegistryEntry).where(ModelRegistryEntry.model_id == entry.model_id))
            data = entry.model_dump()
            if row is None:
                db.add(ModelRegistryEntry(model_id=entry.model_id, entry=data, observed={}))
            elif row.entry != data:
                row.entry = data


def default_providers(endpoints: list[EndpointConfig] | None = None) -> dict[str, ModelProvider]:
    """One provider per configured endpoint. Unconfigured endpoints get no provider."""
    s = get_settings()
    endpoints = endpoints if endpoints is not None else parse_endpoints(None, s.model_endpoint_url)
    providers: dict[str, ModelProvider] = {"mock": MockProvider()}
    for ep in endpoints:
        url = ep.resolved_url()
        key = ep.resolved_key()
        if ep.api == "anthropic":
            if key:
                providers[f"anthropic:{ep.name}"] = AnthropicProvider(key, url, name=ep.name)
            continue
        if not url:
            continue
        if ep.name == DEFAULT_ENDPOINT and not key:
            key = s.model_endpoint_api_key  # may come from .env rather than the process environment
        providers[f"openai_compatible:{ep.name}"] = OpenAICompatibleProvider(
            url,
            key,
            auth=ep.auth,
            headers=ep.headers,
            query=ep.query,
            name=ep.name,
            schema_mode=ep.structured_output,
            stream=ep.stream,
        )
    return providers


@lru_cache
def get_registry() -> ModelRegistry:
    return ModelRegistry.from_file(get_settings().model_registry_path)


def record_observation(db: Session, model_id: str, *, success: bool, latency_ms: int) -> None:
    """Update observed reliability statistics used by the router."""
    row = db.scalar(select(ModelRegistryEntry).where(ModelRegistryEntry.model_id == model_id))
    if row is None:
        return
    obs: dict[str, Any] = dict(row.observed or {})
    calls = int(obs.get("calls", 0)) + 1
    successes = int(obs.get("successes", 0)) + (1 if success else 0)
    total_latency = int(obs.get("total_latency_ms", 0)) + latency_ms
    row.observed = {
        "calls": calls,
        "successes": successes,
        "failures": calls - successes,
        "success_rate": round(successes / calls, 4),
        "total_latency_ms": total_latency,
        "mean_latency_ms": round(total_latency / calls, 1),
    }
