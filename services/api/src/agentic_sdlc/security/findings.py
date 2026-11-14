"""The finding model shared by every scanner, and the policy that decides what blocks a release."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["critical", "high", "medium", "low"]
SEVERITIES: tuple[Severity, ...] = ("critical", "high", "medium", "low")
RANK = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # low=0 ... critical=3


@dataclass
class Finding:
    scanner: str  # secrets | code | dependencies | sarif:<tool>
    rule_id: str
    severity: Severity
    title: str
    path: str
    line: int | None
    message: str
    # Redacted excerpt: secrets never leave the scanner in clear text.
    excerpt: str = ""
    introduced: bool = False  # on a line (or a dependency) the change added
    fingerprint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.fingerprint:
            # Stable across attempts and line moves: rule, file and the normalized matched text.
            key = "|".join([self.scanner, self.rule_id, self.path, self.extra.get("match_key", self.excerpt)])
            self.fingerprint = hashlib.sha256(key.encode()).hexdigest()[:32]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SecurityPolicy:
    """Per-project policy. Only findings the change introduced can block; existing ones are reported."""

    block_severity: Severity = "high"
    block_secrets: bool = True  # any introduced secret blocks, whatever its severity
    require_dependency_scan: bool = False  # treat "no advisory source configured" as blocking

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SecurityPolicy:
        data = data or {}
        sev = data.get("block_severity", "high")
        if sev not in SEVERITIES:
            raise ValueError(f"block_severity must be one of {', '.join(SEVERITIES)}")
        return cls(
            block_severity=sev,
            block_secrets=bool(data.get("block_secrets", True)),
            require_dependency_scan=bool(data.get("require_dependency_scan", False)),
        )

    def blocks(self, f: Finding) -> bool:
        if not f.introduced:
            return False
        if f.scanner == "secrets" and self.block_secrets:
            return True
        return RANK[f.severity] >= RANK[self.block_severity]


def redact(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * min(12, len(secret) - 6)}{secret[-2:]}"
