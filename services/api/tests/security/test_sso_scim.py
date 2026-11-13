"""Single sign-on (OIDC code flow + PKCE) and SCIM provisioning, against a real signing IdP."""

from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import EMAILS, H
from tests.idp_server import _key
from tests.security.conftest import sign_in


def me(client: TestClient, token: str) -> httpx.Response:
    return client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


def test_sign_in_issues_a_session_and_maps_groups_to_roles(
    client: TestClient, project: dict, idp: Any
) -> None:
    fake = idp(
        group_roles=[
            {"group": "payments-engineers", "project": project["key"], "role": "engineer"},
            {"group": "asw-admins", "admin": True},
        ]
    )
    path, params = sign_in(client, return_to="/projects/x?tab=1")
    assert path == "/auth/callback" and params["return_to"] == "/projects/x?tab=1"
    token = params["token"]
    who = me(client, token).json()
    assert who["email"] == "dana@example.com" and who["display_name"] == "Dana Analyst"
    assert who["project_roles"] == {project["id"]: ["engineer"]} and who["is_admin"] is False
    # PKCE and the client secret went to the token endpoint; the ID token never reaches the browser.
    req = fake.token_requests[-1]
    assert req["form"]["code_verifier"] and req["authorization"].startswith("Basic ")
    assert "id_token" not in params

    # Group removed at the IdP: the next sign-in removes the managed role; admin group adds admin.
    fake.user["groups"] = ["asw-admins"]
    _, again = sign_in(client)
    who = me(client, again["token"]).json()
    assert who["project_roles"] == {} and who["is_admin"] is True
    # Same IdP subject, same user.
    assert who["id"] == me(client, token).json()["id"]

    # Logging out revokes the session.
    assert (
        client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {again['token']}"}).status_code
        == 204
    )
    assert me(client, again["token"]).status_code == 401
    from sqlalchemy import select

    from agentic_sdlc.core.db import session_scope
    from agentic_sdlc.orm import AuditEvent

    with session_scope() as db:
        logins = db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.sso_login")).all()
        assert any(e.details.get("subject") == "u-123" and e.details["provider"] == "corp" for e in logins)
        synced = db.scalars(select(AuditEvent).where(AuditEvent.action == "user.roles_synced")).all()
        assert any("admin" in e.details["granted"] for e in synced)


def test_group_sync_never_touches_roles_granted_by_hand(client: TestClient, project: dict, idp: Any) -> None:
    fake = idp(group_roles=[{"group": "payments-engineers", "project": project["key"], "role": "engineer"}])
    _, p = sign_in(client)
    r = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "dana@example.com", "role": "reviewer"},
        headers=H("owner"),
    )
    assert r.status_code == 201, r.text
    fake.user["groups"] = []
    _, p = sign_in(client)
    assert me(client, p["token"]).json()["project_roles"] == {project["id"]: ["reviewer"]}


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ({"aud": "someone-else"}, "ID token rejected"),
        ({"iss": "https://evil.example.com"}, "ID token rejected"),
        ({"exp": int(time.time()) - 3600}, "ID token rejected"),
        ({"nonce": "replayed"}, "nonce"),
        ({"email_verified": False}, "not verified"),
        ({"email": "mallory@other.org"}, "organisation is not allowed"),
        ({"aud": ["asw-web", "other"], "azp": "other"}, "different client"),
    ],
)
def test_invalid_id_tokens_are_refused(client: TestClient, idp: Any, tamper: dict, reason: str) -> None:
    fake = idp(allowed_email_domains=["example.com"])
    fake.token_overrides = tamper
    path, params = sign_in(client)
    assert path == "/" and "token" not in params
    assert reason in params["sso_error"], params


def test_forged_signature_is_refused(client: TestClient, idp: Any) -> None:
    fake = idp()
    fake.sign_with = _key()
    _, params = sign_in(client)
    assert "token" not in params and "Signature verification failed" in params["sso_error"]


