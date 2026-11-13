from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agentic_sdlc.core.db import Base, IdMixin, TimestampMixin


class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    key: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    goals: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[dict[str, Any]] = mapped_column(default=dict)  # tech/security constraints
    environments: Mapped[list[Any]] = mapped_column(default=lambda: ["development", "staging"])
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)  # per-project workflow limit overrides
    # Security scanning policy and analysers: see agentic_sdlc.security.
    security_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": version}


class ProjectRepository(IdMixin, TimestampMixin, Base):
    __tablename__ = "project_repositories"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # "fixture": a sample repository shipped in samples/ (git-initialized on registration)
    # "git": any git URL the platform is authorized to read (read-only clone)
    kind: Mapped[str] = mapped_column(String(20))
    location: Mapped[str] = mapped_column(Text)  # path for fixture, URL for git
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    # Name of the shipped fixture this repository was created from (samples/repositories/<fixture>).
    fixture: Mapped[str | None] = mapped_column(String(200))
    # How to run this repository's tests (execution/test_profiles.TestConfig). Empty: auto-detected.
    test_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Remote repositories: the deployment's git connection (by name, never a secret) and the state
    # of the platform-managed mirror agents work from.
    connection: Mapped[str | None] = mapped_column(String(63))
    sync_status: Mapped[dict[str, Any]] = mapped_column(default=dict)
