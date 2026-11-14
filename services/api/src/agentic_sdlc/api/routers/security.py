"""Security scanning: scan results per run, the project's policy and analysers, accepted risks."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from agentic_sdlc.api.deps import DB, CurrentPrincipal, project_permission
from agentic_sdlc.audit import service as audit
from agentic_sdlc.core.errors import Conflict, NotFound, PolicyViolation, ValidationFailed
from agentic_sdlc.orm import Project, SecurityAcceptance, SecurityScan
from agentic_sdlc.security.findings import SecurityPolicy
from agentic_sdlc.security.scan import validate_tools

router = APIRouter(tags=["security"])
Read = Annotated[Project, Depends(project_permission("quality.read"))]


class SarifTool(BaseModel):
    name: str
    command: list[str]
    sarif: str
    timeout_s: int = 600


class SecurityConfig(BaseModel):
    block_severity: Literal["critical", "high", "medium", "low"] = "high"
    block_secrets: bool = True
    require_dependency_scan: bool = False
    tools: list[SarifTool] = Field(default_factory=list, max_length=10)


class AcceptanceIn(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    reason: str = Field(min_length=20, max_length=2000)


def _scan(s: SecurityScan, accepted: set[str]) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "run_id": str(s.run_id),
        "attempt": s.attempt,
        "base_commit": s.base_commit,
        "head_commit": s.head_commit,
        "status": s.status,
        "scanners": s.scanners,
        "problems": s.problems,
        "policy": s.policy,
        "blocking": s.blocking,
        "findings": [{**f, "accepted": f["fingerprint"] in accepted} for f in s.findings],
        "created_at": s.created_at.isoformat(),
    }


def _accepted(db: DB, project: Project) -> set[str]:
    return set(
        db.scalars(select(SecurityAcceptance.fingerprint).where(SecurityAcceptance.project_id == project.id))
    )


@router.get("/projects/{project_id}/security-scans")
def security_scans(project: Read, db: DB, run_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    stmt = select(SecurityScan).where(SecurityScan.project_id == project.id)
    if run_id:
        stmt = stmt.where(SecurityScan.run_id == run_id)
    accepted = _accepted(db, project)
    return [_scan(s, accepted) for s in db.scalars(stmt.order_by(SecurityScan.created_at.desc()).limit(50))]


@router.get("/projects/{project_id}/security-config", response_model=SecurityConfig)
def get_security_config(project: Read) -> SecurityConfig:
    return SecurityConfig.model_validate(project.security_config or {})


@router.put("/projects/{project_id}/security-config", response_model=SecurityConfig)
def put_security_config(
    body: SecurityConfig,
    db: DB,
    principal: CurrentPrincipal,
    project: Annotated[Project, Depends(project_permission("security.manage"))],
) -> SecurityConfig:
    data = body.model_dump()
    try:
        SecurityPolicy.from_dict(data)
        data["tools"] = validate_tools(data["tools"])
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    project.security_config = data
    audit.record(
        db,
        action="project.security_config.updated",
        actor_id=principal.user_id,
        actor_type=principal.actor_type,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        details=data,
    )
    db.commit()
    return SecurityConfig.model_validate(data)


@router.get("/projects/{project_id}/security-acceptances")
def acceptances(project: Read, db: DB) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(SecurityAcceptance)
        .where(SecurityAcceptance.project_id == project.id)
        .order_by(SecurityAcceptance.created_at.desc())
    )
    return [
        {
            "id": str(a.id),
            "fingerprint": a.fingerprint,
            "rule_id": a.rule_id,
            "path": a.path,
            "reason": a.reason,
            "accepted_by": str(a.accepted_by) if a.accepted_by else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/projects/{project_id}/security-acceptances", status_code=201)
def accept_risk(
    body: AcceptanceIn,
    db: DB,
    principal: CurrentPrincipal,
    project: Annotated[Project, Depends(project_permission("security.accept_risk"))],
) -> dict[str, Any]:
    if principal.is_service_account:
        raise PolicyViolation("only a person can accept a security risk")
    # Only findings this project's scans actually reported can be accepted.
    finding = None
    for scan in db.scalars(
        select(SecurityScan)
        .where(SecurityScan.project_id == project.id)
        .order_by(SecurityScan.created_at.desc())
    ):
        finding = next((f for f in scan.findings if f["fingerprint"] == body.fingerprint), None)
        if finding:
            break
    if finding is None:
        raise NotFound("no scan of this project reported that finding")
    if db.scalar(
        select(SecurityAcceptance).where(
            SecurityAcceptance.project_id == project.id, SecurityAcceptance.fingerprint == body.fingerprint
        )
    ):
        raise Conflict("this finding is already accepted")
    if finding["scanner"] == "secrets" and finding["severity"] == "critical":
        raise PolicyViolation("a committed credential cannot be accepted: remove it and rotate it")
    row = SecurityAcceptance(
        project_id=project.id,
        fingerprint=body.fingerprint,
        rule_id=finding["rule_id"],
        path=finding["path"],
        reason=body.reason,
        accepted_by=principal.user_id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        action="security.risk_accepted",
        actor_id=principal.user_id,
        actor_type=principal.actor_type,
        project_id=project.id,
        target_type="security_finding",
        target_id=body.fingerprint,
        details={
            "rule_id": finding["rule_id"],
            "severity": finding["severity"],
            "path": finding["path"],
            "reason": body.reason,
        },
    )
    db.commit()
    return {"id": str(row.id), "fingerprint": row.fingerprint, "rule_id": row.rule_id, "path": row.path}


@router.delete("/projects/{project_id}/security-acceptances/{acceptance_id}", status_code=204)
def revoke_acceptance(
    acceptance_id: uuid.UUID,
    db: DB,
    principal: CurrentPrincipal,
    project: Annotated[Project, Depends(project_permission("security.accept_risk"))],
) -> None:
    row = db.get(SecurityAcceptance, acceptance_id)
    if row is None or row.project_id != project.id:
        raise NotFound("acceptance not found")
    audit.record(
        db,
        action="security.risk_revoked",
        actor_id=principal.user_id,
        actor_type=principal.actor_type,
        project_id=project.id,
        target_type="security_finding",
        target_id=row.fingerprint,
        details={"rule_id": row.rule_id, "path": row.path},
    )
    db.delete(row)
    db.commit()
