"""Runtime configuration. All values come from environment variables (prefix-free) or `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[5]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    database_url: str = "postgresql+psycopg://sdlc:sdlc@localhost:5432/sdlc"

    # Storage and working directories
    data_dir: Path = REPO_ROOT / "var"
    skills_dir: Path = REPO_ROOT / "skills"
    fixtures_dir: Path = REPO_ROOT / "samples"
    model_registry_path: Path = Path(__file__).resolve().parents[1] / "config" / "models.yaml"

    # Authentication
    auth_mode: Literal["token"] = "token"
    dev_seed_users: bool = True
    token_ttl_days: int = 30

    # Uploads
    max_upload_bytes: int = 10 * 1024 * 1024
    max_docx_entries: int = 2000
    max_docx_uncompressed_bytes: int = 60 * 1024 * 1024

    # Rate limiting (per token, per process)
    rate_limit_per_minute: int = 600

    # Sandbox
    sandbox_backend: Literal["namespace", "docker", "none"] = "namespace"
    sandbox_uid: int = 64000
    sandbox_masked_paths: list[str] = Field(
        default_factory=lambda: ["/root", "/etc/agentic-sdlc", "/var/lib/postgresql", "/home"]
    )
    # Toolchain available inside the sandbox (must be outside masked paths). `make sandbox-toolchain`
    # creates /opt/asw-sandbox with pytest; container images install it into the system Python.
    sandbox_python: str = "/opt/asw-sandbox/bin/python"
    # Disposable workspaces live outside every masked path so the sandbox can see them.
    workspaces_root: Path = Path("/var/tmp/asw-workspaces")  # noqa: S108 - dedicated, 0711
    sandbox_timeout_s: int = 120
    sandbox_memory_bytes: int = 2 * 1024**3
    sandbox_cpu_seconds: int = 120
    sandbox_max_processes: int = 128
    sandbox_max_file_bytes: int = 100 * 1024 * 1024
    sandbox_docker_image: str = "python:3.12-slim"
    sandbox_docker_runtime: str | None = None  # e.g. "runsc" (gVisor)
    # Extra toolchain directories on the sandbox PATH (Go, Node, JDK, .NET...). Each must be owned by
    # root and not writable by the sandbox user; the sandbox refuses to start otherwise. Empty: the
    # directories of `go`, `node`, `java`, `mvn`, `gradle` and `dotnet` found on the worker's PATH.
    sandbox_tool_paths: list[str] | None = None
    # Read-only dependency caches tests may use offline, by name: m2 (Maven), gradle, nuget, npm.
    # Tests never get network access; dependencies must be vendored or present in these caches.
    sandbox_dependency_caches: dict[str, str] = Field(default_factory=dict)
    # Upper bound for runtimes that reserve large virtual memory (JVM, .NET) and ask for more than
    # sandbox_memory_bytes of address space.
    sandbox_max_address_space_bytes: int = 16 * 1024**3
    # Namespace backend: build a fresh root (read-only /usr, toolchains, caches, a minimal /etc,
    # private /dev /proc /tmp, the workspace) and pivot_root into it, so no other host file is visible.
    # False falls back to masking `sandbox_masked_paths` on the shared host root (weaker).
    sandbox_isolated_root: bool = True
    sandbox_extra_ro_paths: list[str] = Field(default_factory=list)
    # /etc entries tools need, bound read-only (globs allowed): dynamic linker cache, CA bundle,
    # Debian alternatives (e.g. /usr/bin/java), JDK configuration.
    sandbox_root_etc: list[str] = Field(
        default_factory=lambda: [
            "/etc/ld.so.cache",
            "/etc/ld.so.conf",
            "/etc/ld.so.conf.d",
            "/etc/alternatives",
            "/etc/java-*",
            "/etc/ssl/certs",
            "/etc/localtime",
            "/etc/mime.types",
        ]
    )

    # Model layer
    model_endpoint_url: str | None = None  # OpenAI-compatible endpoint (e.g. vLLM)
    model_endpoint_api_key: str | None = None
    allow_mock_models: bool = False  # mock provider may be routed to only when explicitly enabled
    # Whether skills may send project content to models on endpoints declared `privacy: external`
    # (hosted APIs). Default: only self-hosted models. Projects can narrow this, never widen it.
    model_privacy_policy: Literal["self_hosted_only", "any"] = "self_hosted_only"
    # Single sign-on and provisioning (auth/identity.py). public_base_url is where the API is
    # reachable from browsers (OIDC redirect URI); web_base_url is the web app (post-sign-in).
    identity_config_file: str | None = None
    public_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:5173"
    sso_session_hours: int = Field(default=12, ge=1, le=24 * 7)
    oidc_insecure_http_issuers: list[str] = Field(default_factory=list)  # tests and labs only
    # Name of the variable holding the token the IdP's SCIM client authenticates with.
    scim_token_env: str = "SCIM_BEARER_TOKEN"  # noqa: S105 - a variable name, not a secret
    # Git hosting connections (integrations/git_hosting.py): a YAML file and/or a JSON list.
    git_connections_file: str | None = None
    git_connections: str | None = None
    # Hosts that may be reached over plain http for git and hosting APIs (tests and air-gapped labs
    # only; never in production).
    git_insecure_http_hosts: list[str] = Field(default_factory=list)
    # How long a run waits for external CI on its pull request, and how often it checks.
    external_ci_timeout_s: int = 7200
    external_ci_poll_s: int = Field(default=60, ge=1)

    # Deployment
    production_deploy_enabled: bool = False
    github_actions_deploy_repo: str | None = None
    github_actions_deploy_workflow: str | None = None

    # Workflow default limits
    max_repair_attempts: int = 3
    max_architecture_revisions: int = 3
    max_model_calls: int = 200
    max_tokens: int = 2_000_000
    max_cost_usd: float = 25.0
    max_wall_clock_s: int = 3600

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app_env == "production" and self.dev_seed_users:
            raise ValueError("dev_seed_users must be false when APP_ENV=production")
        if self.app_env == "production":
            for name in ("public_base_url", "web_base_url"):
                if not getattr(self, name).startswith("https://"):
                    raise ValueError(f"{name} must be https when APP_ENV=production")
            for name in ("oidc_insecure_http_issuers", "git_insecure_http_hosts"):
                if getattr(self, name):
                    raise ValueError(f"{name} must be empty when APP_ENV=production")
        return self

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def workspaces_dir(self) -> Path:
        return self.workspaces_root

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"


@lru_cache
def get_settings() -> Settings:
    return Settings()
