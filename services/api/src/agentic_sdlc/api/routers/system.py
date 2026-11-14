from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Header
from sqlalchemy import select, text

from agentic_sdlc import __version__
from agentic_sdlc.api.deps import DB, CurrentPrincipal
from agentic_sdlc.api.schemas import DevUser, MeOut, SystemInfo
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.orm import Membership
from agentic_sdlc.seed import DEV_USERS

router = APIRouter(tags=["system"])

_ROLE_HINTS = {
    "dev_admin": "global admin (no approval authority)",
    "dev_owner": "project owner of projects they create",
    "dev_engineer": "engineer when added to a project",
    "dev_reviewer": "reviewer when added to a project",
    "dev_qa": "qa when added to a project",
    "dev_approver": "release approver when added to a project",
    "dev_auditor": "auditor when added to a project",
}


@router.get("/health")
def health(db: DB) -> dict[str, str]:
    db.execute(text("select 1"))
    return {"status": "ok"}


@router.get("/ready")
def ready(db: DB) -> Any:
    """Readiness for load balancers and Kubernetes: database reachable, schema at the code's head,
    data directory writable. Workers are reported but do not gate API readiness."""
    from fastapi.responses import JSONResponse

    from agentic_sdlc.workflows.worker import live_workers

    checks: dict[str, Any] = {}
    try:
        db.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"unreachable: {type(exc).__name__}"
    checks["schema"] = _schema_state(db) if checks["database"] == "ok" else "unknown"
    try:
        probe = get_settings().data_dir / ".ready-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
        checks["data_dir"] = "ok"
    except OSError as exc:
        checks["data_dir"] = f"not writable: {exc.strerror}"
    workers = live_workers(db) if checks["database"] == "ok" else []
    checks["workers"] = len(workers)
    ok = all(checks[k] == "ok" for k in ("database", "schema", "data_dir"))
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
    )


def _schema_state(db: Any) -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    location = get_settings().migrations_dir
    if not location.is_dir():
        return f"cannot verify: migrations not found at {location} (set MIGRATIONS_DIR)"
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    try:
        current = {r[0] for r in db.execute(text("select version_num from alembic_version"))}
    except Exception:
        return "not migrated"
    return "ok" if current == heads else f"at {sorted(current)}, code expects {sorted(heads)}: run migrations"


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics(db: DB, authorization: Annotated[str | None, Header()] = None) -> Any:
    """Prometheus exposition. Requires `Authorization: Bearer $METRICS_TOKEN` when that is set."""
    import hmac
    import os

    from fastapi.responses import PlainTextResponse

    from agentic_sdlc.core import metrics
    from agentic_sdlc.core.errors import Unauthorized

    expected = os.environ.get(get_settings().metrics_token_env)
    if expected:
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise Unauthorized("metrics token required")
    return PlainTextResponse(metrics.render(db), media_type="text/plain; version=0.0.4")


@router.get("/system/workers")
def workers(db: DB, principal: CurrentPrincipal) -> list[dict[str, Any]]:
    """Live worker processes (heartbeat within 90 s) and their sandbox status. Administrators only."""
    from agentic_sdlc.core.errors import Forbidden
    from agentic_sdlc.workflows.worker import live_workers

    if not principal.is_admin:
        raise Forbidden("administrators only")
    return [
        {
            "worker_id": w.worker_id,
            "hostname": w.hostname,
            "version": w.version,
            "started_at": w.started_at.isoformat(),
            "seen_at": w.seen_at.isoformat(),
            "jobs_done": w.jobs_done,
            "sandbox": w.sandbox,
        }
        for w in live_workers(db)
    ]


@router.get("/system/licence")
def licence(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    """Licence status, seats in use and what is enforced. Administrators only."""
    from agentic_sdlc.core import licensing
    from agentic_sdlc.core.errors import Forbidden

    if not principal.is_admin:
        raise Forbidden("administrators only")
    return licensing.status(db).public()


@router.get("/system/usage")
def usage_report(db: DB, principal: CurrentPrincipal, month: str) -> dict[str, Any]:
    """Monthly usage report (counts only) for licence true-up. Administrators only."""
    from agentic_sdlc.core import usage
    from agentic_sdlc.core.errors import Forbidden, ValidationFailed

    if not principal.is_admin:
        raise Forbidden("administrators only")
    try:
        return usage.report(db, month)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from None


@router.get("/system/info", response_model=SystemInfo)
def info(db: DB) -> SystemInfo:
    from agentic_sdlc.api.schemas import LicenceSummary
    from agentic_sdlc.core import licensing

    s = get_settings()
    st = licensing.status(db)
    return SystemInfo(
        version=__version__,
        app_env=s.app_env,
        auth_mode=s.auth_mode,
        dev_login_enabled=s.app_env in ("development", "test") and s.dev_seed_users,
        sandbox_backend=s.sandbox_backend,
        model_inference="configured" if s.model_endpoint_url else "unavailable",
        production_deploy_enabled=s.production_deploy_enabled,
        licence=LicenceSummary(state=st.state, days_left=st.days_left),
    )


@router.get("/system/dev-users", response_model=list[DevUser])
def dev_users() -> list[DevUser]:
    """Development-only convenience for the sign-in screen. Returns 404 outside dev/test."""
    s = get_settings()
    if s.app_env not in ("development", "test") or not s.dev_seed_users:
        from agentic_sdlc.core.errors import NotFound

        raise NotFound("not available")
    return [
        DevUser(email=e, display_name=n, token=t, roles_hint=_ROLE_HINTS[t]) for e, n, _admin, t in DEV_USERS
    ]


@router.get("/me", response_model=MeOut)
def me(db: DB, principal: CurrentPrincipal) -> MeOut:
    roles: dict[str, list[str]] = defaultdict(list)
    for m in db.scalars(select(Membership).where(Membership.user_id == principal.user_id)):
        roles[str(m.project_id)].append(m.role)
    return MeOut(
        id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        is_admin=principal.is_admin,
        project_roles=dict(roles),
    )
