"""Business Analyst skills: brd-analysis and transcript-analysis."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from agentic_sdlc.models.types import ModelError
from agentic_sdlc.requirements import llm_extraction, rules
from agentic_sdlc.requirements.llm_extraction import SYSTEM_PROMPT, norm
from agentic_sdlc.skills.base import SkillContext, SkillError, SkillImplementation, SkillOutputInvalid

__all__ = ["SYSTEM_PROMPT", "BrdAnalysis", "TranscriptAnalysis", "norm"]


def quotes_match_blocks(inputs: dict[str, Any], output: dict[str, Any], ctx: SkillContext) -> list[str]:
    """Every cited block must exist and the quote must appear verbatim (whitespace-insensitive) in it.

    This is the anti-fabrication check: a model cannot invent provenance.
    """
    doc = ctx.tools.call("documents.read", document_version_id=inputs["document_version_id"])
    return llm_extraction.quote_problems(output, {b["block_id"]: norm(b["text"]) for b in doc["blocks"]})


def unique_keys(inputs: dict[str, Any], output: dict[str, Any], ctx: SkillContext) -> list[str]:
    return llm_extraction.duplicate_keys(output)


def _document_normalized(inputs: dict[str, Any], ctx: SkillContext) -> str | None:
    doc = ctx.tools.call("documents.read", document_version_id=inputs["document_version_id"])
    return None if doc["blocks"] else "document has no extractable blocks"


class _ExtractionSkill(SkillImplementation):
    strategies: ClassVar[set[str]] = {"heuristic", "llm"}
    document_kind: ClassVar[str]

    def preconditions(self) -> dict[str, Callable[[dict[str, Any], SkillContext], str | None]]:
        return {"document_normalized": _document_normalized}

    def postconditions(
        self,
    ) -> dict[str, Callable[[dict[str, Any], dict[str, Any], SkillContext], list[str]]]:
        return {"quotes_match_blocks": quotes_match_blocks, "unique_keys": unique_keys}

    def run(self, strategy: str, inputs: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        doc = ctx.tools.call("documents.read", document_version_id=inputs["document_version_id"])
        kind = inputs.get("document_kind") or doc["document_kind"]
        if strategy == "heuristic":
            return rules.extract(doc["blocks"], document_kind=kind)
        if strategy == "llm":
            return self._llm(doc, kind, ctx)
        raise SkillOutputInvalid(f"unsupported strategy {strategy}")

    @staticmethod
    def _llm(doc: dict[str, Any], kind: str, ctx: SkillContext) -> dict[str, Any]:
        model = ctx.model
        assert model is not None
        schema = ctx.manifest.output_schema

        def generate(system: str, user: str, attempt: int, max_output_tokens: int) -> dict[str, Any]:
            try:
                return model.structured(
                    system,
                    user,
                    schema,
                    purpose="execution" if attempt == 1 else "retry",
                    max_output_tokens=max_output_tokens,
                )
            except SkillOutputInvalid as exc:
                raise llm_extraction.SectionOutputInvalid(str(exc), getattr(exc, "errors", None)) from exc
            except ModelError as exc:
                if not exc.retryable:
                    raise
                raise llm_extraction.SectionOutputInvalid(f"model error: {exc}", feedback=False) from exc

        try:
            return llm_extraction.extract(
                doc["blocks"],
                document_kind=kind,
                title=doc["title"],
                schema=schema,
                generate=generate,
                max_attempts=ctx.manifest.retry.max_attempts,
                warnings=ctx.warnings,
                before_section=lambda _s: ctx.check_deadline(),
            )
        except llm_extraction.ExtractionFailed as exc:
            raise SkillError(str(exc)) from exc


class BrdAnalysis(_ExtractionSkill):
    document_kind = "brd"


class TranscriptAnalysis(_ExtractionSkill):
    document_kind = "transcript"
