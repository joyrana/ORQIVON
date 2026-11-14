"""OpenID Connect sign-in: Authorization Code flow with PKCE, handled entirely by the backend.

1. `/auth/oidc/{provider}/login` stores a single-use state (with nonce and PKCE verifier) and
   redirects to the provider.
2. `/auth/oidc/{provider}/callback` checks the state, exchanges the code (with the verifier and the
   client secret), and validates the ID token: signature against the provider's JWKS, `iss`,
   `aud`/`azp`, `exp`/`iat`/`nbf` within the allowed clock skew, and the `nonce`.
3. The user is found by (provider, subject), never by email alone unless `link_by_email` is set;
   email must be verified and in an allowed domain; disabled users are refused.
4. Groups from the token update the user's directory-managed roles (unless SCIM manages them).
5. The browser gets a short-lived platform session token in the URL *fragment* (never sent to
   servers or written to access logs) and authorization works exactly as for API tokens.

ID tokens and provider access tokens are never stored or returned.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.audit import service as audit
from agentic_sdlc.auth.directory import new_user, sync_roles
from agentic_sdlc.auth.identity import OidcProvider, identity_config
from agentic_sdlc.auth.service import hash_token, issue_token
from agentic_sdlc.core.db import utcnow
from agentic_sdlc.core.errors import NotFound, Unauthorized
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.orm import ExternalIdentity, OidcLoginState, User

STATE_TTL = timedelta(minutes=10)
ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


class SignInDenied(Unauthorized):
    """A sign-in was refused for a reason the user can act on (shown on the sign-in page)."""


_cache_lock = threading.Lock()
_discovery: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks: dict[str, tuple[float, dict[str, Any]]] = {}


def _http() -> httpx.Client:
    return httpx.Client(timeout=10, follow_redirects=False)


def discovery(p: OidcProvider) -> dict[str, Any]:
    with _cache_lock:
        hit = _discovery.get(p.issuer)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
    r = _http().get(f"{p.issuer}/.well-known/openid-configuration")
    if r.status_code != 200:
        raise SignInDenied(f"identity provider discovery failed (HTTP {r.status_code})")
    doc = r.json()
    if doc.get("issuer", "").rstrip("/") != p.issuer:
        raise SignInDenied("identity provider discovery returned a different issuer")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not str(doc.get(key, "")).startswith(("https://", *get_settings().oidc_insecure_http_issuers)):
            raise SignInDenied(f"identity provider metadata is missing a secure {key}")
    with _cache_lock:
        _discovery[p.issuer] = (time.monotonic(), doc)
    return dict(doc)


def _keys(p: OidcProvider, *, refresh: bool = False) -> dict[str, Any]:
    uri = discovery(p)["jwks_uri"]
    with _cache_lock:
        hit = _jwks.get(uri)
        if hit and not refresh and time.monotonic() - hit[0] < 3600:
            return hit[1]
    r = _http().get(uri)
    if r.status_code != 200:
        raise SignInDenied("could not fetch the identity provider's signing keys")
    keys = r.json()
    with _cache_lock:
        _jwks[uri] = (time.monotonic(), keys)
    return dict(keys)


def _signing_key(p: OidcProvider, token: str) -> Any:
    header = jwt.get_unverified_header(token)
    if header.get("alg") not in ALLOWED_ALGS:
        raise SignInDenied("ID token uses a disallowed signing algorithm")
    kid = header.get("kid")
    for attempt in (False, True):  # unknown kid: the provider may have rotated keys
        for k in _keys(p, refresh=attempt).get("keys", []):
            if kid is None or k.get("kid") == kid:
                return jwt.PyJWK(k).key
    raise SignInDenied("ID token is signed with an unknown key")


def validate_id_token(p: OidcProvider, token: str, nonce: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            _signing_key(p, token),
            algorithms=ALLOWED_ALGS,
            audience=p.client_id,
            issuer=p.issuer,
            leeway=p.clock_skew_s,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise SignInDenied(f"ID token rejected: {exc}") from exc
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(aud) > 1 and claims.get("azp") != p.client_id:
        raise SignInDenied("ID token was issued to a different client (azp)")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInDenied("ID token nonce does not match this sign-in")
    return claims


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def redirect_uri(p: OidcProvider) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/auth/oidc/{p.name}/callback"


def _safe_return_to(value: str | None) -> str:
    """Only same-origin paths: an attacker must not be able to bounce the session elsewhere."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    parts = urlsplit(value)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def provider_or_404(name: str) -> OidcProvider:
    p = identity_config().provider(name)
    if p is None:
        raise NotFound("unknown sign-in provider")
    return p


