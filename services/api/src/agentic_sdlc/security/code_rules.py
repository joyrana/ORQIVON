"""Built-in code pattern rules: the dangerous calls a reviewer would flag on sight.

These are line patterns, not data-flow analysis. They are deliberately few and specific, run on
every repository with no setup, and only block on lines the change added. For deeper analysis,
configure a SARIF tool (Semgrep, CodeQL, Bandit, gosec, SpotBugs) in the project's security
settings; its findings go through the same policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentic_sdlc.security.findings import Finding, Severity

LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".ts": "js",
    ".tsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".go": "go",
    ".java": "java",
    ".kt": "java",
    ".cs": "csharp",
}
COMMENT = {"python": "#", "js": "//", "go": "//", "java": "//", "csharp": "//"}


@dataclass(frozen=True)
class CodeRule:
    id: str
    langs: tuple[str, ...]
    severity: Severity
    title: str
    pattern: re.Pattern[str]
    fix: str
    cwe: str


RULES: tuple[CodeRule, ...] = (
    CodeRule(
        "py-eval",
        ("python",),
        "high",
        "Dynamic code execution",
        re.compile(r"(?<![\w.])(?:eval|exec)\s*\((?!\s*\))"),
        "Parse the input explicitly (ast.literal_eval, json) instead of executing it.",
        "CWE-95",
    ),
    CodeRule(
        "py-shell-true",
        ("python",),
        "high",
        "Shell command built from a string",
        re.compile(r"subprocess\.\w+\(.*shell\s*=\s*True"),
        "Pass an argument list with shell=False.",
        "CWE-78",
    ),
    CodeRule(
        "py-os-system",
        ("python",),
        "high",
        "os.system / os.popen",
        re.compile(r"\bos\.(?:system|popen)\s*\("),
        "Use subprocess.run with an argument list.",
        "CWE-78",
    ),
    CodeRule(
        "py-pickle",
        ("python",),
        "high",
        "Deserializing untrusted data with pickle",
        re.compile(r"\b(?:c?pickle|dill|shelve)\.loads?\s*\("),
        "Use JSON or a schema-validated format for data that crosses a trust boundary.",
        "CWE-502",
    ),
    CodeRule(
        "py-yaml-load",
        ("python",),
        "high",
        "yaml.load without a safe loader",
        re.compile(r"\byaml\.(?:load|load_all)\s*\((?![^)]*Loader\s*=\s*(?:yaml\.)?(?:Safe|CSafe)Loader)"),
        "Use yaml.safe_load.",
        "CWE-502",
    ),
    CodeRule(
        "py-sql-format",
        ("python",),
        "high",
        "SQL built with string formatting",
        re.compile(r"\.execute(?:many)?\(\s*(?:f[\"']|[\"'][^\"']*[\"']\s*(?:%|\.format\(|\+))", re.I),
        "Use bound parameters.",
        "CWE-89",
    ),
    CodeRule(
        "py-tls-verify-off",
        ("python",),
        "high",
        "TLS certificate verification disabled",
        re.compile(r"verify\s*=\s*False|_create_unverified_context|CERT_NONE"),
        "Keep verification on; trust a private CA with verify=<bundle path>.",
        "CWE-295",
    ),
    CodeRule(
        "py-weak-hash",
        ("python",),
        "low",
        "Weak hash (MD5/SHA-1)",
        re.compile(r"hashlib\.(?:md5|sha1)\((?![^)]*usedforsecurity\s*=\s*False)"),
        "Use SHA-256 or better; mark non-security uses with usedforsecurity=False.",
        "CWE-328",
    ),
    CodeRule(
        "py-bind-all",
        ("python",),
        "low",
        "Server bound to all interfaces",
        re.compile(r"""host\s*=\s*["']0\.0\.0\.0["']"""),
        "Bind to localhost unless the service must be reachable.",
        "CWE-1327",
    ),
    CodeRule(
        "js-eval",
        ("js",),
        "high",
        "Dynamic code execution",
        re.compile(r"(?<![\w.])eval\s*\(|new\s+Function\s*\("),
        "Parse the input instead of executing it.",
        "CWE-95",
    ),
    CodeRule(
        "js-child-process",
        ("js",),
        "high",
        "Shell command built from a string",
        re.compile(r"\b(?:exec|execSync)\s*\(\s*(?:`[^`]*\$\{|[\"'][^\"']*[\"']\s*\+)"),
        "Use execFile/spawn with an argument array.",
        "CWE-78",
    ),
    CodeRule(
        "js-inner-html",
        ("js",),
        "medium",
        "HTML injection sink",
        re.compile(r"\.(?:innerHTML|outerHTML)\s*=(?!=)|dangerouslySetInnerHTML|document\.write\s*\("),
        "Render text with textContent, or sanitize with a vetted library.",
        "CWE-79",
    ),
    CodeRule(
        "js-tls-off",
        ("js",),
        "high",
        "TLS certificate verification disabled",
        re.compile(r"rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*[\"']?0"),
        "Keep verification on; add the private CA with the ca option.",
        "CWE-295",
    ),
    CodeRule(
        "go-tls-off",
        ("go",),
        "high",
        "TLS certificate verification disabled",
        re.compile(r"InsecureSkipVerify\s*:\s*true"),
        "Keep verification on; add the private CA to RootCAs.",
        "CWE-295",
    ),
    CodeRule(
        "go-shell",
        ("go",),
        "high",
        "Shell command built from a string",
        re.compile(r"exec\.Command(?:Context)?\((?:ctx,\s*)?\"(?:ba|z)?sh\",\s*\"-c\""),
        "Call the program directly with separate arguments.",
        "CWE-78",
    ),
    CodeRule(
        "go-sql-format",
        ("go",),
        "high",
        "SQL built with string formatting",
        re.compile(r"\.(?:Query|QueryRow|Exec)(?:Context)?\((?:ctx,\s*)?fmt\.Sprintf\("),
        "Use placeholders ($1, ?) and pass values as arguments.",
        "CWE-89",
    ),
    CodeRule(
        "java-runtime-exec",
        ("java",),
        "high",
        "Runtime.exec with a command string",
        re.compile(r"Runtime\.getRuntime\(\)\.exec\(\s*(?:\"[^\"]*\"\s*\+|[a-z]\w*\s*\))"),
        "Use ProcessBuilder with an argument list.",
        "CWE-78",
    ),
    CodeRule(
        "java-deserialization",
        ("java",),
        "high",
        "Java native deserialization",
        re.compile(r"new\s+ObjectInputStream\s*\(|XMLDecoder\s*\("),
        "Use a data format with an explicit schema.",
        "CWE-502",
    ),
    CodeRule(
        "java-sql-concat",
        ("java",),
        "high",
        "SQL built with string concatenation",
        re.compile(r"\.(?:executeQuery|executeUpdate|execute|prepareStatement)\(\s*\"[^\"]*\"\s*\+"),
        "Use PreparedStatement parameters.",
        "CWE-89",
    ),
    CodeRule(
        "cs-binaryformatter",
        ("csharp",),
        "high",
        "BinaryFormatter deserialization",
        re.compile(r"\bBinaryFormatter\b|\bNetDataContractSerializer\b"),
        "Use System.Text.Json.",
        "CWE-502",
    ),
    CodeRule(
        "cs-tls-off",
        ("csharp",),
        "high",
        "TLS certificate validation disabled",
        re.compile(r"ServerCertificate(?:Custom)?ValidationCallback\s*=.*=>\s*true"),
        "Validate certificates; trust a private CA explicitly.",
        "CWE-295",
    ),
)


def language(path: str) -> str | None:
    for suffix, lang in LANG_BY_SUFFIX.items():
        if path.endswith(suffix):
            return lang
    return None


def scan_text(path: str, text: str, added_lines: set[int] | None = None) -> list[Finding]:
    lang = language(path)
    if lang is None:
        return []
    marker = COMMENT[lang]
    out: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith((marker, "*", "/*")) or "asw:allow-code" in line:
            continue
        code = line.split(f" {marker} ", 1)[0]
        for rule in RULES:
            if lang in rule.langs and rule.pattern.search(code):
                out.append(
                    Finding(
                        scanner="code",
                        rule_id=rule.id,
                        severity=rule.severity,
                        title=rule.title,
                        path=path,
                        line=lineno,
                        message=f"{rule.title} ({rule.cwe}). {rule.fix}",
                        excerpt=stripped[:200],
                        introduced=bool(added_lines and lineno in added_lines),
                        extra={"cwe": rule.cwe, "match_key": " ".join(stripped.split())},
                    )
                )
    return out
