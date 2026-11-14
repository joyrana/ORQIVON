"""Scan a change: the repository at the change's head, with "introduced" judged against its base.

Scanners and what they cover:

* secrets       - every tracked text file at head (existing secrets are reported, new ones block);
* code          - built-in dangerous-pattern rules on the files the change touched;
* dependencies  - pinned packages at head against the configured OSV advisories;
* sarif:<tool>  - analysers the project configures (Semgrep, CodeQL, Bandit, gosec...), run in the
                  sandbox with no network, reporting SARIF.

The decision is the project's `SecurityPolicy`: only findings the change introduced can block,
accepted findings (by fingerprint, with a recorded reason) do not, and a scanner that could not
run makes the result "error" rather than a silent pass.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.execution.sandbox import get_sandbox
from agentic_sdlc.execution.test_runner import export_tree
from agentic_sdlc.execution.workspace import git, new_dir
from agentic_sdlc.security import code_rules, dependencies, sarif, secrets
from agentic_sdlc.security.findings import Finding, SecurityPolicy

MAX_FILES = 20_000
MAX_FILE_BYTES = 1_000_000
TOOL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass
class ScanReport:
    status: str  # passed | blocked | error
    base_commit: str
    head_commit: str
    findings: list[dict[str, Any]]
    blocking: list[str]  # fingerprints
    scanners: dict[str, dict[str, Any]]  # name -> {status, findings, detail}
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def added_lines(origin: Path, base: str, head: str) -> dict[str, set[int]]:
    """Line numbers (in the head version) that the change added, per file."""
    diff = git(
        origin, "diff", "--no-color", "--no-ext-diff", "--unified=0", "--no-renames", base, head, timeout=300
    )
    out: dict[str, set[int]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@") and path is not None:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or "1")
                out.setdefault(path, set()).update(range(start, start + count))
    return out


def validate_tools(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list) or len(tools) > 10:
        raise ValueError("tools must be a list of at most 10 analysers")
    out = []
    for t in tools:
        if not isinstance(t, dict):
            raise ValueError("each tool is an object with name, command and sarif")
        name, command, report = t.get("name"), t.get("command"), t.get("sarif")
        if not isinstance(name, str) or not TOOL_NAME.match(name):
            raise ValueError("tool name: lowercase letters, digits, - and _")
        if not (isinstance(command, list) and command and all(isinstance(a, str) and a for a in command)):
            raise ValueError(f"{name}: command must be a non-empty list of strings (no shell)")
        if not isinstance(report, str) or report.startswith("/") or ".." in Path(report).parts:
            raise ValueError(f"{name}: sarif must be a path inside the repository")
        timeout = int(t.get("timeout_s", 600))
        if not 10 <= timeout <= 3600:
            raise ValueError(f"{name}: timeout_s must be between 10 and 3600")
        out.append({"name": name, "command": command, "sarif": report, "timeout_s": timeout})
    return out


def advisory_source() -> dependencies.AdvisorySource | None:
    s = get_settings()
    if s.security_advisory_dir:
        return dependencies.OfflineDatabase(Path(s.security_advisory_dir))
    if s.security_osv_api_url:
        return dependencies.OsvApi(s.security_osv_api_url)
    return None


def _show(origin: Path, commit: str, path: str) -> str | None:
    try:
        return git(origin, "show", f"{commit}:{path}", timeout=60)
    except Exception:
        return None


def scan_change(
    origin: Path,
    base: str,
    head: str,
    *,
    policy: SecurityPolicy,
    tools: list[dict[str, Any]] | None = None,
    accepted: set[str] | None = None,
) -> ScanReport:
    accepted = accepted or set()
    added = added_lines(origin, base, head)
    findings: list[Finding] = []
    scanners: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    work = new_dir("scan")
    tree = work / "tree"
    tree.mkdir()
    try:
        tracked = export_tree(origin, head, tree)[:MAX_FILES]

        def read(rel: str) -> str | None:
            p = tree / rel
            if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                return None
            data = p.read_bytes()
            return None if b"\x00" in data[:8000] else data.decode("utf-8", errors="replace")

        texts = {rel: t for rel in tracked if (t := read(rel)) is not None}

        found = secrets.scan_files(texts.items(), added)
        findings += found
        scanners["secrets"] = {"status": "ok", "findings": len(found), "files": len(texts)}

        found = []
        for rel in sorted(added):
            if rel in texts:
                found += code_rules.scan_text(rel, texts[rel], added[rel])
        findings += found
        scanners["code"] = {"status": "ok", "findings": len(found), "files": len(added)}

        source = advisory_source()
        manifests = [rel for rel in texts if dependencies.is_manifest(rel)]
        if source is None:
            scanners["dependencies"] = {
                "status": "not_configured",
                "findings": 0,
                "detail": "no advisory source (SECURITY_ADVISORY_DIR or SECURITY_OSV_API_URL)",
            }
            if policy.require_dependency_scan:
                problems.append(
                    "dependency scanning is required by policy but no advisory source is configured"
                )
        else:
            head_pkgs = [p for rel in manifests for p in dependencies.parse_manifest(rel, texts[rel])]
            base_keys = {
                p.key
                for rel in manifests
                if (old := _show(origin, base, rel)) is not None
                for p in dependencies.parse_manifest(rel, old)
            }
            try:
                found = dependencies.findings_for(head_pkgs, base_keys, source)
                findings += found
                scanners["dependencies"] = {
                    "status": "ok",
                    "findings": len(found),
                    "packages": len({p.key for p in head_pkgs}),
                    "source": source.name,
                }
            except Exception as exc:
                scanners["dependencies"] = {"status": "error", "findings": 0, "detail": str(exc)[:300]}
                problems.append(f"dependency scan failed: {str(exc)[:200]}")

        for tool in tools or []:
            findings_t, status = _run_tool(tool, tree, added)
            findings += findings_t
            scanners[f"sarif:{tool['name']}"] = {
                "status": "ok" if status is None else "error",
                "findings": len(findings_t),
            }
            if status is not None:
                scanners[f"sarif:{tool['name']}"]["detail"] = status
                problems.append(f"{tool['name']}: {status}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    unique: dict[str, Finding] = {}
    for f in findings:
        prev = unique.get(f.fingerprint)
        if prev is None or (f.introduced and not prev.introduced):
            unique[f.fingerprint] = f
    ordered = sorted(
        unique.values(),
        key=lambda f: (
            not f.introduced,
            -("low", "medium", "high", "critical").index(f.severity),
            f.path,
            f.line or 0,
        ),
    )
    blocking = [f.fingerprint for f in ordered if policy.blocks(f) and f.fingerprint not in accepted]
    status = "error" if problems else "blocked" if blocking else "passed"
    return ScanReport(
        status=status,
        base_commit=base,
        head_commit=head,
        findings=[f.as_dict() for f in ordered],
        blocking=blocking,
        scanners=scanners,
        problems=problems,
    )


def _run_tool(
    tool: dict[str, Any], tree: Path, added: dict[str, set[int]]
) -> tuple[list[Finding], str | None]:
    report = tree / tool["sarif"]
    report.parent.mkdir(parents=True, exist_ok=True)
    r = get_sandbox().run(tool["command"], tree, timeout_s=tool["timeout_s"], env={})
    if r.timed_out:
        return [], f"timed out after {tool['timeout_s']} s"
    if not report.is_file():
        tail = (r.stderr or r.stdout)[-300:].strip()
        return (
            [],
            f"produced no SARIF report at {tool['sarif']} (exit {r.exit_code}){': ' + tail if tail else ''}",
        )
    try:
        doc = json.loads(report.read_text()[:50_000_000])
        return sarif.parse(doc, tool["name"], added), None
    except (ValueError, TypeError, AttributeError) as exc:
        return [], f"unreadable SARIF: {str(exc)[:200]}"
