"""Command line entry point: `agentic-sdlc <command>`."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-sdlc")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="seed agent accounts and (in development) dev users")
    api = sub.add_parser("api", help="run the API server")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    w = sub.add_parser("worker", help="run the workflow worker")
    w.add_argument("--once", action="store_true", help="process available jobs then exit")
    sub.add_parser("openapi", help="print the OpenAPI document")
    wh = sub.add_parser("worker-health", help="exit 0 if a worker on this host sent a heartbeat recently")
    wh.add_argument("--max-age", type=int, default=90)
    models = sub.add_parser("models", help="inspect and check the configured models")
    msub = models.add_subparsers(dest="models_cmd", required=True)
    msub.add_parser("list", help="list models, their endpoints and availability")
    chk = msub.add_parser("check", help="send a synthetic structured-output probe to each model")
    chk.add_argument("--model", help="check only this model_id")
    chk.add_argument("--timeout", type=float, default=60)
    bk = sub.add_parser("backup", help="write a consistent backup (database and data directory)")
    bk.add_argument("--out", default="backups", help="directory to write the backup file into")
    vf = sub.add_parser("verify-backup", help="check a backup's checksums and contents")
    vf.add_argument("archive")
    rs = sub.add_parser("restore", help="restore a backup into this installation")
    rs.add_argument("archive")
    rs.add_argument("--force", action="store_true", help="replace a non-empty database and data directory")
    args = parser.parse_args(argv)

    if args.cmd in {"backup", "verify-backup", "restore"}:
        from pathlib import Path

        from agentic_sdlc.ops.backup import BackupError, create_backup, restore_backup, verify_backup

        try:
            if args.cmd == "backup":
                print(create_backup(Path(args.out)))
            elif args.cmd == "verify-backup":
                print(json.dumps(verify_backup(Path(args.archive)), indent=2))
            else:
                m = restore_backup(Path(args.archive), force=args.force)
                print(
                    f"restored backup from {m['created_at']} (schema {m['schema_revision']}); "
                    "now run: alembic upgrade head"
                )
        except BackupError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "seed":
        from agentic_sdlc.core.db import session_scope
        from agentic_sdlc.seed import seed_agents, seed_dev_users

        with session_scope() as db:
            seed_agents(db)
            users = seed_dev_users(db)
        for user, token in users:
            print(f"{user.email:28} token={token}")
        return 0
    if args.cmd == "api":
        import uvicorn

        uvicorn.run("agentic_sdlc.api.main:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "worker":
        from agentic_sdlc.workflows.worker import run_worker

        run_worker(once=args.once)
        return 0
    if args.cmd == "worker-health":
        import socket
        from datetime import timedelta

        from sqlalchemy import select

        from agentic_sdlc.core.db import session_scope, utcnow
        from agentic_sdlc.orm import WorkerHeartbeat

        with session_scope() as db:
            row = db.scalar(
                select(WorkerHeartbeat)
                .where(
                    WorkerHeartbeat.hostname == socket.gethostname(),
                    WorkerHeartbeat.seen_at >= utcnow() - timedelta(seconds=args.max_age),
                )
                .limit(1)
            )
        if row is None:
            print(
                f"no heartbeat from a worker on {socket.gethostname()} in {args.max_age} s", file=sys.stderr
            )
            return 1
        problems = row.sandbox.get("problems") or []
        if problems:
            print("sandbox problems: " + "; ".join(problems), file=sys.stderr)
            return 1
        print(f"ok: {row.worker_id}, last seen {row.seen_at.isoformat()}")
        return 0
    if args.cmd == "models":
        from agentic_sdlc.models.registry import get_registry

        reg = get_registry()
        if args.models_cmd == "list":
            for e in sorted(reg.entries.values(), key=lambda e: e.model_id):
                ep = reg.endpoints.get(e.endpoint)
                print(
                    f"{e.model_id:32} endpoint={e.endpoint:16} "
                    f"configured={bool(ep and ep.resolved_url()) or e.is_mock!s:5} "
                    f"available={reg.is_available(e)!s:5} privacy={e.privacy} mock={e.is_mock}"
                )
            return 0
        from agentic_sdlc.models.check import check_models

        results = check_models(reg, only=args.model, timeout_s=args.timeout)
        json.dump(results, sys.stdout, indent=2)
        print()
        if not results:
            print("no non-mock models configured", file=sys.stderr)
            return 2
        return 0 if all(r["ok"] for r in results) else 1
    if args.cmd == "openapi":
        from agentic_sdlc.api.app import create_app

        json.dump(create_app().openapi(), sys.stdout, indent=2)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
