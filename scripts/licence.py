"""Vendor-side licence tool. Run on the vendor's own machine; never ship the private key.

    python scripts/licence.py keygen --out vendor-licence.key
        Writes a new Ed25519 private key (owner-only file) and prints its public key. Put the public
        key in VENDOR_PUBLIC_KEYS (services/api/src/agentic_sdlc/core/licensing.py) for the next
        release. Keep the private key offline (password manager, HSM export, sealed backup).

    python scripts/licence.py issue --key vendor-licence.key --customer "Acme Bank" \\
        --seats 50 --months 12 [--licence-id ACME-2027] [--feature sso] [--max-projects 20]
        Prints a licence key for the customer (LICENCE_KEY, or the contents of LICENCE_FILE).

    python scripts/licence.py inspect TOKEN [--public-key KEY]
        Shows a licence's contents and whether its signature verifies.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "services" / "api" / "src")
)

from agentic_sdlc.core import licensing


def _pub(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError("bad date")


def main() -> int:
    parser = argparse.ArgumentParser(description="Vendor licence tool")
    sub = parser.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen")
    kg.add_argument("--out", required=True, type=Path)
    iss = sub.add_parser("issue")
    iss.add_argument("--key", required=True, type=Path)
    iss.add_argument("--customer", required=True)
    iss.add_argument("--seats", required=True, type=int)
    iss.add_argument("--months", type=int, default=12)
    iss.add_argument("--starts", type=date.fromisoformat, default=None)
    iss.add_argument("--licence-id")
    iss.add_argument("--feature", action="append", default=[])
    iss.add_argument("--max-projects", type=int)
    ins = sub.add_parser("inspect")
    ins.add_argument("token")
    ins.add_argument("--public-key", action="append", default=[])
    args = parser.parse_args()

    if args.cmd == "keygen":
        if args.out.exists():
            print(f"{args.out} exists; refusing to overwrite a key", file=sys.stderr)
            return 1
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        print(_pub(key.public_key()))
        return 0

    if args.cmd == "issue":
        key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            print("not an Ed25519 private key", file=sys.stderr)
            return 1
        if args.seats < 1:
            print("--seats must be at least 1", file=sys.stderr)
            return 1
        starts = args.starts or datetime.now(UTC).date()
        payload = {
            "licence_id": args.licence_id or f"LIC-{uuid.uuid4().hex[:12].upper()}",
            "customer": args.customer,
            "issued_at": starts.isoformat(),
            "expires_at": _add_months(starts, args.months).isoformat(),
            "seats": args.seats,
            "features": sorted(set(args.feature)),
            "max_projects": args.max_projects,
        }
        print(licensing.sign(payload, key))
        return 0

    keys = [
        Ed25519PublicKey.from_public_bytes(licensing._b64d(k)) for k in args.public_key
    ]
    try:
        lic = licensing.parse(args.token, keys or None)
    except licensing.LicenceError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    print(f"valid signature\n{lic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