def begin_login(db: Session, provider: str, return_to: str | None) -> str:
    p = provider_or_404(provider)
    meta = discovery(p)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    db.add(
        OidcLoginState(
            state_hash=hash_token(state),
            provider=p.name,
            nonce=nonce,
            code_verifier=verifier,
            return_to=_safe_return_to(return_to),
            expires_at=utcnow() + STATE_TTL,
        )
    )
    query = {
        "response_type": "code",
        "client_id": p.client_id,
        "redirect_uri": redirect_uri(p),
        "scope": " ".join(p.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    return f"{meta['authorization_endpoint']}?{urlencode(query)}"


def _exchange(p: OidcProvider, code: str, verifier: str) -> dict[str, Any]:
    import os

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(p),
        "code_verifier": verifier,
        "client_id": p.client_id,
    }
    auth: tuple[str, str] | None = None
    if p.client_auth != "none":
        secret = os.environ.get(p.client_secret_env or "")
        if not secret:
            raise SignInDenied("sign-in is misconfigured: the client secret is not available")
        if p.client_auth == "client_secret_basic":
            auth = (p.client_id, secret)
        else:
            data["client_secret"] = secret
    r = _http().post(
        discovery(p)["token_endpoint"],
        data=data,
        auth=auth if auth else httpx.USE_CLIENT_DEFAULT,
        headers={"Accept": "application/json"},
    )
    if r.status_code != 200:
        raise SignInDenied(f"the identity provider refused the sign-in code (HTTP {r.status_code})")
    body: dict[str, Any] = r.json()
    if "id_token" not in body:
        raise SignInDenied("the identity provider returned no ID token")
    return body


def complete_login(
    db: Session, provider: str, code: str | None, state: str | None, error: str | None
) -> tuple[str, str]:
    """Returns (platform session token, return_to)."""
    p = provider_or_404(provider)
    if error:
        raise SignInDenied(f"the identity provider reported: {error[:100]}")
    if not code or not state:
        raise SignInDenied("missing code or state")
    row = db.scalar(
        select(OidcLoginState).where(OidcLoginState.state_hash == hash_token(state)).with_for_update()
    )
    if row is None or row.provider != p.name:
        raise SignInDenied("this sign-in link is invalid or was already used")
    db.delete(row)  # single use, even if the rest fails
    db.flush()
    if row.expires_at < utcnow():
        raise SignInDenied("this sign-in took too long; start again")
    tokens = _exchange(p, code, row.code_verifier)
    claims = validate_id_token(p, tokens["id_token"], row.nonce)
    user = _resolve_user(db, p, claims)
    session = issue_token(
        db, user, name=f"sso:{p.name}", ttl_days=None, ttl=timedelta(hours=get_settings().sso_session_hours)
    )
    audit.record(
        db,
        action="auth.sso_login",
        actor_id=user.id,
        actor_type="human",
        target_type="user",
        target_id=user.id,
        details={"provider": p.name, "subject": claims["sub"]},
    )
    return session, row.return_to


def _resolve_user(db: Session, p: OidcProvider, claims: dict[str, Any]) -> User:
    email = str(claims.get(p.email_claim) or "").strip().lower()
    if p.require_email_verified and claims.get("email_verified") is not True:
        raise SignInDenied("your email address is not verified with the identity provider")
    if not email or "@" not in email:
        raise SignInDenied("the identity provider did not supply an email address")
    domain = email.rsplit("@", 1)[1]
    if p.allowed_email_domains and domain not in {d.lower() for d in p.allowed_email_domains}:
        raise SignInDenied("your organisation is not allowed to sign in here")
    link = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == p.name, ExternalIdentity.subject == claims["sub"]
        )
    )
    user = db.get(User, link.user_id) if link else None
    if user is None:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            if not p.link_by_email:
                raise SignInDenied(
                    "an account with this email already exists; "
                    "an administrator must link it to single sign-on"
                )
            user = existing
        elif p.auto_provision:
            user = new_user(db, email=email, display_name=str(claims.get(p.name_claim) or email))
        else:
            raise SignInDenied("you have not been given access to this workspace")
        db.add(ExternalIdentity(provider=p.name, subject=str(claims["sub"]), user_id=user.id))
    if not user.is_active:
        raise SignInDenied("your account is disabled")
    if user.is_service_account:
        raise SignInDenied("service accounts cannot sign in")
    name = claims.get(p.name_claim)
    if isinstance(name, str) and name.strip():
        user.display_name = name.strip()[:200]
    if p.groups_claim and not user.scim_managed:
        raw = claims.get(p.groups_claim) or []
        user.groups = sorted({str(g) for g in raw})[:500] if isinstance(raw, list) else []
        sync_roles(db, user, actor=f"oidc:{p.name}")
    return user


def cleanup_expired_states(db: Session) -> int:
    rows = db.scalars(select(OidcLoginState).where(OidcLoginState.expires_at < utcnow())).all()
    for r in rows:
        db.delete(r)
    return len(rows)


def pkce_challenge(verifier: str) -> str:
    """Exposed for tests: the S256 challenge for a verifier."""
    return _b64(hashlib.sha256(verifier.encode()).digest())
