"""Deployment targets: hand an approved release to the customer's own pipeline and follow it.

Declared by the operator next to the work-tool connections (`deploy_targets:` in INTEGRATIONS_FILE):

    deploy_targets:
      - name: gha-staging
        kind: github_actions             # github_actions | jenkins | azure_pipelines | argocd
        environments: [staging]
        base_url: https://api.github.com
        auth: bearer
        token_env: GITHUB_DEPLOY_TOKEN   # fine-grained: Actions read & write on the repo
        repo: acme/payments
        workflow: deploy.yml             # must set `run-name: ${{ inputs.asw_deployment_id }}`
        ref: main
        health_url: https://staging.payments.acme.internal/healthz

The platform triggers the pipeline with the release's version, commit and artifact checksum, polls
it until it finishes, and records the outcome. It never holds deployment credentials beyond the
trigger token, and production stays behind `production_deploy_enabled` and its own approval.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import yaml
from pydantic import model_validator

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.integrations.worktools import HttpEndpoint, IntegrationError, _insecure_ok

TargetKind = Literal["github_actions", "jenkins", "azure_pipelines", "argocd"]
Environment = Literal["staging", "production"]


@dataclass(frozen=True)
class DeployRequest:
    deployment_id: str
    environment: str
    version: str
    commit: str
    artifact_sha256: str

    def params(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "release_version": self.version,
            "commit": self.commit,
            "artifact_sha256": self.artifact_sha256,
            "asw_deployment_id": self.deployment_id,
        }


@dataclass
class DeployState:
    state: Literal["running", "succeeded", "failed"]
    detail: str
    url: str | None = None


class DeployTarget(HttpEndpoint):
    kind: TargetKind
    environments: list[Environment] = ["staging"]
    health_url: str | None = None
    deploy_timeout_s: int = 3600
    # github_actions
    repo: str | None = None
    workflow: str | None = None
    ref: str = "main"
    # jenkins
    job: str | None = None
    # azure_pipelines
    project: str | None = None
    pipeline_id: int | None = None
    # argocd
    application: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> DeployTarget:
        if self.kind == "github_actions" and not self.base_url:
            self.base_url = "https://api.github.com"
        self._check_base_url(f"{self.kind} targets")
        self._check_auth()
        need = {
            "github_actions": ("repo", "workflow"),
            "jenkins": ("job",),
            "azure_pipelines": ("project", "pipeline_id"),
            "argocd": ("application",),
        }[self.kind]
        missing = [f for f in need if not getattr(self, f)]
        if missing:
            raise ValueError(f"{self.kind} targets need {', '.join(missing)}")
        if self.repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo):
            raise ValueError("repo must be owner/name")
        if self.health_url and not (self.health_url.startswith("https://") or _insecure_ok(self.health_url)):
            raise ValueError("health_url must use https")
        return self

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "environments": list(self.environments),
            "host": urlsplit(self.base_url or "").hostname,
            "credential_available": self.credential_available(),
            "health_url": self.health_url,
        }

    def _pipeline(self) -> str:
        return f"/{quote(self.project or '', safe='')}/_apis/pipelines/{self.pipeline_id}"

    # --- trigger ---------------------------------------------------------------------------------

    def trigger(self, req: DeployRequest) -> dict[str, Any]:
        """Start the pipeline; returns the reference used to follow it."""
        if self.kind == "github_actions":
            self.request(
                "POST",
                f"/repos/{self.repo}/actions/workflows/{quote(self.workflow or '', safe='')}/dispatches",
                json={"ref": self.ref, "inputs": req.params()},
                headers={"Accept": "application/vnd.github+json"},
            )
            started = (datetime.now(UTC) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"deployment_id": req.deployment_id, "since": started}
        if self.kind == "jenkins":
            path = "".join(f"/job/{quote(part, safe='')}" for part in (self.job or "").split("/") if part)
            r = self.send(
                "POST", f"{path}/buildWithParameters", params={k.upper(): v for k, v in req.params().items()}
            )
            queue = r.headers.get("location")
            if not queue:
                raise IntegrationError(f"{self.name}: Jenkins did not return a queue item", retryable=False)
            return {"queue_url": queue.rstrip("/") + "/"}
        if self.kind == "azure_pipelines":
            out = self.request(
                "POST",
                f"{self._pipeline()}/runs?api-version=7.1",
                json={
                    "resources": {"repositories": {"self": {"refName": f"refs/heads/{self.ref}"}}},
                    "templateParameters": req.params(),
                },
            )
            return {"run_id": out["id"], "url": ((out.get("_links") or {}).get("web") or {}).get("href")}
        # argocd: sync the application to the release commit
        self.request(
            "POST",
            f"/api/v1/applications/{quote(self.application or '', safe='')}/sync",
            json={"revision": req.commit, "prune": False, "dryRun": False},
        )
        return {"revision": req.commit}

    # --- status ----------------------------------------------------------------------------------

    def status(self, ref: dict[str, Any]) -> DeployState:
        if self.kind == "github_actions":
            runs = self.request(
                "GET",
                f"/repos/{self.repo}/actions/runs",
                params={"event": "workflow_dispatch", "created": f">={ref['since']}", "per_page": 50},
                headers={"Accept": "application/vnd.github+json"},
            ).get("workflow_runs", [])
            run = next((r for r in runs if ref["deployment_id"] in (r.get("display_title") or "")), None)
            if run is None:
                return DeployState("running", "waiting for the workflow run to appear")
            if run.get("status") != "completed":
                return DeployState("running", f"workflow {run.get('status')}", run.get("html_url"))
            ok = run.get("conclusion") == "success"
            return DeployState(
                "succeeded" if ok else "failed", f"workflow {run.get('conclusion')}", run.get("html_url")
            )
        if self.kind == "jenkins":
            if "build_url" not in ref:
                item = self.request("GET", f"{ref['queue_url']}api/json")
                if item.get("cancelled"):
                    return DeployState("failed", "the queued build was cancelled")
                executable = item.get("executable")
                if not executable:
                    return DeployState("running", f"queued: {item.get('why') or 'waiting'}")
                ref["build_url"] = executable["url"].rstrip("/") + "/"
            build = self.request("GET", f"{ref['build_url']}api/json")
            if build.get("building") or build.get("result") is None:
                return DeployState("running", "build running", ref["build_url"])
            ok = build["result"] == "SUCCESS"
            return DeployState("succeeded" if ok else "failed", f"build {build['result']}", ref["build_url"])
        if self.kind == "azure_pipelines":
            run = self.request(
                "GET",
                f"{self._pipeline()}/runs/{ref['run_id']}?api-version=7.1",
            )
            url = ((run.get("_links") or {}).get("web") or {}).get("href") or ref.get("url")
            if run.get("state") != "completed":
                return DeployState("running", f"run {run.get('state')}", url)
            ok = run.get("result") == "succeeded"
            return DeployState("succeeded" if ok else "failed", f"run {run.get('result')}", url)
        app = self.request("GET", f"/api/v1/applications/{quote(self.application or '', safe='')}")
        st = app.get("status") or {}
        phase = (st.get("operationState") or {}).get("phase")
        revision = (st.get("sync") or {}).get("revision")
        health = (st.get("health") or {}).get("status")
        url = f"{self.base_url}/applications/{self.application}"
        if phase in ("Failed", "Error"):
            return DeployState(
                "failed", f"sync {phase}: {(st.get('operationState') or {}).get('message', '')}"[:300], url
            )
        if phase == "Succeeded" and revision == ref["revision"]:
            if health == "Healthy":
                return DeployState("succeeded", f"synced {revision[:12]}, healthy", url)
            if health == "Degraded":
                return DeployState("failed", "synced but degraded", url)
        return DeployState("running", f"sync {phase or 'pending'}, health {health or 'unknown'}", url)

    def check_health(self) -> dict[str, Any]:
        if not self.health_url:
            return {"name": "health_url", "ok": None, "detail": "no health URL configured for this target"}
        import httpx

        try:
            r = httpx.get(self.health_url, timeout=15, follow_redirects=False)
            return {"name": "health_url", "ok": 200 <= r.status_code < 300, "detail": f"HTTP {r.status_code}"}
        except httpx.HTTPError as exc:
            return {"name": "health_url", "ok": False, "detail": type(exc).__name__}


@lru_cache(maxsize=1)
def load_targets() -> dict[str, DeployTarget]:
    s = get_settings()
    raw: list[dict[str, Any]] = []
    if s.integrations_file:
        raw += (yaml.safe_load(Path(s.integrations_file).read_text()) or {}).get("deploy_targets", [])
    if s.deploy_targets:
        raw += json.loads(s.deploy_targets)
    targets = [DeployTarget.model_validate(t) for t in raw]
    if len({t.name for t in targets}) != len(targets):
        raise ValueError("duplicate deployment target name")
    return {t.name: t for t in targets}


def get_target(name: str | None, environment: str) -> DeployTarget:
    t = load_targets().get(name or "")
    if t is None or environment not in t.environments:
        raise ValidationFailed(
            "unknown deployment target for this environment",
            details={
                "target": name,
                "available": sorted(n for n, x in load_targets().items() if environment in x.environments),
            },
        )
    return t
