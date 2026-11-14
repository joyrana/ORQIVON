"""Prometheus metrics, without extra dependencies.

Process-local counters and histograms (HTTP requests) plus gauges computed from the database at
scrape time (runs by status, job queue, workers, approvals waiting, token cost), so one scrape of
any API replica gives the fleet-wide workflow picture. Labels are bounded: routes are the
templated path (`/api/v1/projects/{project_id}`), never raw URLs, and no label carries user data.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import MutableMapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

_lock = threading.Lock()
_requests: dict[tuple[str, str, str], int] = defaultdict(int)
_duration: dict[tuple[str, str], list[float]] = {}
_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
_duration_count: dict[tuple[str, str], int] = defaultdict(int)


def route_template(scope: MutableMapping[str, Any]) -> str:
    """The templated path of the matched route, including any router prefix ("unmatched" if none).

    Included routers can hand over a route whose path is relative to their prefix, so the prefix is
    recovered as the part of the request path before the route's own pattern matches.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    regex = getattr(route, "path_regex", None)
    if not template or regex is None:
        return "unmatched"
    path = str(scope.get("path", ""))
    for i in range(len(path)):
        if path[i] == "/" and regex.match(path[i:]):
            return path[:i] + template
    return str(template)


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    key = (method, route)
    with _lock:
        _requests[(method, route, str(status))] += 1
        counts = _duration.setdefault(key, [0.0] * len(BUCKETS))
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                counts[i] += 1
        _duration_sum[key] += seconds
        _duration_count[key] += 1


def reset() -> None:
    with _lock:
        _requests.clear()
        _duration.clear()
        _duration_sum.clear()
        _duration_count.clear()


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv: Any) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in kv.items()) + "}" if kv else ""


def render(db: Session) -> str:
    from agentic_sdlc.core.db import utcnow
    from agentic_sdlc.orm import ApprovalRequest, TokenUsage, WorkflowJob, WorkflowRun
    from agentic_sdlc.workflows.worker import live_workers

    out: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[dict[str, Any], float]]) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{_labels(**lbl)} {val:g}" for lbl, val in samples)

    with _lock:
        reqs = [
            ({"method": m, "route": r, "status": s}, float(n)) for (m, r, s), n in sorted(_requests.items())
        ]
        hist: list[tuple[dict[str, Any], float]] = []
        for (m, r), counts in sorted(_duration.items()):
            cumulative = 0.0
            for b, c in zip(BUCKETS, counts, strict=True):
                cumulative = c  # counts are already cumulative (<= bucket)
                hist.append(({"method": m, "route": r, "le": b}, cumulative))
            hist.append(({"method": m, "route": r, "le": "+Inf"}, float(_duration_count[(m, r)])))
        sums = [({"method": m, "route": r}, v) for (m, r), v in sorted(_duration_sum.items())]
        cnts = [({"method": m, "route": r}, float(v)) for (m, r), v in sorted(_duration_count.items())]
    metric("asw_http_requests_total", "counter", "HTTP requests handled by this process", reqs)
    out.append("# HELP asw_http_request_duration_seconds HTTP request latency in this process")
    out.append("# TYPE asw_http_request_duration_seconds histogram")
    out.extend(f"asw_http_request_duration_seconds_bucket{_labels(**lbl)} {v:g}" for lbl, v in hist)
    out.extend(f"asw_http_request_duration_seconds_sum{_labels(**lbl)} {v:g}" for lbl, v in sums)
    out.extend(f"asw_http_request_duration_seconds_count{_labels(**lbl)} {v:g}" for lbl, v in cnts)

    runs = db.execute(select(WorkflowRun.status, func.count()).group_by(WorkflowRun.status)).all()
    metric(
        "asw_workflow_runs", "gauge", "Workflow runs by status", [({"status": s}, float(n)) for s, n in runs]
    )
    jobs = db.execute(select(WorkflowJob.status, func.count()).group_by(WorkflowJob.status)).all()
    metric("asw_jobs", "gauge", "Background jobs by status", [({"status": s}, float(n)) for s, n in jobs])
    oldest = db.scalar(
        select(func.min(WorkflowJob.available_at)).where(
            WorkflowJob.status == "queued", WorkflowJob.available_at <= utcnow()
        )
    )
    lag = (utcnow() - oldest).total_seconds() if oldest else 0.0
    metric("asw_job_queue_lag_seconds", "gauge", "Age of the oldest job that is due", [({}, max(0.0, lag))])
    metric(
        "asw_workers_live",
        "gauge",
        "Workers with a heartbeat in the last 90 s",
        [({}, float(len(live_workers(db))))],
    )
    pending = db.scalar(
        select(func.count()).select_from(ApprovalRequest).where(ApprovalRequest.status == "pending")
    )
    metric(
        "asw_approvals_pending",
        "gauge",
        "Approval requests waiting for a person",
        [({}, float(pending or 0))],
    )
    usage = db.execute(
        select(
            TokenUsage.is_estimated,
            func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens),
            func.sum(TokenUsage.cost_usd),
        ).group_by(TokenUsage.is_estimated)
    ).all()
    metric(
        "asw_model_tokens_total",
        "counter",
        "Model tokens used, by whether usage was estimated",
        [({"estimated": str(bool(e)).lower()}, float(t or 0)) for e, t, _ in usage],
    )
    metric(
        "asw_model_cost_usd_total",
        "counter",
        "Model cost in USD, by whether usage was estimated",
        [({"estimated": str(bool(e)).lower()}, float(c or 0)) for e, _, c in usage],
    )
    from agentic_sdlc.core import licensing

    lic = licensing.status(db)
    metric(
        "asw_licence_runs_allowed",
        "gauge",
        "1 while the licence allows new delivery runs",
        [({"state": lic.state}, 1.0 if lic.can_start_runs else 0.0)],
    )
    if lic.days_left is not None:
        metric(
            "asw_licence_days_left", "gauge", "Days until the licence expires", [({}, float(lic.days_left))]
        )
    if lic.licence is not None:
        metric(
            "asw_licence_seats",
            "gauge",
            "Licensed seats and seats in use",
            [({"kind": "licensed"}, float(lic.licence.seats)), ({"kind": "used"}, float(lic.seats_used))],
        )
    return "\n".join(out) + "\n"
