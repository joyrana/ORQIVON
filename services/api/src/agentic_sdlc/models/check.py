"""Live model checks: `agentic-sdlc models check`.

Runs against the endpoints configured for this deployment and reports, per model, whether it is
served, whether it returns schema-valid structured output, which structured-output mode the server
accepted, reported token usage and latency. It sends only a synthetic prompt, never project data.
This is how an operator verifies a real deployment; CI cannot, because it has no model.
"""

from __future__ import annotations

import time
from typing import Any

from jsonschema import Draft202012Validator

from agentic_sdlc.models.providers import OpenAICompatibleProvider
from agentic_sdlc.models.registry import ModelRegistry
from agentic_sdlc.models.types import Message, ModelError, ModelRequest

PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirement_ids", "has_ambiguity"],
    "properties": {
        "requirement_ids": {"type": "array", "items": {"type": "string", "pattern": "^REQ-[0-9]+$"}},
        "has_ambiguity": {"type": "boolean"},
    },
}
PROBE_TEXT = (
    "REQ-1: The system shall reject titles longer than 120 characters.\n"
    "REQ-2: Responses should be fast.\n"
    "List the requirement ids, and say whether any requirement is ambiguous."
)


def check_models(
    registry: ModelRegistry, *, only: str | None = None, timeout_s: float = 60
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in sorted(registry.entries.values(), key=lambda e: e.model_id):
        if entry.is_mock or (only and entry.model_id != only):
            continue
        row: dict[str, Any] = {"model_id": entry.model_id, "endpoint": entry.endpoint, "ok": False}
        provider = registry.provider_for(entry)
        if provider is None:
            row["error"] = f"endpoint '{entry.endpoint}' is not configured"
            results.append(row)
            continue
        if isinstance(provider, OpenAICompatibleProvider):
            served = provider.served_models(refresh=True)
            row["served"] = entry.served_model_name in served
            if not row["served"]:
                row["error"] = f"'{entry.served_model_name}' is not in the endpoint's /models list" + (
                    f" ({len(served)} other models listed)" if served else " (endpoint unreachable or empty)"
                )
                results.append(row)
                continue
        request = ModelRequest(
            messages=[
                Message(role="system", content="You extract requirement identifiers. Reply with JSON only."),
                Message(role="user", content=PROBE_TEXT),
            ],
            json_schema=PROBE_SCHEMA,
            max_output_tokens=200,
            timeout_s=timeout_s,
        )
        start = time.perf_counter()
        try:
            import json

            resp = provider.generate(entry, request)
            data = json.loads(resp.text.strip().removeprefix("```json").removesuffix("```").strip())
            errors = [e.message for e in Draft202012Validator(PROBE_SCHEMA).iter_errors(data)]
            row.update(
                schema_valid=not errors,
                answer_correct=not errors
                and sorted(data["requirement_ids"]) == ["REQ-1", "REQ-2"]
                and data["has_ambiguity"] is True,
                structured_output_mode=getattr(provider, "schema_mode", "n/a"),
                usage_reported=not resp.usage_estimated,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                latency_ms=resp.latency_ms,
            )
            row["ok"] = bool(row["schema_valid"])
            if errors:
                row["error"] = "output did not match the schema: " + "; ".join(errors[:3])
        except (ModelError, ValueError, KeyError, TypeError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["latency_ms"] = int((time.perf_counter() - start) * 1000)
        results.append(row)
    return results