def test_state_is_single_use_and_bound_to_the_flow(client: TestClient, idp: Any) -> None:
    idp()
    r = client.get("/api/v1/auth/oidc/corp/login", follow_redirects=False)
    at_idp = httpx.get(r.headers["location"], follow_redirects=False)
    back = urlsplit(at_idp.headers["location"])
    first = client.get(f"{back.path}?{back.query}", follow_redirects=False)
    assert "#token=" in first.headers["location"]
    replay = client.get(f"{back.path}?{back.query}", follow_redirects=False)
    assert (
        "sso_error" in replay.headers["location"]
        and "already used" in parse_qs(urlsplit(replay.headers["location"]).query)["sso_error"][0]
    )
    forged = client.get(f"{back.path}?code=x&state=made-up", follow_redirects=False)
    assert (
        "invalid or was already used" in parse_qs(urlsplit(forged.headers["location"]).query)["sso_error"][0]
    )


@pytest.mark.parametrize(
    "target", ["https://evil.example.com/", "//evil.example.com", "/\\evil", "javascript:x"]
)
def test_return_to_cannot_leave_the_app(client: TestClient, idp: Any, target: str) -> None:
    idp()
    _, params = sign_in(client, return_to=target)
    assert params["return_to"] == "/"


def test_existing_accounts_are_not_taken_over_by_email(client: TestClient, idp: Any) -> None:
    fake = idp()
    fake.user["email"] = EMAILS["engineer"]  # a seeded user (no SSO link)
    fake.user["sub"] = "attacker-sub"
    _, params = sign_in(client)
    assert "token" not in params and "administrator must link" in params["sso_error"]
    idp(link_by_email=True)
    _, params = sign_in(client)
    assert me(client, params["token"]).json()["email"] == EMAILS["engineer"]


def test_no_auto_provisioning_and_disabled_users(client: TestClient, idp: Any) -> None:
    fake = idp(auto_provision=False)
    fake.user.update(sub="new-sub", email="newbie@example.com")
    _, params = sign_in(client)
    assert "not been given access" in params["sso_error"]


def test_providers_are_listed_publicly_without_secrets(client: TestClient, idp: Any) -> None:
    idp()
    body = client.get("/api/v1/auth/providers").json()
    assert body["providers"] == [
        {"name": "corp", "display_name": "Corp SSO", "login_url": "/api/v1/auth/oidc/corp/login"}
    ]
    assert "secret" not in str(body).lower()
    assert client.get("/api/v1/auth/oidc/nope/login", follow_redirects=False).status_code == 404


# --- SCIM ---------------------------------------------------------------------------------------------------


@pytest.fixture
def scim(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "scim-test-token")
    return {"Authorization": "Bearer scim-test-token", "Content-Type": "application/scim+json"}


