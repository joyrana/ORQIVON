"""Secret detection: provider-specific token formats plus high-entropy credential assignments.

Precision matters as much as recall: a gate that cries wolf gets switched off. So generic
assignments need entropy and must not look like a placeholder, and test fixtures are reported
at low severity. Matches are redacted before they leave this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from agentic_sdlc.security.findings import Finding, Severity, redact


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: Severity
    pattern: re.Pattern[str]
    group: int = 0  # the capture group holding the secret


RULES: tuple[Rule, ...] = (
    Rule(
        "private-key",
        "Private key",
        "critical",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
    Rule(
        "aws-access-key-id",
        "AWS access key ID",
        "critical",
        re.compile(r"\b((?:AKIA|ASIA)[A-Z0-9]{16})\b"),
        1,
    ),
    Rule(
        "aws-secret-access-key",
        "AWS secret access key",
        "critical",
        re.compile(r"(?i)aws.{0,20}?(?:secret|private).{0,20}?['\"]([A-Za-z0-9/+=]{40})['\"]"),
        1,
    ),
    Rule(
        "github-token",
        "GitHub token",
        "critical",
        re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b"),
        1,
    ),
    Rule("gitlab-token", "GitLab token", "critical", re.compile(r"\b(glpat-[A-Za-z0-9_-]{20,})\b"), 1),
    Rule("slack-token", "Slack token", "high", re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})\b"), 1),
    Rule(
        "slack-webhook",
        "Slack webhook URL",
        "high",
        re.compile(r"(https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,})"),
        1,
    ),
    Rule(
        "stripe-live-key",
        "Stripe live key",
        "critical",
        re.compile(r"\b((?:sk|rk)_live_[A-Za-z0-9]{20,})\b"),
        1,
    ),
    Rule("google-api-key", "Google API key", "high", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"), 1),
    Rule(
        "azure-storage-key",
        "Azure storage account key",
        "critical",
        re.compile(r"AccountKey=([A-Za-z0-9+/]{80,120}={0,2})(?![A-Za-z0-9+/=])"),
        1,
    ),
    Rule(
        "openai-api-key",
        "Model provider API key",
        "high",
        re.compile(r"\b(sk-(?:proj-|ant-api\d\d-)?[A-Za-z0-9_-]{32,})\b"),
        1,
    ),
    Rule(
        "jwt",
        "JSON Web Token",
        "medium",
        re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
        1,
    ),
    Rule(
        "connection-string-password",
        "Password in a connection string",
        "high",
        re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^/\s:@'\"]{1,64}:([^@/\s'\"]{6,})@[A-Za-z0-9.-]+"),
        1,
    ),
)

GENERIC = re.compile(
    r"""(?ix)
    (?P<name>[a-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|
        client[_-]?secret|private[_-]?key|token))
    ["']?\s*(?::|=|:=|=>)\s*
    (?P<q>["'])(?P<value>[^"'\s]{8,200})(?P=q)
    """
)
PLACEHOLDER = re.compile(
    r"(?i)^(?:x+|\*+|\.+|<[^>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|%\([^)]*\)s|"
    r".*(?:changeme|change_me|example|placeholder|your[_-]|dummy|redacted|replace|xxxx|todo|fake|sample|"
    r"password|secret|test).*)$"
)
ALLOW_MARKER = "asw:allow-secret"
SKIP_SUFFIXES = (".lock", "-lock.json", ".sum", ".min.js", ".map", ".svg")
MAX_BYTES = 1_000_000


def entropy(s: str) -> float:
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def _is_test_path(path: str) -> bool:
    p = "/" + path.lower()
    return any(
        k in p
        for k in ("/test", "/tests/", "/spec/", "/__tests__/", "/fixtures/", "_test.", ".test.", ".spec.")
    )


def _connection_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.match(value)) or value.startswith(("$", "{"))


def scan_text(path: str, text: str, added_lines: set[int] | None = None) -> list[Finding]:
    """Findings for one file. `added_lines` marks which lines the change introduced."""
    if path.endswith(SKIP_SUFFIXES) or "\x00" in text[:8000]:
        return []
    out: list[Finding] = []
    test_file = _is_test_path(path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line or len(line) > 5000:
            continue
        seen_spans: list[tuple[int, int]] = []
        value_spans: list[tuple[int, int]] = []
        for rule in RULES:
            for m in rule.pattern.finditer(line):
                value = m.group(rule.group)
                a, b = m.span(rule.group)
                if rule.id == "connection-string-password" and (
                    _connection_placeholder(value) or any(x < b and a < y for x, y in value_spans)
                ):
                    continue  # a provider token in a URL is reported once, by its own rule
                seen_spans.append(m.span())
                value_spans.append((a, b))
                out.append(
                    _finding(rule.id, rule.title, rule.severity, path, lineno, value, test_file, added_lines)
                )
        for m in GENERIC.finditer(line):
            if any(a <= m.start("value") < b for a, b in seen_spans):
                continue
            value = m.group("value")
            if PLACEHOLDER.match(value) or entropy(value) < 3.0 or value.isdigit():
                continue
            if re.fullmatch(r"[a-z_][a-z0-9_.]*\(?", value, re.I) and not re.search(r"\d", value):
                continue  # an identifier or function name, not a literal secret
            if re.fullmatch(r"[a-z]+(?:[-_.][a-z]+)+", value):
                continue  # a resource name ("app-db-credentials"), not the credential itself
            out.append(
                _finding(
                    "generic-credential",
                    f"Hard-coded credential ({m.group('name')})",
                    "medium",
                    path,
                    lineno,
                    value,
                    test_file,
                    added_lines,
                )
            )
    return out


def _finding(
    rule_id: str,
    title: str,
    severity: Severity,
    path: str,
    line: int,
    value: str,
    test_file: bool,
    added: set[int] | None,
) -> Finding:
    return Finding(
        scanner="secrets",
        rule_id=rule_id,
        severity="low" if test_file and severity != "critical" else severity,
        title=title,
        path=path,
        line=line,
        message=(
            f"{title} committed to the repository. Remove it, rotate the credential, and load it "
            "from the environment or a secret store."
        ),
        excerpt=redact(value),
        introduced=bool(added and line in added),
        extra={"match_key": _digest(value)},
    )


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:24]


def scan_files(files: Iterable[tuple[str, str]], added: dict[str, set[int]]) -> list[Finding]:
    out: list[Finding] = []
    for path, text in files:
        out.extend(scan_text(path, text, added.get(path)))
    return out
