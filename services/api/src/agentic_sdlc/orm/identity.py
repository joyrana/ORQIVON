from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentic_sdlc.core.db import Base, IdMixin, TimestampMixin

ROLES = ("project_owner", "engineer", "reviewer", "qa", "release_approver", "auditor")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Directory groups (from SCIM, or the OIDC groups claim) that drive managed roles.
    groups: Mapped[list[Any]] = mapped_column(default=list)
    scim_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(200))  # SCIM externalId
    admin_source: Mapped[str | None] = mapped_column(String(20))  # "directory" when granted by a group

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class ApiToken(IdMixin, TimestampMixin, Base):
    __tablename__ = "api_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class Membership(IdMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id", "role"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | directory

    user: Mapped[User] = relationship(back_populates="memberships")


class ExternalIdentity(IdMixin, TimestampMixin, Base):
    """An identity-provider subject linked to a user. Sign-in matches on (provider, subject)."""

    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    provider: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class OidcLoginState(Base):
    """A pending sign-in: single use, expires in minutes. The state itself is stored hashed."""

    __tablename__ = "oidc_login_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    nonce: Mapped[str] = mapped_column(String(100))
    code_verifier: Mapped[str] = mapped_column(String(200))
    return_to: Mapped[str] = mapped_column(String(500), default="/")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DirectoryGroup(IdMixin, TimestampMixin, Base):
    """A group provisioned over SCIM. Members' `groups` mirror its display name."""

    __tablename__ = "directory_groups"

    display_name: Mapped[str] = mapped_column(String(200), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    members: Mapped[list[Any]] = mapped_column(default=list)  # user ids (strings)
