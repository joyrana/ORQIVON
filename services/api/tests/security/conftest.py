"""Fixtures shared by the security tests: a real signing OIDC provider and the sign-in flow."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agentic_sdlc.auth import identity, oidc
from agentic_sdlc.core.settings import get_settings
from tests.idp_server import FakeIdP, serve_idp

WEB = "http://web.test"


@pytest.fixture
def idp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """An IdP plus an identity config; `configure(**provider_overrides, group_roles=[...])`."""
    s = get_settings()
    saved = (s.identity_config_file, s.public_base_url, s.web_base_url, list(s.oidc_insecure_http_issuers))
    with serve_idp(FakeIdP()) as fake:
        s.public_base_url, s.web_base_url = "http://testserver", WEB
        s.oidc_insecure_http_issuers = ["http://127.0.0.1"]
        monkeypatch.setenv("TEST_OIDC_SECRET", fake.client_secret)

        def configure(group_roles: list[dict[str, Any]] | None = None, **overrides: Any) -> FakeIdP:
            path = tmp_path / f"identity-{uuid.uuid4().hex[:6]}.yaml"
            provider = {
                "name": "corp",
                "display_name": "Corp SSO",
                "issuer": fake.url,
                "client_id": fake.client_id,
                "client_secret_env": "TEST_OIDC_SECRET",
                **overrides,
            }
            path.write_text(yaml.safe_dump({"oidc_providers": [provider], "group_roles": group_roles or []}))
            s.identity_config_file = str(path)
            identity.identity_config.cache_clear()
            oidc._discovery.clear()
            oidc._jwks.clear()
            return fake

        configure()
        yield configure
    s.identity_config_file, s.public_base_url, s.web_base_url, s.oidc_insecure_http_issuers = saved
    identity.identity_config.cache_clear()
    oidc._discovery.clear()
    oidc._jwks.clear()


def sign_in(client: TestClient, return_to: str = "/projects") -> tuple[str, dict[str, str]]:
    """Drive the browser redirects. Returns (final web URL path, fragment or query params)."""
    r = client.get(f"/api/v1/auth/oidc/corp/login?return_to={return_to}", follow_redirects=False)
    assert r.status_code == 302, r.text
    at_idp = httpx.get(r.headers["location"], follow_redirects=False)
    assert at_idp.status_code == 302
    back = urlsplit(at_idp.headers["location"])
    assert back.path == "/api/v1/auth/oidc/corp/callback"
    done = client.get(f"{back.path}?{back.query}", follow_redirects=False)
    assert done.status_code == 302
    final = urlsplit(done.headers["location"])
    assert f"{final.scheme}://{final.netloc}" == WEB
    params = parse_qs(final.fragment or final.query)
    return final.path, {k: v[0] for k, v in params.items()}
