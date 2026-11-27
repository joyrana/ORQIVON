"""Signing out of a fixed development account must not lock it out; seeding repairs older lockouts."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from agentic_sdlc.auth.service import hash_token
from agentic_sdlc.core.db import session_scope, utcnow
from agentic_sdlc.orm import ApiToken
from agentic_sdlc.seed import seed_dev_users
from tests.conftest import H


def test_sign_out_keeps_development_token_usable(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout", headers=H("auditor")).status_code == 204
    assert client.get("/api/v1/me", headers=H("auditor")).status_code == 200


def test_seed_restores_a_revoked_development_token(client: TestClient) -> None:
    with session_scope() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token("dev_qa")))
        assert row is not None
        row.revoked_at = utcnow()
    assert client.get("/api/v1/me", headers=H("qa")).status_code == 401
    with session_scope() as db:
        seed_dev_users(db)
    assert client.get("/api/v1/me", headers=H("qa")).status_code == 200
