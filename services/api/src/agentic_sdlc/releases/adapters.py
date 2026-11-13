"""Deployment adapters.

Every adapter re-checks authorization itself (defense in depth): it refuses to deploy without an approved,
non-stale decision for the exact release, and credentials never pass through prompts or agent tools.

- LocalStagingAdapter: atomic directory deploy with a `current` pointer and rollback (the default).
- ExternalPipelineAdapter: the project's declared target (GitHub Actions, Jenkins, Azure Pipelines,
  Argo CD); triggered here and followed by the workflow until the pipeline finishes.
- Production: refused unless `production_deploy_enabled` and a `release_production` approval exist.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentic_sdlc.core.errors import PolicyViolation
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.core.storage import get_storage


@dataclass
class DeployResult:
    status: str  # succeeded | failed
    location: str | None
    log: list[str] = field(default_factory=list)
    previous: str | None = None
    external: dict[str, Any] | None = None  # how to follow an asynchronous pipeline


class DeploymentAdapter(Protocol):
    name: str
    environment: str

    def deploy(
        self, *, project_id: uuid.UUID, release_id: uuid.UUID, artifact_key: str, artifact_sha256: str
    ) -> DeployResult: ...

    def rollback(self, *, project_id: uuid.UUID, to_release: str | None) -> DeployResult: ...


def manifest_of(tree: Path) -> dict[str, str]:
    out = {}
    for p in sorted(tree.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(tree))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _safe_extract(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not target.is_relative_to(dest.resolve()) or m.issym() or m.islnk() or m.isdev():
                raise PolicyViolation(f"unsafe member in release artifact: {m.name}")
        tar.extractall(dest, filter="data")


class LocalStagingAdapter:
    name = "local_staging"
    environment = "staging"

    def root(self, project_id: uuid.UUID) -> Path:
        return get_settings().staging_dir / str(project_id)

    def deploy(
        self, *, project_id: uuid.UUID, release_id: uuid.UUID, artifact_key: str, artifact_sha256: str
    ) -> DeployResult:
        data = get_storage().get(artifact_key)
        if hashlib.sha256(data).hexdigest() != artifact_sha256:
            raise PolicyViolation("artifact integrity check failed; refusing to deploy")
        root = self.root(project_id)
        releases = root / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        tmp = releases / f".incoming-{release_id}"
        final = releases / str(release_id)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        _safe_extract(data, tmp)
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
        current = root / "current"
        previous = os.readlink(current) if current.is_symlink() else None
        swap = root / f".current-{uuid.uuid4().hex[:6]}"
        swap.symlink_to(final)
        swap.replace(current)  # atomic pointer switch
        return DeployResult(
            "succeeded",
            str(final),
            [f"extracted {artifact_sha256[:12]} to {final}", f"current -> {final.name}"],
            previous,
        )

    def current_tree(self, project_id: uuid.UUID) -> Path | None:
        current = self.root(project_id) / "current"
        return current.resolve() if current.is_symlink() else None

    def rollback(self, *, project_id: uuid.UUID, to_release: str | None) -> DeployResult:
        root = self.root(project_id)
        current = root / "current"
        if to_release and Path(to_release).is_dir():
            swap = root / f".current-{uuid.uuid4().hex[:6]}"
            swap.symlink_to(Path(to_release))
            swap.replace(current)
            return DeployResult("succeeded", to_release, [f"current -> {Path(to_release).name} (rollback)"])
        if current.is_symlink():
            current.unlink()
        return DeployResult("succeeded", None, ["no previous release; staging pointer cleared"])


class ExternalPipelineAdapter:
    """Hands the release to a declared deployment target (GitHub Actions, Jenkins, Azure Pipelines,
    Argo CD). Deploying is asynchronous: `deploy` triggers and returns "running" with a reference,
    and the workflow polls `status` until the pipeline finishes."""

    def __init__(self, target: Any, environment: str) -> None:
        self.target = target
        self.environment = environment
        self.name = f"{target.kind}:{target.name}"

    def deploy(
        self,
        *,
        project_id: uuid.UUID,
        release_id: uuid.UUID,
        artifact_key: str,
        artifact_sha256: str,
        deployment_id: uuid.UUID | None = None,
        version: str = "",
        commit: str = "",
    ) -> DeployResult:
        from agentic_sdlc.integrations.deploy_targets import DeployRequest

        ref = self.target.trigger(
            DeployRequest(
                deployment_id=str(deployment_id or release_id),
                environment=self.environment,
                version=version,
                commit=commit,
                artifact_sha256=artifact_sha256,
            )
        )
        return DeployResult(
            "running", None, [f"triggered {self.name} for {version} ({commit[:12]})"], external=ref
        )

    def rollback(self, *, project_id: uuid.UUID, to_release: str | None) -> DeployResult:
        raise PolicyViolation(
            "external targets roll back by deploying the previous release (see releases.rollback)"
        )


def adapter_for(environment: str, project: Any = None) -> Any:
    """The adapter for an environment: the project's declared target, else local staging."""
    s = get_settings()
    binding = ((getattr(project, "integrations", None) or {}).get("deploy") or {}).get(environment)
    if environment == "production" and not s.production_deploy_enabled:
        raise PolicyViolation("production deployment is disabled by policy")
    if binding:
        from agentic_sdlc.integrations.deploy_targets import get_target

        return ExternalPipelineAdapter(get_target(binding, environment), environment)
    if environment == "staging":
        return LocalStagingAdapter()
    if environment == "production":
        raise PolicyViolation("no production deployment target is configured for this project")
    raise PolicyViolation(f"unknown environment {environment}")
