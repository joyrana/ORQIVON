"""Offline licence keys and their enforcement.

A licence is a signed statement from the vendor, checked locally with no call home (installations
are often air-gapped):

    ASW1.<base64url JSON payload>.<base64url Ed25519 signature over "ASW1.<payload>">

Payload: licence_id, customer, issued_at, expires_at (ISO dates), seats (active people; agent
service accounts do not count), optional features and max_projects. The vendor signs with a
private key kept offline (`scripts/licence.py issue`); this code holds only public keys, so a
customer cannot mint or extend a licence, and a key can be rotated by shipping a new public key
alongside the old one.

Enforcement is deliberately gentle, because it runs inside a customer's delivery process:

* valid                  everything works;
* expired, in grace      everything works, administrators see a warning (LICENCE_GRACE_DAYS, 14);
* expired past grace,    no new delivery runs start and no new people are added. Existing runs
  missing or invalid     finish, nothing is deleted and all data stays readable and exportable;
* over the seat count    no new people are added (existing ones keep working);
* at the project limit   no new projects (when the licence sets max_projects).

Development and test installations (APP_ENV != production) are not enforced unless a licence is
configured. A build with no vendor public key ("unlicensed build") does not enforce at all; the
status says so, so an operator can see it.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_sdlc.core.errors import PolicyViolation
from agentic_sdlc.core.settings import get_settings

PREFIX = "ASW1"

# Vendor public keys (raw Ed25519, base64url), newest first. Generate the pair once with
# `python scripts/licence.py keygen`, keep the private key offline, and add the public key here.
# Extra keys can be supplied with LICENCE_PUBLIC_KEYS (comma-separated), e.g. for tests or rotation.
VENDOR_PUBLIC_KEYS: tuple[str, ...] = ()

State = Literal["valid", "grace", "expired", "missing", "invalid", "unenforced"]


@dataclass
class Licence:
    licence_id: str
    customer: str
    issued_at: date
    expires_at: date
    seats: int
    features: list[str] = field(default_factory=list)
    max_projects: int | None = None


@dataclass
class LicenceStatus:
    state: State
    detail: str
    licence: Licence | None = None
    seats_used: int = 0
    days_left: int | None = None

    @property
    def can_start_runs(self) -> bool:
        return self.state in ("valid", "grace", "unenforced")

    def can_add_people(self) -> bool:
        if self.state == "unenforced":
            return True
        if not self.can_start_runs or self.licence is None:
            return False
        return self.seats_used < self.licence.seats

    def public(self) -> dict[str, Any]:
        lic = self.licence
        return {
            "state": self.state,
            "detail": self.detail,
            "days_left": self.days_left,
            "seats_used": self.seats_used,
            "licence": None
            if lic is None
            else {
                "licence_id": lic.licence_id,
                "customer": lic.customer,
                "issued_at": lic.issued_at.isoformat(),
                "expires_at": lic.expires_at.isoformat(),
                "seats": lic.seats,
                "features": lic.features,
                "max_projects": lic.max_projects,
            },
        }


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def public_keys() -> list[Ed25519PublicKey]:
    raw = list(VENDOR_PUBLIC_KEYS) + [
        k.strip() for k in os.environ.get("LICENCE_PUBLIC_KEYS", "").split(",") if k.strip()
    ]
    keys = []
    for k in raw:
        try:
            keys.append(Ed25519PublicKey.from_public_bytes(_b64d(k)))
        except ValueError:
            continue
    return keys


class LicenceError(ValueError):
    pass


def parse(token: str, keys: list[Ed25519PublicKey] | None = None) -> Licence:
    """Verify the signature and decode the payload; raises LicenceError with a plain reason."""
    token = token.strip()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenceError("not a licence key (expected ASW1.<payload>.<signature>)")
    keys = public_keys() if keys is None else keys
    signed = f"{parts[0]}.{parts[1]}".encode()
    try:
        sig = _b64d(parts[2])
        payload_raw = _b64d(parts[1])
    except ValueError:
        raise LicenceError("the licence key is damaged") from None
    for key in keys:
        try:
            key.verify(sig, signed)
            break
        except InvalidSignature:
            continue
    else:
        raise LicenceError("the licence signature is not valid for this product")
    try:
        p = json.loads(payload_raw)
        return Licence(
            licence_id=str(p["licence_id"]),
            customer=str(p["customer"]),
            issued_at=date.fromisoformat(p["issued_at"]),
            expires_at=date.fromisoformat(p["expires_at"]),
            seats=int(p["seats"]),
            features=[str(f) for f in p.get("features", [])],
            max_projects=int(p["max_projects"]) if p.get("max_projects") is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        raise LicenceError("the licence payload is incomplete") from None


def sign(payload: dict[str, Any], private_key: Any) -> str:
    """Vendor side (scripts/licence.py, tests): produce a licence key."""
    body = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = private_key.sign(f"{PREFIX}.{body}".encode())
    return f"{PREFIX}.{body}.{_b64e(sig)}"


def _configured_token() -> str | None:
    s = get_settings()
    if s.licence_key:
        return s.licence_key.get_secret_value()
    if s.licence_file and Path(s.licence_file).is_file():
        return Path(s.licence_file).read_text()
    return None


def seats_used(db: Session) -> int:
    from agentic_sdlc.orm import User

    return int(
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_active.is_(True), User.is_service_account.is_(False))
        )
        or 0
    )


def status(db: Session, today: date | None = None) -> LicenceStatus:
    s = get_settings()
    today = today or datetime.now(UTC).date()
    used = seats_used(db)
    token = _configured_token()
    keys = public_keys()
    if not keys:
        return LicenceStatus("unenforced", "unlicensed build: no vendor key is embedded", seats_used=used)
    if token is None:
        if s.app_env != "production":
            return LicenceStatus("unenforced", f"no licence configured ({s.app_env})", seats_used=used)
        return LicenceStatus(
            "missing", "no licence is configured (LICENCE_KEY or LICENCE_FILE)", seats_used=used
        )
    try:
        lic = parse(token, keys)
    except LicenceError as exc:
        return LicenceStatus("invalid", str(exc), seats_used=used)
    days_left = (lic.expires_at - today).days
    if days_left >= 0:
        detail = f"licensed to {lic.customer} until {lic.expires_at.isoformat()}"
        if used > lic.seats:
            detail += f"; {used} people active for {lic.seats} seats: no one new can be added"
        return LicenceStatus("valid", detail, lic, used, days_left)
    grace_end = lic.expires_at + timedelta(days=s.licence_grace_days)
    if today <= grace_end:
        return LicenceStatus(
            "grace",
            f"the licence expired on {lic.expires_at.isoformat()}; new runs stop after "
            f"{grace_end.isoformat()}. Renew to keep delivering.",
            lic,
            used,
            days_left,
        )
    return LicenceStatus(
        "expired",
        f"the licence expired on {lic.expires_at.isoformat()}. Existing work stays readable; "
        "new delivery runs and new people need a renewed licence.",
        lic,
        used,
        days_left,
    )


def require_runs(db: Session) -> None:
    st = status(db)
    if not st.can_start_runs:
        raise PolicyViolation(f"licence: {st.detail}", details={"licence_state": st.state})


def require_project(db: Session) -> None:
    """Licences may cap the number of projects (sample projects do not count)."""
    from agentic_sdlc.orm import Project

    st = status(db)
    cap = st.licence.max_projects if st.licence and st.state != "unenforced" else None
    if cap is None:
        return
    count = db.scalar(select(func.count()).select_from(Project).where(Project.is_sample.is_(False))) or 0
    if count >= cap:
        raise PolicyViolation(
            f"licence: the licence covers {cap} projects; archive one or extend the licence",
            details={"licence_state": st.state},
        )


def require_seat(db: Session) -> None:
    st = status(db)
    if not st.can_add_people():
        if st.licence is not None and st.can_start_runs:
            reason = f"all {st.licence.seats} licensed seats are in use"
        else:
            reason = st.detail
        raise PolicyViolation(f"licence: {reason}", details={"licence_state": st.state})
