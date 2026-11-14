"""Shared FastAPI dependencies."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock
from typing import Annotated

from fastapi import Depends, Header, Path
from sqlalchemy.orm import Session

from agentic_sdlc.auth.service import Principal, authenticate, authorize
from agentic_sdlc.core.db import get_db
from agentic_sdlc.core.errors import RateLimited
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.orm import Project

DB = Annotated[Session, Depends(get_db)]


class _SlidingWindowLimiter:
    """Per-process sliding-window limiter keyed by token id (RATE_LIMIT_BACKEND=memory): for single
    processes and tests; not shared across API replicas."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, limit: int) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                raise RateLimited(
                    "rate limit exceeded", details={"retry_after_s": int(60 - (now - q[0])) + 1}
                )
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class _PostgresLimiter:
    """Fixed one-minute windows counted in PostgreSQL, so every API replica shares one limit.

    One upsert per authenticated request, on its own short transaction (never the request's).
    Windows older than five minutes are pruned opportunistically.
    """

    def check(self, key: str, limit: int) -> None:
        import random

        from sqlalchemy import text

        from agentic_sdlc.core.db import get_engine as engine

        with engine().begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO rate_limit_windows (key, window_start, count) "
                    "VALUES (:key, date_trunc('minute', now()), 1) "
                    "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_windows.count + 1 "
                    "RETURNING count, extract(epoch from (window_start + interval '1 minute' - now()))"
                ),
                {"key": key[:200]},
            ).one()
            if random.random() < 0.01:  # noqa: S311 - housekeeping, not security
                conn.execute(
                    text("DELETE FROM rate_limit_windows WHERE window_start < now() - interval '5 minutes'")
                )
        if int(row[0]) > limit:
            raise RateLimited("rate limit exceeded", details={"retry_after_s": max(1, int(row[1]) + 1)})

    def reset(self) -> None:
        from sqlalchemy import text

        from agentic_sdlc.core.db import get_engine as engine

        with engine().begin() as conn:
            conn.execute(text("DELETE FROM rate_limit_windows"))


_memory = _SlidingWindowLimiter()
_postgres = _PostgresLimiter()


class _Limiter:
    """The configured backend (RATE_LIMIT_BACKEND: postgres, the default, or memory)."""

    def _backend(self) -> _SlidingWindowLimiter | _PostgresLimiter:
        return _postgres if get_settings().rate_limit_backend == "postgres" else _memory

    def check(self, key: str, limit: int) -> None:
        self._backend().check(key, limit)

    def reset(self) -> None:
        self._backend().reset()


limiter = _Limiter()


def current_principal(db: DB, authorization: Annotated[str | None, Header()] = None) -> Principal:
    principal = authenticate(db, authorization)
    limiter.check(str(principal.token_id or principal.user_id), get_settings().rate_limit_per_minute)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def project_permission(permission: str) -> Callable[..., Project]:
    def dep(
        db: DB,
        principal: CurrentPrincipal,
        project_id: Annotated[uuid.UUID, Path()],
    ) -> Project:
        return authorize(db, principal, project_id, permission)

    return dep
