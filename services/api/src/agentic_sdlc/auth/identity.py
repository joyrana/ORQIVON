"""Identity configuration: OIDC providers and directory-group → role mapping.

Declared in the file `IDENTITY_CONFIG_FILE` points to (see
`infrastructure/config/identity.example.yaml`). Client secrets come only from environment variables.

```yaml
oidc_providers:
  - name: entra
    display_name: Microsoft Entra ID
    issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
    client_id: 00000000-0000-0000-0000-000000000000
    client_secret_env: OIDC_ENTRA_SECRET
    groups_claim: groups
    allowed_email_domains: [example.com]
group_roles:
  - group: asw-admins
    admin: true
  - group: payments-engineers
    project: PAY
    role: engineer
```

Roles granted from groups are *managed*: they are recomputed whenever a user's groups change (at
sign-in, or through SCIM), so removing someone from a group removes the role. Roles granted by a
project owner in the UI are never touched by group sync.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentic_sdlc.core.settings import get_settings

PROJECT_ROLES = ("project_owner", "engineer", "reviewer", "qa", "release_approver", "auditor")


class OidcProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    issuer: str
    client_id: str
    client_secret_env: str | None = None  # absent: public client (PKCE only)
    client_auth: Literal["client_secret_basic", "client_secret_post", "none"] = "client_secret_basic"
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    groups_claim: str | None = "groups"
    email_claim: str = "email"
    name_claim: str = "name"
    require_email_verified: bool = True  # tokens without email_verified=true are refused
    allowed_email_domains: list[str] = Field(default_factory=list)  # empty: any domain
    auto_provision: bool = True  # create users on first sign-in (otherwise SCIM/admin only)
    link_by_email: bool = False  # attach first sign-in to an existing user with the same email
    clock_skew_s: int = Field(default=60, ge=0, le=300)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", v):
            raise ValueError("provider name must be lowercase letters, digits or '-'")
        return v

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, v: str) -> str:
        insecure = get_settings().oidc_insecure_http_issuers
        if not v.startswith("https://") and not any(v.startswith(i) for i in insecure):
            raise ValueError("issuer must be https")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _secret(self) -> OidcProvider:
        if self.client_auth != "none" and not self.client_secret_env:
            raise ValueError("a confidential client needs client_secret_env (or set client_auth: none)")
        return self


class GroupRole(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    admin: bool = False
    project: str | None = None  # project key
    role: str | None = None

    @model_validator(mode="after")
    def _target(self) -> GroupRole:
        if self.admin == bool(self.project or self.role):
            raise ValueError("a group mapping grants either admin: true, or a project and role")
        if not self.admin and (not self.project or self.role not in PROJECT_ROLES):
            raise ValueError(f"project mappings need a project key and a role in {PROJECT_ROLES}")
        return self


class IdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oidc_providers: list[OidcProvider] = Field(default_factory=list)
    group_roles: list[GroupRole] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> IdentityConfig:
        names = [p.name for p in self.oidc_providers]
        if len(names) != len(set(names)):
            raise ValueError("duplicate OIDC provider name")
        return self

    def provider(self, name: str) -> OidcProvider | None:
        return next((p for p in self.oidc_providers if p.name == name), None)


@lru_cache(maxsize=1)
def identity_config() -> IdentityConfig:
    path = get_settings().identity_config_file
    if not path:
        return IdentityConfig()
    return IdentityConfig.model_validate(yaml.safe_load(Path(path).read_text()) or {})
