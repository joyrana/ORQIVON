# Licensing and usage reports

ORQIVON is proprietary software (see [LICENSE](../../LICENSE)). Each installation
runs under a licence key issued by the vendor. Keys are checked locally; the product never calls
home, so air-gapped installations work the same way.

## Installing a licence

Put the key in the deployment secret as `LICENCE_KEY` (Helm: the `existingSecret`), or mount it as a
file and set `LICENCE_FILE`. Check it:

```bash
agentic-sdlc licence          # prints state, licensee, expiry and seats in use; exit 1 if runs are blocked
```

Administrators also see it at `GET /api/v1/system/licence`. Prometheus gets
`asw_licence_runs_allowed`, `asw_licence_days_left` and `asw_licence_seats`; the example alert
rules warn 30 days before expiry.

## What the licence controls

| State | Meaning | Effect |
|---|---|---|
| `valid` | Signature good, not expired | Everything works. Everyone sees a notice in the last 30 days. |
| `grace` | Expired up to `LICENCE_GRACE_DAYS` (default 14) ago | Everything works; everyone sees a warning. |
| `expired` | Past the grace period | No new delivery runs and no new people. Runs already in progress finish. Nothing is deleted; all projects, evidence and audit history stay readable and exportable. |
| `missing` / `invalid` | No key in production, or a key that does not verify | Same as `expired`. |
| `unenforced` | Development or test installation without a key | No limits. |

Seats count active people. Agent service accounts do not count, and neither do deactivated users,
so removing someone in the directory (SCIM `active: false`) frees their seat. When every seat is in
use, SSO sign-in of a new person and SCIM provisioning are refused with a clear message, and people
who already have accounts keep working.

## Monthly usage report

```bash
agentic-sdlc usage-report --month 2026-11 > usage-2026-11.json
```

or `GET /api/v1/system/usage?month=2026-11` as an administrator. The report holds counts and totals
only: people active in the month, accounts active now, projects, delivery runs by outcome,
successful deployments per environment, and model tokens and cost per model. It contains no names,
e-mail addresses, project names or content. A `sha256` over the canonical JSON lets both parties
refer to exactly the same figures.

## For the vendor: issuing keys

Done once, on a vendor machine, never in a customer installation:

```bash
python scripts/licence.py keygen --out vendor-licence.key   # prints the public key
```

Keep `vendor-licence.key` offline (a password manager or sealed backup). Add the printed public key to
`VENDOR_PUBLIC_KEYS` in `services/api/src/agentic_sdlc/core/licensing.py` and release. A build with
no public key embedded reports `unenforced` ("unlicensed build"). To rotate, add the new public key
first, so older keys keep verifying.

```bash
python scripts/licence.py issue --key vendor-licence.key --customer "Acme Bank" --seats 50 --months 12
python scripts/licence.py inspect <key> --public-key <public key>
```

A licence carries: licence id, customer, issue and expiry dates, seats, and optional features and
project limit. It is signed with Ed25519, so changing any field invalidates it.
