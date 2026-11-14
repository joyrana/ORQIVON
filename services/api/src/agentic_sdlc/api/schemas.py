"""HTTP request/response contracts (Pydantic). Kept separate from ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# --- identity -------------------------------------------------------------------------------------


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool
    project_roles: dict[str, list[str]]


class SystemInfo(BaseModel):
    version: str
    app_env: str
    auth_mode: str
    dev_login_enabled: bool
    sandbox_backend: str
    model_inference: Literal["configured", "unavailable"]
    production_deploy_enabled: bool


class DevUser(BaseModel):
    email: str
    display_name: str
    token: str
    roles_hint: str


# --- projects -------------------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    key: str | None = Field(default=None, max_length=10)
    description: str = ""
    goals: str = ""
    constraints: dict[str, Any] = {}


class ProjectUpdate(BaseModel):
    expected_version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    goals: str | None = None
    constraints: dict[str, Any] | None = None
    environments: list[str] | None = None
    limits: dict[str, Any] | None = None


class ProjectOut(ORMModel):
    id: uuid.UUID
    key: str
    name: str
    description: str
    goals: str
    constraints: dict[str, Any]
    environments: list[Any]
    limits: dict[str, Any]
    is_sample: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ProjectSummary(BaseModel):
    project: ProjectOut
    my_roles: list[str]
    counts: dict[str, int]
    stage: str | None
    active_run: dict[str, Any] | None
    pending_approvals: int
    open_defects: int
    tokens: dict[str, Any]


class MemberAdd(BaseModel):
    email: str
    role: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str


class RepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["fixture", "git"]
    location: str  # fixture name, or an https clone URL
    default_branch: str = "main"
    connection: str | None = None  # git hosting connection name (kind "git")


class RepositoryOut(ORMModel):
    id: uuid.UUID
    name: str
    kind: str
    default_branch: str
    is_sample: bool
    location: str | None = None
    connection: str | None = None
    sync_status: dict[str, Any] = Field(default_factory=dict)
    test_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("location")
    @classmethod
    def _public_location(cls, v: str | None) -> str | None:
        # Sample repositories live in the platform's data directory: do not expose its path.
        return v if v and v.startswith(("https://", "http://")) else None


class RepositoryTestConfigUpdate(BaseModel):
    """Replace the repository's test configuration (empty object: detect automatically)."""

    test_config: dict[str, Any]


class TestProfileOut(BaseModel):
    __test__ = False
    configured: dict[str, Any]
    profile: dict[str, Any] | None
    source: Literal["configured", "detected", "none"]
    commit: str | None
    problem: str | None = None


class EventOut(BaseModel):
    event_id: uuid.UUID
    seq: int
    type: str
    schema_version: int
    project_id: uuid.UUID
    run_id: uuid.UUID | None
    ts: datetime
    correlation_id: str | None
    causation_id: str | None
    artifact_refs: list[dict[str, Any]]
    payload: dict[str, Any]


class AuditOut(ORMModel):
    id: int
    ts: datetime
    actor_id: uuid.UUID | None
    actor_type: str
    action: str
    run_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    correlation_id: str | None
