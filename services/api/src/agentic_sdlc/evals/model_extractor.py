"""Model-backed requirement extraction for the evaluation harness (`--strategy llm`).

Uses the same prompt, schema and anti-fabrication check (every quote must exist in its cited block)
as the `brd-analysis` skill's `llm` strategy, so the measured quality is the quality the workflow
gets. An output that fails validation counts as an empty extraction for that case: a model that
answers out of contract is scored as having answered nothing usable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from agentic_sdlc.models.registry import ModelRegistry
from agentic_sdlc.models.types import Message, ModelError, ModelRequest
from agentic_sdlc.skills.base import _json_text
from agentic_sdlc.skills.registry import get_skill_registry
from agentic_sdlc.skills_impl.requirements import SYSTEM_PROMPT, _norm


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
        self.schema = get_skill_registry().get("brd-analysis").output_schema

    def __call__(self, blocks: list[dict[str, Any]], *, document_kind: str) -> dict[str, Any]:
        empty: dict[str, Any] = {"requirements": [], "business_rules": [], "questions": []}
        payload = "\n".join(
            f"[{b['block_id']}] ({' > '.join(b.get('section_path', [])) or 'root'}) {b['text']}"
            for b in blocks
        )
        user = f"Document kind: {document_kind}\n<document>\n{payload}\n</document>"
        request = ModelRequest(
            messages=[Message(role="system", content=SYSTEM_PROMPT), Message(role="user", content=user)],
            json_schema=self.schema,
            max_output_tokens=min(8000, self.entry.max_output_tokens),
            timeout_s=300,
        )
        self.calls += 1
        try:
            resp = self.provider.generate(self.entry, request)
        except ModelError as exc:
            self.invalid += 1
            self.errors.append(str(exc))
            return empty
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        self.cost_usd += self.entry.cost(resp.input_tokens, resp.output_tokens)
        self.usage_estimated = self.usage_estimated or resp.usage_estimated
        try:
            out = json.loads(_json_text(resp.text))
        except json.JSONDecodeError:
            self.invalid += 1
            self.errors.append("invalid JSON")
            return empty
        problems = [e.message for e in Draft202012Validator(self.schema).iter_errors(out)][:3]
        by_id = {b["block_id"]: _norm(b["text"]) for b in blocks}
        for item in [*out.get("requirements", []), *out.get("business_rules", []), *out.get("questions", [])]:
            bid, quote = item.get("block_id"), item.get("quote")
            if bid is not None and (bid not in by_id or not quote or _norm(quote) not in by_id[bid]):
                problems.append(f"fabricated or misplaced quote for {item.get('key') or item.get('kind')}")
        if problems:
            self.invalid += 1
            self.errors.append("; ".join(problems[:3]))
            return empty
        # The scorer compares quotes with block text exactly; the skill accepts whitespace differences.
        for item in [*out["requirements"], *out["business_rules"], *out["questions"]]:
            text = next((b["text"] for b in blocks if b["block_id"] == item.get("block_id")), None)
            if text is not None and _norm(item.get("quote") or "") == _norm(text):
                item["quote"] = text
        return dict(out)

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
