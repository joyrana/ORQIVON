"""Named model endpoints.

A deployment can serve models from several places at once, for example Qwen on an in-house vLLM
cluster and a hosted OpenAI-compatible service for larger tasks. Each registry entry names the
endpoint that serves it (`endpoint:`), and each endpoint is declared once:

```yaml
endpoints:
  - name: inhouse-vllm
    base_url_env: MODEL_ENDPOINT_URL        # or base_url: https://...
    api_key_env: MODEL_ENDPOINT_API_KEY     # secrets only ever come from the environment
    privacy: self_hosted                    # self_hosted | external
  - name: azure-openai
    base_url_env: AZURE_OPENAI_BASE_URL     # https://<resource>.openai.azure.com/openai/v1
    api_key_env: AZURE_OPENAI_API_KEY
    auth: api_key_header                    # sends `api-key: <key>` instead of a bearer token
    privacy: external
```

The `default` endpoint is always defined from `MODEL_ENDPOINT_URL` / `MODEL_ENDPOINT_API_KEY` so
existing single-endpoint configurations keep working. An endpoint whose URL is not configured is
simply absent: models on it are reported as "provider not configured" by the router.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_ENDPOINT = "default"


class EndpointConfig(BaseModel):
    name: str
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    auth: Literal["bearer", "api_key_header", "none"] = "bearer"
    privacy: Literal["self_hosted", "external"] = "self_hosted"
    headers: dict[str, str] = Field(default_factory=dict)  # non-secret extra headers only
    query: dict[str, str] = Field(default_factory=dict)  # e.g. {"api-version": "2025-04-01-preview"}
    timeout_s: float | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", v):
            raise ValueError("endpoint name must be lowercase letters, digits, '.', '_' or '-'")
        return v

    @field_validator("headers")
    @classmethod
    def _no_secrets_in_headers(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if key.lower() in {"authorization", "api-key", "x-api-key", "proxy-authorization"}:
                raise ValueError(f"header '{key}' carries a credential; use api_key_env instead")
        return v

    @model_validator(mode="after")
    def _one_url_source(self) -> EndpointConfig:
        if self.base_url and self.base_url_env:
            raise ValueError("set base_url or base_url_env, not both")
        if self.base_url and not re.match(r"^https?://", self.base_url):
            raise ValueError("base_url must be an http(s) URL")
        return self

    def resolved_url(self, env: dict[str, str] | None = None) -> str | None:
        env = env if env is not None else dict(os.environ)
        url = self.base_url or (env.get(self.base_url_env) if self.base_url_env else None)
        return url.rstrip("/") if url else None

    def resolved_key(self, env: dict[str, str] | None = None) -> str | None:
        env = env if env is not None else dict(os.environ)
        return env.get(self.api_key_env) if self.api_key_env else None

    def public(self, env: dict[str, str] | None = None) -> dict[str, Any]:
        """Description safe to show to administrators: never includes the key."""
        url = self.resolved_url(env)
        return {
            "name": self.name,
            "configured": url is not None,
            "base_url": url,
            "auth": self.auth,
            "api_key_set": bool(self.resolved_key(env)),
            "privacy": self.privacy,
        }


def default_endpoint(model_endpoint_url: str | None) -> EndpointConfig:
    return EndpointConfig(
        name=DEFAULT_ENDPOINT,
        base_url=model_endpoint_url.rstrip("/") if model_endpoint_url else None,
        api_key_env="MODEL_ENDPOINT_API_KEY",
    )


def parse_endpoints(raw: list[dict[str, Any]] | None, model_endpoint_url: str | None) -> list[EndpointConfig]:
    endpoints = [EndpointConfig.model_validate(e) for e in raw or []]
    names = [e.name for e in endpoints]
    if len(names) != len(set(names)):
        raise ValueError("duplicate endpoint name in registry")
    if DEFAULT_ENDPOINT not in names:
        endpoints.insert(0, default_endpoint(model_endpoint_url))
    elif model_endpoint_url:
        # Settings may come from `.env` rather than the process environment: fall back to them.
        i = names.index(DEFAULT_ENDPOINT)
        if endpoints[i].resolved_url() is None:
            endpoints[i] = endpoints[i].model_copy(
                update={"base_url": model_endpoint_url.rstrip("/"), "base_url_env": None}
            )
    return endpoints
