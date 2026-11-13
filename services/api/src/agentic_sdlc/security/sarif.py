"""SARIF 2.1.0 results from external analysers (Semgrep, CodeQL, Bandit, gosec, SpotBugs...)."""

from __future__ import annotations

from typing import Any

from agentic_sdlc.security.findings import Finding, Severity

LEVEL: dict[str, Severity] = {"error": "high", "warning": "medium", "note": "low", "none": "low"}


def _severity(result: dict[str, Any], rule: dict[str, Any]) -> Severity:
    # GitHub's convention: a numeric security-severity (CVSS-like) on the rule wins over level.
    props = {**(rule.get("properties") or {}), **(result.get("properties") or {})}
    score = props.get("security-severity")
    try:
        if score is not None:
            s = float(score)
            return "critical" if s >= 9 else "high" if s >= 7 else "medium" if s >= 4 else "low"
    except (TypeError, ValueError):
        pass
    level = result.get("level") or (rule.get("defaultConfiguration") or {}).get("level") or "warning"
    return LEVEL.get(level, "medium")


def parse(doc: dict[str, Any], tool_name: str, added: dict[str, set[int]]) -> list[Finding]:
    if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):
        raise ValueError("not a SARIF document (no runs)")
    out: list[Finding] = []
    for run in doc["runs"]:
        driver = ((run.get("tool") or {}).get("driver")) or {}
        rules = {r.get("id"): r for r in driver.get("rules") or [] if isinstance(r, dict)}
        for res in run.get("results") or []:
            rule_id = str(res.get("ruleId") or (res.get("rule") or {}).get("id") or "unknown")
            rule = rules.get(rule_id, {})
            loc = ((res.get("locations") or [{}])[0].get("physicalLocation")) or {}
            path = str(((loc.get("artifactLocation") or {}).get("uri")) or "").removeprefix("file://")
            path = path.removeprefix("./")
            line = (loc.get("region") or {}).get("startLine")
            text = ((res.get("message") or {}).get("text")) or rule.get("shortDescription", {}).get(
                "text", ""
            )
            title = (rule.get("shortDescription") or {}).get("text") or rule.get("name") or rule_id
            out.append(
                Finding(
                    scanner=f"sarif:{tool_name}",
                    rule_id=rule_id,
                    severity=_severity(res, rule),
                    title=str(title)[:200],
                    path=path,
                    line=int(line) if isinstance(line, int) else None,
                    message=str(text)[:2000],
                    excerpt=str(((loc.get("region") or {}).get("snippet") or {}).get("text", ""))[:200],
                    introduced=isinstance(line, int) and line in added.get(path, set()),
                    extra={"match_key": f"{path}:{rule_id}:{text}"[:500]},
                )
            )
    return out
