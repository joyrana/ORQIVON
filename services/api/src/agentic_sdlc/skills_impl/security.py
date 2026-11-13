"""security-scanning (security agent): secrets, code patterns, dependency advisories, SARIF tools."""

from __future__ import annotations

from typing import Any, ClassVar

from agentic_sdlc.skills.base import SkillContext, SkillImplementation


class SecurityScanning(SkillImplementation):
    strategies: ClassVar[set[str]] = {"heuristic"}

    def run(self, strategy: str, inputs: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        result: dict[str, Any] = ctx.tools.call(
            "security.scan",
            repository_id=inputs["repository_id"],
            base_commit=inputs["base_commit"],
            head_commit=inputs["head_commit"],
        )
        return result
