"""Workflow worker: claims jobs and dispatches them to handlers."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.core.db import session_scope, utcnow
from agentic_sdlc.core.logging import configure_logging, correlation_id_var, log
from agentic_sdlc.core.tracing import configure_tracing, span
from agentic_sdlc.orm import WorkerHeartbeat
from agentic_sdlc.workflows import jobs

logger = logging.getLogger("agentic_sdlc.worker")

Handler = Callable[[dict[str, Any], uuid.UUID | None], None]


def handlers() -> dict[str, Handler]:
    from agentic_sdlc.workflows import delivery, intake, planning_job

    return {
        "intake": intake.handle,
        "plan": planning_job.handle,
        "delivery.start": delivery.handle_start,
        "delivery.resume": delivery.handle_resume,
        "repository.sync": handle_repository_sync,
        "integrations.notify": handle_notify,
        "backlog.sync": handle_backlog_sync,
    }


def handle_notify(payload: dict[str, Any], run_id: uuid.UUID | None) -> None:
    from agentic_sdlc.integrations.notify import deliver
    from agentic_sdlc.integrations.worktools import IntegrationError

    with session_scope() as db:
        try:
            deliver(db, payload["event_id"], payload["connection"])
        except IntegrationError as exc:
            if not exc.retryable:
                raise NonRetryable(str(exc)) from None
            raise


def handle_backlog_sync(payload: dict[str, Any], run_id: uuid.UUID | None) -> None:
    from agentic_sdlc.audit import service as audit
    from agentic_sdlc.core.errors import ValidationFailed
    from agentic_sdlc.events import service as events
    from agentic_sdlc.integrations.backlog_sync import sync_backlog
    from agentic_sdlc.orm import Project

    with session_scope() as db:
        project = db.get(Project, uuid.UUID(payload["project_id"]))
        if project is None:
            return
        try:
            report = sync_backlog(db, project).as_dict()
        except ValidationFailed as exc:
            raise NonRetryable(str(exc)) from None
        audit.record(
            db,
            action="backlog.synced",
            actor_id=uuid.UUID(payload["requested_by"]) if payload.get("requested_by") else None,
            actor_type="human" if payload.get("requested_by") else "system",
            project_id=project.id,
            target_type="project",
            target_id=project.id,
            details=report,
        )
        events.emit(
            db,
            type="backlog.synced",
            project_id=project.id,
            payload={
                **{k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in report.items()},
                "failed_items": {k: v[:300] for k, v in list(report["failed"].items())[:20]},
            },
        )


def handle_repository_sync(payload: dict[str, Any], run_id: uuid.UUID | None) -> None:
    from agentic_sdlc.audit import service as audit
    from agentic_sdlc.orm import ProjectRepository
    from agentic_sdlc.projects.repositories import sync

    with session_scope() as db:
        repo = db.get(ProjectRepository, uuid.UUID(payload["repository_id"]))
        if repo is None or repo.kind != "git":
            return
        status = sync(repo)
        audit.record(
            db,
            action="repository.synced" if status["state"] == "ok" else "repository.sync_failed",
            actor_id=None,
            actor_type="system",
            project_id=repo.project_id,
            target_type="repository",
            target_id=repo.id,
            details={k: status.get(k) for k in ("state", "head", "error")},
        )


class NonRetryable(Exception):
    """Raise from a handler to fail the job immediately."""


def process_one() -> bool:
    """Claim and process a single job. Returns False when the queue is empty."""
    with session_scope() as db:
        job = jobs.claim(db)
        if job is None:
            return False
        job_id, kind, payload, run_id, attempt = job.id, job.kind, dict(job.payload), job.run_id, job.attempts
    token = correlation_id_var.set(str(payload.get("correlation_id") or job_id))
    try:
        handler = handlers().get(kind)
        if handler is None:
            raise NonRetryable(f"no handler for job kind '{kind}'")
        with span("job.process", kind=kind, job_id=str(job_id), attempt=attempt):
            handler(payload, run_id)
        with session_scope() as db:
            jobs.complete(db, job_id)
        log(logger, logging.INFO, "job done", kind=kind, job_id=str(job_id))
    except Exception as exc:
        log(
            logger,
            logging.ERROR,
            "job failed",
            kind=kind,
            job_id=str(job_id),
            error=str(exc),
            trace=traceback.format_exc(limit=5),
        )
        with session_scope() as db:
            job2 = jobs.fail(db, job_id, f"{type(exc).__name__}: {exc}")
            if isinstance(exc, NonRetryable) and job2 is not None:
                job2.status = "failed"
            permanently_failed = job2 is not None and job2.status == "failed"
        if permanently_failed:
            from agentic_sdlc.workflows import delivery

            delivery.on_job_failed(kind, payload, f"{type(exc).__name__}: {exc}")
    finally:
        correlation_id_var.reset(token)
    return True


def drain(max_jobs: int = 1000) -> int:
    """Process jobs until the queue is empty (used by tests and `worker --once`)."""
    n = 0
    while n < max_jobs and process_one():
        n += 1
    return n


HEARTBEAT_S = 15
STALE_AFTER_S = 90  # a worker silent for longer is considered gone


def heartbeat(worker_id: str, started: datetime, jobs_done: int) -> None:
    from agentic_sdlc import __version__
    from agentic_sdlc.core.settings import get_settings
    from agentic_sdlc.execution.sandbox import toolchain_problems

    s = get_settings()
    try:
        problems = toolchain_problems(s) if s.sandbox_backend == "namespace" else []
    except Exception as exc:  # never let a probe stop the worker
        problems = [str(exc)[:200]]
    with session_scope() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(
                worker_id=worker_id,
                hostname=socket.gethostname(),
                version=__version__,
                started_at=started,
                seen_at=utcnow(),
                jobs_done=jobs_done,
            )
            db.add(row)
        row.seen_at = utcnow()
        row.jobs_done = jobs_done
        row.sandbox = {
            "backend": s.sandbox_backend,
            "isolated_root": s.sandbox_isolated_root,
            "problems": problems,
        }


def live_workers(db: Session) -> list[WorkerHeartbeat]:
    cutoff = utcnow() - timedelta(seconds=STALE_AFTER_S)
    return list(db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.seen_at >= cutoff)))


def run_worker(*, once: bool = False, poll_s: float = 1.0) -> None:
    configure_logging()
    configure_tracing("agentic-sdlc-worker")
    from agentic_sdlc.core.db import session_scope as scope
    from agentic_sdlc.models.registry import get_registry
    from agentic_sdlc.skills.registry import get_skill_registry

    with scope() as db:
        get_skill_registry().sync_to_db(db)
        get_registry().sync_to_db(db)
    # Create or migrate LangGraph's checkpoint tables before taking jobs, so the one-time setup
    # never runs in the middle of a user's workflow.
    from agentic_sdlc.workflows.checkpoint import checkpointer

    with checkpointer():
        pass
    if once:
        drain()
        return
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    started = utcnow()
    jobs_done = 0
    last_beat = 0.0
    log(logger, logging.INFO, "worker started", worker_id=worker_id)
    while not stop:
        if time.monotonic() - last_beat > HEARTBEAT_S:
            heartbeat(worker_id, started, jobs_done)
            last_beat = time.monotonic()
        if process_one():
            jobs_done += 1
        else:
            time.sleep(poll_s)
    with scope() as db:  # leave cleanly so readiness does not count a stopped worker
        row = db.get(WorkerHeartbeat, worker_id)
        if row is not None:
            db.delete(row)
