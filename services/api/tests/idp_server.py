"""A minimal OpenID Connect provider for tests: discovery, authorize, token (with PKCE), JWKS.

ID tokens are really signed (RS256) with a key generated per test, so signature, issuer, audience,
nonce and expiry validation are exercised for real. `token_overrides` lets a test tamper with the
next ID token (wrong audience, wrong nonce, expired, signed with a different key...).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tests.model_server import _free_port


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class FakeIdP:
    client_id: str = "asw-web"
    client_secret: str = "idp-client-secret"
    key: Any = field(default_factory=_key)
    kid: str = "k1"
    url: str = ""
    # The signed-in user the next /authorize issues a code for.
    user: dict[str, Any] = field(
        default_factory=lambda: {
            "sub": "u-123",
            "email": "dana@example.com",
            "email_verified": True,
            "name": "Dana Analyst",
            "groups": ["payments-engineers"],
        }
    )
    token_overrides: dict[str, Any] = field(default_factory=dict)
    sign_with: Any = None  # a different private key to forge signatures
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_requests: list[dict[str, Any]] = field(default_factory=list)

    async def discovery(self, request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self.url,
                "authorization_endpoint": f"{self.url}/authorize",
                "token_endpoint": f"{self.url}/token",
                "jwks_uri": f"{self.url}/jwks",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def jwks(self, request: Request) -> Response:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return JSONResponse({"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]})

    async def authorize(self, request: Request) -> Response:
        q = request.query_params
        assert q["response_type"] == "code" and q["client_id"] == self.client_id
        assert q["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": q["code_challenge"],
            "nonce": q["nonce"],
            "redirect_uri": q["redirect_uri"],
            "user": dict(self.user),
        }
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", 302)

    async def token(self, request: Request) -> Response:
        form = dict(await request.form())
        self.token_requests.append({"form": form, "authorization": request.headers.get("authorization")})
        auth = request.headers.get("authorization", "")
        expected = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if auth != expected and form.get("client_secret") != self.client_secret:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        grant = self.codes.pop(str(form.get("code")), None)  # codes are single use
        if grant is None or form.get("redirect_uri") != grant["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        verifier = str(form.get("code_verifier", ""))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != grant["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
        now = int(time.time())
        claims = {
            "iss": self.url,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": grant["nonce"],
            **grant["user"],
            **self.token_overrides,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        id_token = jwt.encode(
            claims, self.sign_with or self.key, algorithm="RS256", headers={"kid": self.kid}
        )
        return JSONResponse({"access_token": "at", "token_type": "Bearer", "id_token": id_token})

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self.discovery),
                Route("/jwks", self.jwks),
                Route("/authorize", self.authorize),
                Route("/token", self.token, methods=["POST"]),
            ]
        )


@contextmanager
def serve_idp(idp: FakeIdP) -> Iterator[FakeIdP]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(idp.app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake IdP did not start")
        time.sleep(0.02)
    idp.url = f"http://127.0.0.1:{port}"
    try:
        yield idp
    finally:
        server.should_exit = True
        thread.join(timeout=10)
