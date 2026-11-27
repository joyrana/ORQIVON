"""Single sign-on endpoints (public: they establish the session)."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Header, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from agentic_sdlc.api.deps import DB
from agentic_sdlc.auth import oidc
from agentic_sdlc.auth.identity import identity_config
from agentic_sdlc.auth.service import authenticate
from agentic_sdlc.core.db import utcnow
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.orm import ApiToken

router = APIRouter(tags=["auth"])


class SignInProvider(BaseModel):
    name: str
    display_name: str
    login_url: str


class SignInOptions(BaseModel):
    providers: list[SignInProvider]
    token_sign_in: bool  # API tokens can be pasted (automation, break-glass)


@router.get("/auth/providers", response_model=SignInOptions)
def providers() -> SignInOptions:
    return SignInOptions(
        providers=[
            SignInProvider(
                name=p.name, display_name=p.display_name, login_url=f"/api/v1/auth/oidc/{p.name}/login"
            )
            for p in identity_config().oidc_providers
        ],
        token_sign_in=True,
    )


def _web(path: str, *, fragment: dict[str, str] | None = None, query: dict[str, str] | None = None) -> str:
    url = get_settings().web_base_url.rstrip("/") + path
    if query:
        url += "?" + urlencode(query)
    if fragment:
        url += "#" + urlencode(fragment, quote_via=quote)
    return url


@router.get("/auth/oidc/{provider}/login")
def login(
    provider: str, db: DB, return_to: Annotated[str | None, Query(max_length=500)] = None
) -> RedirectResponse:
    try:
        url = oidc.begin_login(db, provider, return_to)
    except oidc.SignInDenied as exc:
        return RedirectResponse(_web("/", query={"sso_error": exc.message}), status_code=302)
    db.commit()
    return RedirectResponse(url, status_code=302)


@router.get("/auth/oidc/{provider}/callback")
def callback(
    provider: str,
    db: DB,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    from agentic_sdlc.audit import service as audit

    try:
        session, return_to = oidc.complete_login(db, provider, code, state, error)
    except oidc.SignInDenied as exc:
        db.rollback()
        audit.record(
            db,
            action="auth.sso_denied",
            actor_id=None,
            actor_type="system",
            target_type="sign_in",
            target_id=None,
            details={"provider": provider, "reason": exc.message[:300]},
        )
        # The consumed state must stay consumed even though sign-in failed.
        if state:
            from agentic_sdlc.auth.service import hash_token
            from agentic_sdlc.orm import OidcLoginState

            row = db.get(OidcLoginState, hash_token(state))
            if row is not None:
                db.delete(row)
        db.commit()
        return RedirectResponse(_web("/", query={"sso_error": exc.message}), status_code=302)
    db.commit()
    # The session token travels in the fragment: browsers never send it to a server or log it.
    response = RedirectResponse(
        _web("/auth/callback", fragment={"token": session, "return_to": return_to}), status_code=302
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/auth/logout", status_code=204)
def logout(db: DB, authorization: Annotated[str | None, Header()] = None) -> None:
    """Revoke the token this request is made with (signing out of an SSO session ends it).

    The fixed development tokens (dev_admin, dev_owner, ...) are not revoked: they are public, and
    revoking one would lock that development account out until the database is re-seeded.
    """
    from agentic_sdlc.seed import is_dev_token

    principal = authenticate(db, authorization)
    token = db.scalar(select(ApiToken).where(ApiToken.id == principal.token_id))
    if token is not None and token.revoked_at is None and not is_dev_token(token.token_hash):
        token.revoked_at = utcnow()
    db.commit()