def test_scim_requires_its_token_and_is_off_without_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCIM_BEARER_TOKEN", raising=False)
    assert client.get("/scim/v2/Users").status_code == 404
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "right")
    r = client.get("/scim/v2/Users", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    # A platform API token is not a SCIM credential.
    assert client.get("/scim/v2/Users", headers=H("admin")).status_code == 401


def test_scim_user_lifecycle_and_group_roles(
    client: TestClient, project: dict, idp: Any, scim: dict[str, str]
) -> None:
    idp(group_roles=[{"group": "Payments QA", "project": project["key"], "role": "qa"}])
    email = f"sam-{uuid.uuid4().hex[:6]}@example.com"
    r = client.post(
        "/scim/v2/Users",
        headers=scim,
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": email,
            "name": {"givenName": "Sam", "familyName": "Tester"},
            "externalId": "ext-1",
            "active": True,
        },
    )
    assert r.status_code == 201, r.text
    user = r.json()
    assert user["userName"] == email and user["displayName"] == "Sam Tester" and user["active"] is True
    assert client.post("/scim/v2/Users", headers=scim, json={"userName": email}).status_code == 409
    found = client.get(f'/scim/v2/Users?filter=userName eq "{email.upper()}"', headers=scim).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == user["id"]
    assert client.get('/scim/v2/Users?filter=name sw "x"', headers=scim).status_code == 400

    g = client.post(
        "/scim/v2/Groups",
        headers=scim,
        json={"displayName": "Payments QA", "members": [{"value": user["id"]}]},
    )
    assert g.status_code == 201, g.text
    gid = g.json()["id"]
    roles = client.get(f"/api/v1/projects/{project['id']}/members", headers=H("owner")).json()
    assert any(m["email"] == email and m["role"] == "qa" for m in roles)

    # The user signs in later through SSO: the SCIM-managed groups are kept, not the token claim.
    from agentic_sdlc.core.db import session_scope
    from agentic_sdlc.orm import ApiToken, ExternalIdentity, User

    with session_scope() as db:
        db.add(ExternalIdentity(provider="corp", subject="sam-sub", user_id=uuid.UUID(user["id"])))
    fake = idp(group_roles=[{"group": "Payments QA", "project": project["key"], "role": "qa"}])
    fake.user.update(sub="sam-sub", email=email, groups=["something-else"])
    _, p = sign_in(client)
    assert me(client, p["token"]).json()["project_roles"] == {project["id"]: ["qa"]}

    # Removed from the group: role goes. Deactivated: session revoked immediately.
    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "remove", "path": f'members[value eq "{user["id"]}"]'}],
    }
    assert client.patch(f"/scim/v2/Groups/{gid}", headers=scim, json=patch).status_code == 200
    roles = client.get(f"/api/v1/projects/{project['id']}/members", headers=H("owner")).json()
    assert not any(m["email"] == email for m in roles)
    off = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "value": {"active": False}}],
    }
    r = client.patch(f"/scim/v2/Users/{user['id']}", headers=scim, json=off)
    assert r.status_code == 200 and r.json()["active"] is False
    assert me(client, p["token"]).status_code == 401
    _, denied = sign_in(client)
    assert "disabled" in denied["sso_error"]
    with session_scope() as db:
        u = db.get(User, uuid.UUID(user["id"]))
        assert u is not None and not u.is_active
        assert all(t.revoked_at is not None for t in db.query(ApiToken).filter(ApiToken.user_id == u.id))

    # DELETE never hard-deletes (approvals and audit keep their author).
    assert client.delete(f"/scim/v2/Users/{user['id']}", headers=scim).status_code == 204
    assert client.get(f"/scim/v2/Users/{user['id']}", headers=scim).json()["active"] is False
    assert client.delete(f"/scim/v2/Groups/{gid}", headers=scim).status_code == 204
    assert client.get(f"/scim/v2/Groups/{gid}", headers=scim).status_code == 404


def test_scim_discovery(client: TestClient, scim: dict[str, str]) -> None:
    spc = client.get("/scim/v2/ServiceProviderConfig", headers=scim)
    assert spc.status_code == 200 and spc.headers["content-type"].startswith("application/scim+json")
    assert spc.json()["patch"]["supported"] is True
    assert client.get("/scim/v2/ResourceTypes", headers=scim).json()["totalResults"] == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"dev_seed_users": True}, "dev_seed_users"),
        ({"public_base_url": "http://api.example.com"}, "public_base_url must be https"),
        ({"web_base_url": "http://app.example.com"}, "web_base_url must be https"),
        ({"oidc_insecure_http_issuers": ["http://idp"]}, "oidc_insecure_http_issuers must be empty"),
        ({"git_insecure_http_hosts": ["127.0.0.1"]}, "git_insecure_http_hosts must be empty"),
    ],
)
def test_production_refuses_insecure_identity_and_git_settings(overrides: dict, message: str) -> None:
    from agentic_sdlc.core.settings import Settings

    safe = {
        "app_env": "production",
        "dev_seed_users": False,
        "public_base_url": "https://api.example.com",
        "web_base_url": "https://app.example.com",
    }
    Settings(**safe)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message):
        Settings(**{**safe, **overrides})  # type: ignore[arg-type]
