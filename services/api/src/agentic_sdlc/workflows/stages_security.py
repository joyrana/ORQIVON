"""Security gate: regression → security_scan → (create security defects → implement)* → publish.

The Security agent scans the cumulative change (run base..head). Findings the change introduced
that the project's policy blocks become defects and go back to the Developer agent through the
same bounded repair loop as failing tests. A scan that could not complete (an analyser failed,
a required advisory source is missing) escalates to a person: it is never treated as a pass.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agentic_sdlc.backlog.service import next_key as next_backlog_key
from agentic_sdlc.events import service as events
from agentic_sdlc.orm import BacklogItem, Defect, Project, SecurityScan
from agentic_sdlc.skills.runtime import SkillRuntime
from agentic_sdlc.workflows.delivery import DeliveryState, agent_ctx, escalate_now, node, stage
from agentic_sdlc.workflows.stages_build import _attempt, _change_for

DEFECT_SEVERITY = {"critical": "critical", "high": "major", "medium": "minor", "low": "minor"}
PREFIX = "security:"


@node("security_scan")
def security_scan(state: DeliveryState) -> DeliveryState:
    with stage(state["run_id"], "security_scan") as (db, run, budget):
        attempt = _attempt(run)
        scan = db.scalar(
            select(SecurityScan).where(SecurityScan.run_id == run.id, SecurityScan.attempt == attempt)
        )
        if scan is None:
            out: dict[str, Any] = (
                SkillRuntime()
                .execute(
                    "security-scanning",
                    {
                        "repository_id": run.config["repository_id"],
                        "base_commit": run.outcome["base_commit"],
                        "head_commit": run.outcome["head_commit"],
                    },
                    agent_ctx(db, run, "security", "security_scan", budget),
                )
                .output
            )
            scan = SecurityScan(
                project_id=run.project_id,
                run_id=run.id,
                attempt=attempt,
                base_commit=out["base_commit"],
                head_commit=out["head_commit"],
                status=out["status"],
                findings=out["findings"],
                blocking=out["blocking"],
                scanners=out["scanners"],
                problems=out["problems"],
                policy=out["policy"],
            )
            db.add(scan)
            db.flush()
            events.emit(
                db,
                type="security.scanned",
                project_id=run.project_id,
                run_id=run.id,
                payload={
                    "attempt": attempt,
                    "status": scan.status,
                    "findings": len(scan.findings),
                    "introduced": sum(1 for f in scan.findings if f["introduced"]),
                    "blocking": len(scan.blocking),
                },
                artifact_refs=[{"type": "security_scan", "id": str(scan.id)}],
            )
        run.outcome = {
            **run.outcome,
            "security": {
                "scan_id": str(scan.id),
                "status": scan.status,
                "commit": scan.head_commit,
                "blocking": len(scan.blocking),
            },
        }
        if scan.status == "error":
            escalate_now(
                db, run, "security_scan", "security scan could not complete: " + "; ".join(scan.problems)
            )
            return {"halt": True}
        security_defects = db.scalars(
            select(Defect).where(Defect.run_id == run.id, Defect.dedup_key.startswith(PREFIX))
        ).all()
        if scan.status == "passed":
            for d in security_defects:
                if d.status in ("open", "fixed"):
                    d.status = "verified"
                    d.fixed_in_attempt = d.fixed_in_attempt or attempt
                    bug = db.get(BacklogItem, d.backlog_item_id) if d.backlog_item_id else None
                    if bug is not None:
                        bug.status = "done"
            return {"outcome": "ok"}

        # Blocked: one defect per blocking finding (reopened if it came back), then repair.
        change = _change_for(db, run, attempt)
        assert change is not None
        by_key = {d.dedup_key: d for d in security_defects}
        findings = {f["fingerprint"]: f for f in scan.findings}
        project = db.get(Project, run.project_id)
        assert project is not None
        for fp in scan.blocking:
            f = findings[fp]
            key = PREFIX + fp
            where = f"{f['path']}:{f['line']}" if f.get("line") else f["path"]
            existing = by_key.get(key)
            if existing is not None:
                if existing.status != "open":
                    existing.status = "open"
                    existing.fixed_in_attempt = None
                continue
            bug = BacklogItem(
                project_id=run.project_id,
                run_id=run.id,
                parent_id=change.backlog_item_id,
                key=next(next_backlog_key(db, project)),
                kind="bug",
                title=f"Security: {f['title']} ({where})"[:300],
                description=f["message"][:4000],
                priority=1,
                labels=["defect", "security", f["severity"]],
                origin="generated",
                assigned_agent="developer",
            )
            db.add(bug)
            db.flush()
            defect = Defect(
                project_id=run.project_id,
                run_id=run.id,
                dedup_key=key,
                backlog_item_id=bug.id,
                source_item_id=change.backlog_item_id,
                code_change_id=change.id,
                severity=DEFECT_SEVERITY[f["severity"]],
                title=bug.title,
                reproduction_steps=[
                    f"Security scan of {scan.base_commit[:12]}..{scan.head_commit[:12]}",
                    f"{f['scanner']} rule {f['rule_id']} at {where}",
                ],
                expected=(
                    "The change introduces no committed secret, dangerous construct or vulnerable "
                    "dependency at or above the project's blocking severity."
                ),
                actual=f"{f['message']}" + (f"\nExcerpt: {f['excerpt']}" if f.get("excerpt") else ""),
                evidence={
                    "security_scan_id": str(scan.id),
                    "fingerprint": fp,
                    "scanner": f["scanner"],
                    "rule_id": f["rule_id"],
                    "severity": f["severity"],
                },
                status="open",
                found_in_attempt=attempt,
            )
            db.add(defect)
            db.flush()
            events.emit(
                db,
                type="defect.created",
                project_id=run.project_id,
                run_id=run.id,
                payload={
                    "title": defect.title,
                    "severity": defect.severity,
                    "attempt": attempt,
                    "security": True,
                },
                artifact_refs=[{"type": "defect", "id": str(defect.id)}],
            )
        repairs_used = attempt - 1
        limit = int(run.config["limits"]["max_repair_attempts"])
        if repairs_used >= limit:
            escalate_now(
                db,
                run,
                "security_scan",
                f"repair limit reached ({limit} repair attempt(s)); {len(scan.blocking)} security finding(s) "
                "still block. Fix them in the repository, or accept a finding as a risk with a reason; "
                "then start a new run.",
            )
            return {"halt": True}
        run.counters = {**run.counters, "attempt": attempt + 1, "repairs": repairs_used + 1}
    return {"outcome": "repair"}
