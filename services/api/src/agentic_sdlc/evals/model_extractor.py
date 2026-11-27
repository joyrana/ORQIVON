"""Model-backed requirement extraction for the evaluation harness (`--strategy llm`).

Runs the same section-by-section extraction (prompt, repair, validation, retries and merge) as the
`brd-analysis` skill's `llm` strategy, so the measured quality is the quality the workflow gets. A
document whose extraction fails counts as an empty extraction: a model that answers out of contract is
scored as having answered nothing usable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agentic_sdlc.models.registry import ModelRegistry
from agentic_sdlc.models.types import Message, ModelError, ModelRequest
from agentic_sdlc.requirements import llm_extraction
from agentic_sdlc.skills.base import _json_text
from agentic_sdlc.skills.registry import get_skill_registry


@dataclass
class ModelExtractor:
    registry: ModelRegistry
    model_id: str
    calls: int = 0
    invalid: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    usage_estimated: bool = False
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.entry = self.registry.get(self.model_id)
        provider = self.registry.provider_for(self.entry)
        if provider is None or not self.registry.is_available(self.entry):
            raise SystemExit(f"model '{self.model_id}' is not available; run `agentic-sdlc models check`")
        self.provider = provider
        manifest = get_skill_registry().get("brd-analysis")
        self.schema = manifest.output_schema
        self.max_attempts = manifest.retry.max_attempts
        self.timeout_s = manifest.timeout_s * self.registry.timeout_scale(self.entry)

    def _generate(self, system: str, user: str, attempt: int, max_output_tokens: int) -> dict[str, Any]:
        request = ModelRequest(
            messages=[Message(role="system", content=system), Message(role="user", content=user)],
            json_schema=self.schema,
            max_output_tokens=min(8000, self.entry.max_output_tokens, max_output_tokens),
            timeout_s=self.timeout_s,
        )
        self.calls += 1
        try:
            resp = self.provider.generate(self.entry, request)
        except ModelError as exc:
            if not exc.retryable:
                raise
            raise llm_extraction.SectionOutputInvalid(f"model error: {exc}", feedback=False) from exc
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        self.cost_usd += self.entry.cost(resp.input_tokens, resp.output_tokens)
        self.usage_estimated = self.usage_estimated or resp.usage_estimated
        try:
            out = json.loads(_json_text(resp.text))
        except json.JSONDecodeError as exc:
            raise llm_extraction.SectionOutputInvalid("model returned invalid JSON", [str(exc)]) from exc
        if not isinstance(out, dict):
            raise llm_extraction.SectionOutputInvalid("model output must be a JSON object")
        return out

    def __call__(self, blocks: list[dict[str, Any]], *, document_kind: str) -> dict[str, Any]:
        empty: dict[str, Any] = {"requirements": [], "business_rules": [], "questions": []}
        try:
            out = llm_extraction.extract(
                blocks,
                document_kind=document_kind,
                title="",
                schema=self.schema,
                generate=self._generate,
                max_attempts=self.max_attempts,
                warnings=[],
            )
        except (llm_extraction.ExtractionFailed, ModelError) as exc:
            self.invalid += 1
            self.errors.append(str(exc)[:500])
            return empty
        # The scorer compares quotes with block text exactly; the extraction accepts whitespace differences.
        text_by_id = {b["block_id"]: b["text"] for b in blocks}
        for item in [*out["requirements"], *out["business_rules"], *out["questions"]]:
            text = text_by_id.get(item.get("block_id") or "")
            if text is not None and llm_extraction.norm(item.get("quote") or "") == llm_extraction.norm(text):
                item["quote"] = text
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "model_ref": self.entry.ref,
            "calls": self.calls,
            "invalid_outputs": self.invalid,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_estimated": self.usage_estimated,
            "cost_usd": round(self.cost_usd, 6),
            "errors": self.errors[:10],
        }
