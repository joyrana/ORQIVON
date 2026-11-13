"""OpenTelemetry tracing setup.

`OTEL_TRACES_EXPORTER=otlp` sends spans over OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT` (any
OpenTelemetry Collector, Jaeger, Tempo, Honeycomb, Datadog Agent...), honouring the standard
`OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_RESOURCE_ATTRIBUTES` variables. `console` prints spans.
Unset: spans are created (for log correlation) but not exported.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_configured = False


def configure_tracing(service_name: str) -> None:
    global _configured
    if _configured:
        return
    trace.set_tracer_provider(build_provider(service_name))
    _configured = True


def build_provider(service_name: str) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = os.environ.get("OTEL_TRACES_EXPORTER", "")
    if exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


tracer = trace.get_tracer("agentic_sdlc")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, str | int | float | bool) else str(v))
        yield s


def current_trace_context() -> dict[str, str] | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
