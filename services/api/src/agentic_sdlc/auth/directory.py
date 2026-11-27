"""Users from an identity provider: provisioning, deactivation and group-derived roles."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_sdlc.audit import service as audit
from agentic_sdlc.auth.identity import identity_config
from agentic_sdlc.core.db import utcnow
from agentic_sdlc.orm import ApiToken, Membership, Project, User

MANAGED = "directory"  # Membership.source for roles derived from directory groups


def sync_roles(db: Session, user: User, *, actor: str) -> dict[str, list[str]]:
    """Make the user's directory-managed roles match their groups. Manual memberships are kept.

    Returns {"granted": [...], "revoked": [...]} as "PROJECT:role" strings (and "admin").
    """
    groups = set(user.groups or [])
    mappings = [m for m in identity_config().group_roles if m.group in groups]
    keys = {m.project for m in mappings if m.project}
    projects = {p.key: p for p in db.scalars(select(Project).where(Project.key.in_(keys)))} if keys else {}
    wanted = {(projects[m.project].id, m.role) for m in mappings if m.project in projects and m.role}
    current = {
        (m.project_id, m.role): m
        for m in db.scalars(
            select(Membership).where(Membership.user_id == user.id, Membership.source == MANAGED)
        )
    }
    manual = {
        (m.project_id, m.role)
        for m in db.scalars(
            select(Membership).where(Membership.user_id == user.id, Membership.source != MANAGED)
        )
    }
    key_of = {p.id: p.key for p in projects.values()}
    for pid, _role in current:
        key_of.setdefault(pid, str(pid))
    granted, revoked = [], []
    for pid, role in wanted - set(current) - manual:
        db.add(Membership(project_id=pid, user_id=user.id, role=role, source=MANAGED))
        granted.append(f"{key_of[pid]}:{role}")
    for pair, m in current.items():
        if pair not in wanted:
            db.delete(m)
            revoked.append(f"{key_of[pair[0]]}:{pair[1]}")
    admin = any(m.admin for m in mappings)
    if user.admin_source == MANAGED or admin:
        if admin != user.is_admin:
            (granted if admin else revoked).append("admin")
        user.is_admin = admin
        user.admin_source = MANAGED if admin else None
    if granted or revoked:
        audit.record(
            db,
            action="user.roles_synced",
            actor_id=None,
            actor_type="system",
            target_type="user",
            target_id=user.id,
            details={"granted": sorted(granted), "revoked": sorted(revoked), "source": actor},
        )
    return {"granted": sorted(granted), "revoked": sorted(revoked)}


def deactivate(db: Session, user: User, *, actor: str) -> None:
    """Disable sign-in and revoke every token at once (offboarding)."""
    user.is_active = False
    now = utcnow()
    for t in db.scalars(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))):
        t.revoked_at = now
    audit.record(
        db,
        action="user.deactivated",
        actor_id=None,
        actor_type="system",
        target_type="user",
        target_id=user.id,
        details={"source": actor},
    )


def new_user(db: Session, *, email: str, display_name: str) -> User:
    """A new person (a directory account; no roles until a mapping or an administrator grants them)."""
    user = User(id=uuid.uuid4(), email=email.lower(), display_name=display_name or email, groups=[])
    db.add(user)
    db.flush()
    return user
