"""Connections to the customer's work tools: Jira, Azure Boards, Confluence, Slack, Microsoft Teams.

Connections are declared by the operator (a YAML file or JSON setting), never by project users;
projects only choose which declared connection to use. Credentials come from environment
variables named in the declaration and are never stored in the database or returned by the API.

    connections:
      - name: jira
        kind: jira                      # jira | azure_boards | confluence | slack | teams
        base_url: https://acme.atlassian.net
        auth: basic                     # basic (email + API token) | bearer (PAT) | none
        username_env: JIRA_EMAIL
        token_env: JIRA_API_TOKEN
        api_version: 3                  # Jira Cloud 3 (ADF descriptions) | Data Center 2 (text)
      - name: boards
        kind: azure_boards
        base_url: https://dev.azure.com/acme
        auth: pat
        token_env: ADO_PAT
      - name: wiki
        kind: confluence
        base_url: https://acme.atlassian.net/wiki
        auth: basic
        username_env: JIRA_EMAIL
        token_env: JIRA_API_TOKEN
      - name: eng-alerts
        kind: slack                     # or teams: an incoming / Workflows webhook URL
        webhook_url_env: SLACK_WEBHOOK_URL

Requests go only to the connection's own host (no redirects), errors never echo credentials, and
plain http is refused unless the host is listed in INTEGRATIONS_INSECURE_HTTP_HOSTS (tests).
"""

from __future__ import annotations

import base64
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.core.settings import get_settings

Kind = Literal["jira", "azure_boards", "confluence", "slack", "teams"]


class IntegrationError(Exception):
    """A call to a work tool failed. `retryable` is False for errors a retry cannot fix."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class HttpEndpoint(BaseModel):
    """A declared HTTP service with credentials from the environment. Shared by work-tool
    connections and deployment targets: same host pinning, https and error rules."""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str | None = None
    auth: Literal["basic", "bearer", "pat", "none"] = "none"
    username_env: str | None = None
    token_env: str | None = None
    timeout_s: float = 30.0

    def _check_auth(self) -> None:
        if self.auth == "basic" and not (self.username_env and self.token_env):
            raise ValueError("auth: basic needs username_env and token_env")
        if self.auth in ("bearer", "pat") and not self.token_env:
            raise ValueError(f"auth: {self.auth} needs token_env")

    def _check_base_url(self, what: str) -> None:
        if not self.base_url:
            raise ValueError(f"{what} needs base_url")
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url.startswith("https://") and not _insecure_ok(self.base_url):
            raise ValueError("base_url must use https")

    def credential_available(self) -> bool:
        envs = [e for e in (self.username_env, self.token_env) if e]
        return all(os.environ.get(e) for e in envs)

    def _headers(self) -> dict[str, str]:
        if self.auth == "basic":
            raw = f"{os.environ.get(self.username_env or '', '')}:{os.environ.get(self.token_env or '', '')}"
            return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}
        if self.auth == "pat":  # Azure DevOps: empty user name, PAT as password
            raw = f":{os.environ.get(self.token_env or '', '')}"
            return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}
        if self.auth == "bearer":
            return {"Authorization": f"Bearer {os.environ.get(self.token_env or '', '')}"}
        return {}

    def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request to this endpoint's own host; raises IntegrationError on failure."""
        if not self.credential_available():
            raise IntegrationError(
                f"{self.name}: credentials are not configured on this server", retryable=False
            )
        assert self.base_url
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        if urlsplit(url).hostname != urlsplit(self.base_url).hostname:
            raise IntegrationError(f"{self.name}: refusing to call another host", retryable=False)
        headers = {"Accept": "application/json", **self._headers(), **kwargs.pop("headers", {})}
        try:
            with httpx.Client(timeout=self.timeout_s, follow_redirects=False) as client:
                r = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"{self.name}: {type(exc).__name__}") from None
        _result(self.name, r)
        return r

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return _result(self.name, self.send(method, path, **kwargs))


class WorkToolConnection(HttpEndpoint):
    kind: Kind
    webhook_url_env: str | None = None
    api_version: Literal[2, 3] = 3

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", v):
            raise ValueError("name: lowercase letters, digits, - and _")
        return v

    @model_validator(mode="after")
    def _shape(self) -> WorkToolConnection:
        if self.kind in ("slack", "teams"):
            if not self.webhook_url_env:
                raise ValueError(f"{self.kind} connections need webhook_url_env")
            return self
        self._check_base_url(f"{self.kind} connections")
        self._check_auth()
        return self

    def credential_available(self) -> bool:
        if self.kind in ("slack", "teams"):
            return bool(os.environ.get(self.webhook_url_env or ""))
        return super().credential_available()

    def public(self) -> dict[str, Any]:
        host = urlsplit(self.base_url).hostname if self.base_url else None
        return {
            "name": self.name,
            "kind": self.kind,
            "host": host,
            "auth": self.auth,
            "credential_available": self.credential_available(),
        }

    def post_webhook(self, body: dict[str, Any]) -> None:
        url = os.environ.get(self.webhook_url_env or "", "")
        if not url:
            raise IntegrationError(
                f"{self.name}: webhook URL is not configured on this server", retryable=False
            )
        if not url.startswith("https://") and not _insecure_ok(url):
            raise IntegrationError(f"{self.name}: webhook URL must use https", retryable=False)
        try:
            with httpx.Client(timeout=self.timeout_s, follow_redirects=False) as client:
                r = client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"{self.name}: {type(exc).__name__}") from None
        _result(self.name, r)


def _result(name: str, r: httpx.Response) -> Any:
    if r.status_code >= 300:
        # The body may describe the problem (missing field, no permission); it never holds our credentials.
        detail = re.sub(r"\s+", " ", r.text)[:300]
        retryable = r.status_code in (408, 409, 425, 429) or r.status_code >= 500
        raise IntegrationError(
            f"{name}: HTTP {r.status_code}: {detail}", retryable=retryable, status=r.status_code
        )
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


def _insecure_ok(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return url.startswith("http://") and host in get_settings().integrations_insecure_http_hosts


@lru_cache(maxsize=1)
def load_connections() -> dict[str, WorkToolConnection]:
    s = get_settings()
    raw: list[dict[str, Any]] = []
    if s.integrations_file:
        raw += (yaml.safe_load(Path(s.integrations_file).read_text()) or {}).get("connections", [])
    if s.integrations:
        raw += json.loads(s.integrations)
    conns = [WorkToolConnection.model_validate(c) for c in raw]
    if len({c.name for c in conns}) != len(conns):
        raise ValueError("duplicate integration connection name")
    return {c.name: c for c in conns}


def get_connection(name: str | None, *, kinds: tuple[str, ...]) -> WorkToolConnection:
    conns = load_connections()
    conn = conns.get(name or "")
    if conn is None or conn.kind not in kinds:
        raise ValidationFailed(
            "unknown integration connection",
            details={"connection": name, "available": sorted(n for n, c in conns.items() if c.kind in kinds)},
        )
    return conn
