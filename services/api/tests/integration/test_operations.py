"""Self-hosted operations: shared rate limiting, readiness, worker heartbeats, metrics, OTLP export."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentic_sdlc.core.db import get_engine, session_scope, utcnow
from agentic_sdlc.core.errors import RateLimited
from agentic_sdlc.core.settings import get_settings
from tests.conftest import H


def test_rate_limit_is_shared_across_api_replicas() -> None:
    """Two limiter instances stand in for two API processes: they share one budget."""
    from agentic_sdlc.api.deps import _PostgresLimiter

    a, b = _PostgresLimiter(), _PostgresLimiter()
    a.reset()
    key = f"tok-{time.time_ns()}"
    a.check(key, 3)
    b.check(key, 3)
    a.check(key, 3)
    with pytest.raises(RateLimited) as exc:
        b.check(key, 3)
    assert 1 <= exc.value.details["retry_after_s"] <= 61
    b.check(f"{key}-other", 3)  # per key
    # A new minute starts a new window; old windows do not count.
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE rate_limit_windows SET window_start = window_start - interval '2 minutes' "
                "WHERE key = :k"
            ),
            {"k": key},
        )
    a.check(key, 3)


def test_rate_limit_counts_concurrent_requests_exactly() -> None:
    from agentic_sdlc.api.deps import _PostgresLimiter

    lim = _PostgresLimiter()
    key = f"burst-{time.time_ns()}"
    denied: list[int] = []

    def hit() -> None:
        try:
            lim.check(key, 10)
        except RateLimited:
            denied.append(1)

    threads = [threading.Thread(target=hit) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(denied) == 15


def test_readiness_reports_schema_and_storage(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = client.get("/api/v1/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] | {"workers": 0} == {
        "database": "ok",
        "schema": "ok",
        "data_dir": "ok",
        "workers": 0,
    }

    # Code newer than the database: not ready until migrations run.
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (tmp_path / "migrations" / "script.py.mako").write_text("")
    (versions / "9999_future.py").write_text('revision = "9999"\ndown_revision = None\n')
    monkeypatch.setattr(get_settings(), "migrations_dir", tmp_path / "migrations")
    r = client.get("/api/v1/ready")
    assert r.status_code == 503 and "run migrations" in r.json()["checks"]["schema"]


def test_heartbeats_workers_endpoint_and_health_probe(client: TestClient) -> None:
    import socket

    from agentic_sdlc.cli import main
    from agentic_sdlc.orm import WorkerHeartbeat
    from agentic_sdlc.workflows.worker import heartbeat, live_workers

    worker_id = f"{socket.gethostname()}:test-{time.time_ns()}"
    heartbeat(worker_id, utcnow(), 7)
    with session_scope() as db:
        assert any(w.worker_id == worker_id and w.jobs_done == 7 for w in live_workers(db))
    assert client.get("/api/v1/system/workers", headers=H("owner")).status_code == 403
    listed = client.get("/api/v1/system/workers", headers=H("admin")).json()
    mine = next(w for w in listed if w["worker_id"] == worker_id)
    assert mine["sandbox"]["backend"] == get_settings().sandbox_backend
    assert main(["worker-health"]) == 0

    with session_scope() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        assert row is not None
        row.seen_at = utcnow().replace(year=2000)
        # Other heartbeats from this host (e.g. a running local worker) would mask the check.
        db.execute(
            text("UPDATE worker_heartbeats SET seen_at = '2000-01-01' WHERE hostname = :h"),
            {"h": socket.gethostname()},
        )
    assert main(["worker-health", "--max-age", "60"]) == 1


def test_metrics_exposition(client: TestClient, project: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_sdlc.core import metrics

    metrics.reset()
    client.get(f"/api/v1/projects/{project['id']}", headers=H("owner"))
    client.get(f"/api/v1/projects/{project['id']}", headers=H("owner"))
    body = client.get("/api/v1/metrics").text
    assert (
        'asw_http_requests_total{method="GET",route="/api/v1/projects/{project_id}",status="200"} 2' in body
    )
    assert project["id"] not in body  # bounded labels: templated routes, never raw ids
    assert re.search(
        r'asw_http_request_duration_seconds_bucket\{method="GET",'
        r'route="/api/v1/projects/\{project_id\}",le="\+Inf"\} 2',
        body,
    )
    for name in (
        "asw_workflow_runs",
        "asw_jobs",
        "asw_job_queue_lag_seconds",
        "asw_workers_live",
        "asw_approvals_pending",
        "asw_model_tokens_total",
        "asw_model_cost_usd_total",
    ):
        assert f"# TYPE {name} " in body, name
    monkeypatch.setenv("METRICS_TOKEN", "m-secret")
    assert client.get("/api/v1/metrics").status_code == 401
    assert client.get("/api/v1/metrics", headers={"Authorization": "Bearer m-secret"}).status_code == 200


def test_traces_are_exported_over_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    from agentic_sdlc.core.tracing import build_provider
    from tests.model_server import _free_port

    received: list[dict[str, Any]] = []

    async def traces(request: Request) -> Response:
        received.append(
            {
                "type": request.headers.get("content-type"),
                "body": await request.body(),
                "auth": request.headers.get("x-api-key"),
            }
        )
        return Response(b"", status_code=200, media_type="application/x-protobuf")

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/v1/traces", traces, methods=["POST"])]),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=collector-key")
        provider = build_provider("asw-test-service")
        with provider.get_tracer("t").start_as_current_span("workflow.stage") as span:
            span.set_attribute("stage", "qa_tests")
        assert provider.force_flush(10_000)
        provider.shutdown()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert received and received[0]["type"] == "application/x-protobuf"
    assert b"asw-test-service" in received[0]["body"] and b"workflow.stage" in received[0]["body"]
    assert received[0]["auth"] == "collector-key"
