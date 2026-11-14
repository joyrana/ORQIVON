"""Offline licence keys, their enforcement, and the monthly usage report."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from agentic_sdlc.core import licensing, usage
from agentic_sdlc.core.db import session_scope
from agentic_sdlc.core.errors import PolicyViolation
from agentic_sdlc.core.settings import get_settings
from tests.conftest import H
from tests.security.conftest import sign_in

ROOT = Path(__file__).resolve().parents[4]


def _pub(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("LICENCE_PUBLIC_KEYS", _pub(key))
    return key


def _licence(
    key: Ed25519PrivateKey, *, seats: int = 100, expires: date | None = None, **extra: object
) -> str:
    today = date.today()
    return licensing.sign(
        {
            "licence_id": "LIC-TEST",
            "customer": "Acme Bank",
            "issued_at": (today - timedelta(days=30)).isoformat(),
            "expires_at": (expires or today + timedelta(days=335)).isoformat(),
            "seats": seats,
            **extra,
        },
        key,
    )


def _use(monkeypatch: pytest.MonkeyPatch, token: str | None, env: str = "production") -> None:
    s = get_settings()
    monkeypatch.setattr(s, "app_env", env)
    monkeypatch.setattr(s, "licence_file", None)
    if token is None:
        monkeypatch.setattr(s, "licence_key", None)
    else:
        from pydantic import SecretStr

        monkeypatch.setattr(s, "licence_key", SecretStr(token))


def test_signature_payload_and_tampering(vendor: Ed25519PrivateKey) -> None:
    token = _licence(vendor, seats=25, features=["sso"])
    lic = licensing.parse(token)
    assert (lic.customer, lic.seats, lic.features) == ("Acme Bank", 25, ["sso"])

    head, body, sig = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged["seats"] = 5000
    forged_body = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    for bad, reason in (
        (f"{head}.{forged_body}.{sig}", "signature"),
        (_licence(Ed25519PrivateKey.generate()), "signature"),  # someone else's key
        ("ASW1.abc", "not a licence key"),
        ("hello", "not a licence key"),
    ):
        with pytest.raises(licensing.LicenceError, match=reason):
            licensing.parse(bad)


def test_states_and_what_each_allows(vendor: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today()
    with session_scope() as db:
        used = licensing.seats_used(db)
        _use(monkeypatch, _licence(vendor, seats=used + 5))
        st = licensing.status(db)
        assert st.state == "valid" and st.can_start_runs and st.can_add_people()

        _use(monkeypatch, _licence(vendor, seats=used))  # exactly full
        st = licensing.status(db)
        assert st.state == "valid" and st.can_start_runs and not st.can_add_people()
        with pytest.raises(PolicyViolation, match="seats are in use"):
            licensing.require_seat(db)

        _use(monkeypatch, _licence(vendor, seats=used + 5, expires=today - timedelta(days=3)))
        st = licensing.status(db)
        assert st.state == "grace" and st.can_start_runs and st.days_left == -3

        _use(monkeypatch, _licence(vendor, seats=used + 5, expires=today - timedelta(days=30)))
        st = licensing.status(db)
        assert st.state == "expired" and not st.can_start_runs and not st.can_add_people()
        with pytest.raises(PolicyViolation, match="expired"):
            licensing.require_runs(db)

        _use(monkeypatch, None)
        assert licensing.status(db).state == "missing"
        _use(monkeypatch, "ASW1.garbage.sig")
        assert licensing.status(db).state == "invalid"
        # Development and test installations are not enforced without a licence.
        _use(monkeypatch, None, env="development")
        assert licensing.status(db).state == "unenforced"


def test_unlicensed_build_does_not_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LICENCE_PUBLIC_KEYS", raising=False)
    monkeypatch.setattr(licensing, "VENDOR_PUBLIC_KEYS", ())
    _use(monkeypatch, None)
    with session_scope() as db:
        st = licensing.status(db)
    assert st.state == "unenforced" and "unlicensed build" in st.detail


def test_expired_licence_stops_new_runs_but_not_reading(
    client: TestClient, project: dict, vendor: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _licence(vendor, expires=date.today() - timedelta(days=60)))
    r = client.post(
        f"/api/v1/projects/{project['id']}/runs",
        json={"repository_id": str(uuid.uuid4())},
        headers=H("owner"),
    )
    assert r.status_code == 403 and "licence" in r.json()["error"]["message"]
    assert client.get(f"/api/v1/projects/{project['id']}", headers=H("owner")).status_code == 200
    assert client.get(f"/api/v1/projects/{project['id']}/runs", headers=H("owner")).status_code == 200

    status = client.get("/api/v1/system/licence", headers=H("admin")).json()
    assert status["state"] == "expired" and status["licence"]["customer"] == "Acme Bank"
    assert client.get("/api/v1/system/licence", headers=H("owner")).status_code == 403
    # Every signed-in user learns that the licence needs attention (no customer details).
    info = client.get("/api/v1/system/info", headers=H("owner")).json()
    assert info["licence"] == {"state": "expired", "days_left": -60}


def test_full_seats_refuse_new_people_from_scim(
    client: TestClient, vendor: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "scim-test-token")
    scim = {"Authorization": "Bearer scim-test-token", "Content-Type": "application/scim+json"}
    with session_scope() as db:
        used = licensing.seats_used(db)
    _use(monkeypatch, _licence(vendor, seats=used + 1))
    ok = client.post(
        "/scim/v2/Users", headers=scim, json={"userName": f"a-{uuid.uuid4().hex[:6]}@example.com"}
    )
    assert ok.status_code == 201, ok.text
    full = client.post(
        "/scim/v2/Users", headers=scim, json={"userName": f"b-{uuid.uuid4().hex[:6]}@example.com"}
    )
    assert full.status_code == 403
    assert full.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert "seats" in full.json()["detail"]
    # Freeing a seat (deprovisioning) lets the next person in.
    uid = ok.json()["id"]
    assert (
        client.patch(
            f"/scim/v2/Users/{uid}",
            headers=scim,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "value": {"active": False}}],
            },
        ).status_code
        == 200
    )
    again = client.post(
        "/scim/v2/Users", headers=scim, json={"userName": f"c-{uuid.uuid4().hex[:6]}@example.com"}
    )
    assert again.status_code == 201
    # Reactivating someone takes a seat too.
    react = client.patch(
        f"/scim/v2/Users/{uid}",
        headers=scim,
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "value": {"active": True}}],
        },
    )
    assert react.status_code == 403


def test_usage_report_counts_without_personal_data(
    client: TestClient, project: dict, vendor: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _licence(vendor))
    month = date.today().strftime("%Y-%m")
    r = client.get(f"/api/v1/system/usage?month={month}", headers=H("admin"))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["month"] == month and rep["licence_id"] == "LIC-TEST"
    assert rep["people"]["people_active_in_month"] >= 1  # creating the project is audited
    assert rep["projects"]["total"] >= 1
    assert usage.verify(rep)
    text = json.dumps(rep)
    assert "@" not in text and project["name"] not in text and project["id"] not in text
    assert usage.verify({**rep, "delivery_runs": {"started": 0, "by_status": {}}}) is (
        rep["delivery_runs"]["started"] == 0
    )
    assert client.get(f"/api/v1/system/usage?month={month}", headers=H("owner")).status_code == 403
    assert client.get("/api/v1/system/usage?month=2026-13", headers=H("admin")).status_code == 422
    # The same report from the command line, for air-gapped installations.
    from agentic_sdlc.cli import main

    assert main(["usage-report", "--month", month]) == 0


def test_vendor_tool_issues_a_licence_the_product_accepts(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "licence.py"
    keyfile = tmp_path / "vendor.key"
    pub = subprocess.run(
        [sys.executable, str(script), "keygen", "--out", str(keyfile)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
    assert (
        subprocess.run(
            [sys.executable, str(script), "keygen", "--out", str(keyfile)], capture_output=True, check=False
        ).returncode
        == 1
    )  # never overwrites a key
    token = subprocess.run(
        [
            sys.executable, str(script), "issue", "--key", str(keyfile), "--customer", "Acme Bank",
            "--seats", "40", "--months", "12", "--starts", "2026-01-31", "--feature", "sso",
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    lic = licensing.parse(token, [Ed25519PublicKey.from_public_bytes(licensing._b64d(pub))])
    assert (lic.seats, lic.issued_at, lic.expires_at, lic.features) == (
        40,
        date(2026, 1, 31),
        date(2027, 1, 31),
        ["sso"],
    )
    inspect = subprocess.run(
        [sys.executable, str(script), "inspect", token, "--public-key", pub],
        capture_output=True,
        text=True,
        check=False,
    )
    assert inspect.returncode == 0 and "valid signature" in inspect.stdout


def test_sso_sign_in_of_a_new_person_needs_a_free_seat(
    client: TestClient, idp: Any, vendor: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = idp()
    fake.user.update(sub=f"s-{uuid.uuid4().hex[:6]}", email=f"new-{uuid.uuid4().hex[:6]}@example.com")
    with session_scope() as db:
        used = licensing.seats_used(db)
    # A configured licence is enforced in any environment.
    _use(monkeypatch, _licence(vendor, seats=used), env="development")
    _, params = sign_in(client)
    assert "token" not in params and "no free licensed seats" in params["sso_error"]
    _use(monkeypatch, _licence(vendor, seats=used + 1), env="development")
    _, params = sign_in(client)
    assert "token" in params


def test_project_limit(
    client: TestClient, vendor: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    from agentic_sdlc.orm import Project

    with session_scope() as db:
        count = db.scalar(select(func.count()).select_from(Project).where(Project.is_sample.is_(False)))
    _use(monkeypatch, _licence(vendor, max_projects=count + 1), env="development")
    ok = client.post("/api/v1/projects", json={"name": f"P {uuid.uuid4().hex[:6]}"}, headers=H("owner"))
    assert ok.status_code == 201, ok.text
    full = client.post("/api/v1/projects", json={"name": f"P {uuid.uuid4().hex[:6]}"}, headers=H("owner"))
    assert full.status_code == 403 and "covers" in full.json()["error"]["message"]
