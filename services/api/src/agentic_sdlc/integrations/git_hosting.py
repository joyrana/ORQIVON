"""Git hosting connections: credentials, pull requests and CI status.

A *connection* is declared once per deployment (`GIT_CONNECTIONS_FILE` YAML, or `GIT_CONNECTIONS`
JSON) and referenced by name from a repository. Secrets only ever come from environment variables
or files the operator controls; the database stores the connection *name*.

```yaml
connections:
  - name: github
    provider: github
    auth: github_app                     # short-lived installation tokens (recommended)
    app_id: 123456
    private_key_file: /run/secrets/github-app.pem
  - name: gitlab
    provider: gitlab
    api_url: https://gitlab.example.com/api/v4
    auth: token
    token_env: GITLAB_TOKEN              # project/group access token: read_repository, write_repository, api
```

Credentials are used only by the platform process for fetch, push and API calls. They are passed
to git as a per-URL `http.extraHeader` through `GIT_CONFIG_*` environment variables (never argv,
never written to disk) and never reach the sandbox, prompts, logs or events. A connection's token
is sent only to its own host.

Permissions are separated by operation: `read` (fetch), `push` (create a branch), `pull_request`
(open/update a PR) and `checks` (read CI status). Merging is not an operation the platform has.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.core.settings import get_settings

Provider = Literal["github", "gitlab", "bitbucket", "azure_devops", "generic"]
Operation = Literal["read", "push", "pull_request", "checks"]
ALL_OPERATIONS: tuple[Operation, ...] = ("read", "push", "pull_request", "checks")

DEFAULT_API = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com/api/v4",
    "bitbucket": "https://api.bitbucket.org/2.0",
    "azure_devops": "https://dev.azure.com",
}
DEFAULT_GIT_HOST = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
    "azure_devops": "dev.azure.com",
}


class GitHostingError(Exception):
    """A hosting operation failed. The message never contains credentials."""


class GitConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provider: Provider
    api_url: str | None = None
    git_host: str | None = None  # host in clone URLs, if different from the API host
    auth: Literal["token", "github_app", "none"] = "token"
    token_env: str | None = None
    username: str | None = None  # Bitbucket app passwords need the account name
    app_id: int | None = None
    private_key_env: str | None = None
    private_key_file: str | None = None
    installation_id: int | None = None  # looked up per repository when absent
    operations: list[Operation] = Field(default_factory=lambda: list(ALL_OPERATIONS))
    draft_pull_requests: bool = False
    timeout_s: float = 30

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", v):
            raise ValueError("connection name must be lowercase letters, digits, '.', '_' or '-'")
        return v

    @field_validator("api_url")
    @classmethod
    def _api_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.match(r"^https://", v) and not _insecure_allowed(v):
            raise ValueError("api_url must be https")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _auth_fields(self) -> GitConnection:
        if self.auth == "token" and not self.token_env:
            raise ValueError("auth: token needs token_env (the variable that holds the token)")
        if self.auth == "github_app":
            if self.provider != "github":
                raise ValueError("auth: github_app is only valid for provider github")
            if not self.app_id or not (self.private_key_env or self.private_key_file):
                raise ValueError("auth: github_app needs app_id and private_key_env or private_key_file")
        if self.provider == "generic" and set(self.operations) & {"pull_request", "checks"}:
            self.operations = [o for o in self.operations if o in ("read", "push")]
        return self

    @property
    def api(self) -> str:
        if self.api_url:
            return self.api_url
        if self.provider == "generic":
            return ""
        return DEFAULT_API[self.provider]

    @property
    def host(self) -> str:
        if self.git_host:
            return self.git_host.lower()
        if self.api_url:
            api_host = (urlsplit(self.api_url).hostname or "").lower()
            # SaaS APIs live on their own host; self-managed ones share the git host.
            return {"api.github.com": "github.com", "api.bitbucket.org": "bitbucket.org"}.get(
                api_host, api_host
            )
        return DEFAULT_GIT_HOST.get(self.provider, "")

    def allows(self, op: Operation) -> bool:
        return op in self.operations

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "host": self.host,
            "auth": self.auth,
            "operations": list(self.operations),
            "credential_available": _credential_available(self),
        }


def _insecure_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return url.startswith("http://") and host in get_settings().git_insecure_http_hosts


def _credential_available(conn: GitConnection) -> bool:
    if conn.auth == "none":
        return True
    if conn.auth == "token":
        return bool(conn.token_env and os.environ.get(conn.token_env))
    if conn.private_key_env:
        return bool(os.environ.get(conn.private_key_env))
    return bool(conn.private_key_file and Path(conn.private_key_file).is_file())


@lru_cache(maxsize=1)
def load_connections() -> dict[str, GitConnection]:
    s = get_settings()
    raw: list[dict[str, Any]] = []
    if s.git_connections_file:
        data = yaml.safe_load(Path(s.git_connections_file).read_text()) or {}
        raw += data.get("connections", [])
    if s.git_connections:
        raw += json.loads(s.git_connections)
    conns = [GitConnection.model_validate(c) for c in raw]
    names = [c.name for c in conns]
    if len(names) != len(set(names)):
        raise ValueError("duplicate git connection name")
    return {c.name: c for c in conns}


def get_connection(name: str | None) -> GitConnection:
    conns = load_connections()
    if not name or name not in conns:
        raise ValidationFailed(
            "unknown git connection", details={"connection": name, "available": sorted(conns)}
        )
    return conns[name]


# --- repository coordinates ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoRef:
    """Where a repository lives on its host, parsed from its clone URL."""

    url: str  # canonical https clone URL
    host: str
    path: str  # owner/repo, group/sub/repo, workspace/slug, org/project/_git/repo

    def parts(self) -> list[str]:
        return self.path.split("/")


def parse_clone_url(url: str, conn: GitConnection) -> RepoRef:
    parts = urlsplit(url)
    if parts.scheme != "https" and not _insecure_allowed(url):
        raise ValidationFailed("only https clone URLs are accepted")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValidationFailed("clone URLs must not contain credentials, a query or a fragment")
    host = (parts.hostname or "").lower()
    host_port = f"{host}:{parts.port}" if parts.port else host
    if conn.provider != "generic" and host != conn.host:
        raise ValidationFailed(
            "the clone URL's host does not match the connection",
            details={"url_host": host, "connection_host": conn.host},
        )
    path = parts.path.strip("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9._~-]+(/[A-Za-z0-9._~ %-]+)+", path) or ".." in path.split("/"):
        raise ValidationFailed("the clone URL does not name a repository")
    if conn.provider in ("github", "bitbucket") and len(path.split("/")) != 2:
        raise ValidationFailed(f"{conn.provider} clone URLs look like https://{conn.host}/<owner>/<repo>")
    if conn.provider == "azure_devops" and "/_git/" not in f"/{path}/":
        raise ValidationFailed(
            "Azure DevOps clone URLs look like https://dev.azure.com/<org>/<project>/_git/<repo>"
        )
    return RepoRef(url=f"{parts.scheme}://{host_port}/{path}", host=host, path=path)


# --- credentials ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    username: str
    secret: str
    expires_at: float | None = None  # monotonic deadline for short-lived tokens

    def basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self.username}:{self.secret}".encode()).decode()


_token_cache: dict[tuple[str, str], Credential] = {}
_token_lock = threading.Lock()


def credential(conn: GitConnection, ref: RepoRef, client: httpx.Client | None = None) -> Credential | None:
    """A credential for this repository, minted fresh for GitHub Apps (cached until near expiry)."""
    if conn.auth == "none":
        return None
    if conn.auth == "token":
        token = os.environ.get(conn.token_env or "")
        if not token:
            raise GitHostingError(f"connection '{conn.name}': ${conn.token_env} is not set")
        user = {
            "github": "x-access-token",
            "gitlab": "oauth2",
            "bitbucket": conn.username or "x-token-auth",
            "azure_devops": conn.username or "pat",
            "generic": conn.username or "git",
        }[conn.provider]
        return Credential(user, token)
    key = (conn.name, ref.path.lower())
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached.expires_at and cached.expires_at - time.monotonic() > 300:
            return cached
        cred = _github_app_token(conn, ref, client)
        _token_cache[key] = cred
        return cred


def _github_app_token(conn: GitConnection, ref: RepoRef, client: httpx.Client | None) -> Credential:
    import jwt

    pem = (
        os.environ.get(conn.private_key_env or "")
        if conn.private_key_env
        else Path(conn.private_key_file or "").read_text()
    )
    if not pem:
        raise GitHostingError(f"connection '{conn.name}': GitHub App private key is not available")
    now = int(time.time())
    app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(conn.app_id)}, pem, algorithm="RS256")
    headers = {"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"}
    http = client or httpx.Client()
    installation = conn.installation_id
    if installation is None:
        owner, repo = ref.parts()
        r = http.get(f"{conn.api}/repos/{owner}/{repo}/installation", headers=headers, timeout=conn.timeout_s)
        _raise_for(r, "find the GitHub App installation for this repository")
        installation = int(r.json()["id"])
    # Narrowest token: this one repository, only the permissions the platform uses.
    permissions = {"contents": "write" if conn.allows("push") else "read", "metadata": "read"}
    if conn.allows("pull_request"):
        permissions["pull_requests"] = "write"
    if conn.allows("checks"):
        permissions["checks"] = "read"
        permissions["statuses"] = "read"
    r = http.post(
        f"{conn.api}/app/installations/{installation}/access_tokens",
        headers=headers,
        json={"repositories": [ref.parts()[1]], "permissions": permissions},
        timeout=conn.timeout_s,
    )
    _raise_for(r, "mint a GitHub App installation token")
    body = r.json()
    lifetime = 3600.0
    return Credential("x-access-token", body["token"], expires_at=time.monotonic() + lifetime)


def git_auth_env(ref: RepoRef, cred: Credential | None) -> dict[str, str]:
    """Environment that makes git send the credential to this repository's URL only."""
    if cred is None:
        return {}
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{ref.url}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {cred.basic()}",
    }


def _raise_for(r: httpx.Response, what: str) -> None:
    if r.status_code < 400:
        return
    detail = ""
    try:
        body = r.json()
        detail = str(body.get("message") or body.get("error") or body)[:200] if isinstance(body, dict) else ""
    except ValueError:
        detail = r.text[:200]
    hint = {401: "credential rejected", 403: "permission denied", 404: "not found or no access"}.get(
        r.status_code, ""
    )
    raise GitHostingError(f"could not {what}: HTTP {r.status_code} {hint} {detail}".strip())


# --- pull requests and CI status -----------------------------------------------------------------------


@dataclass
class PullRequest:
    number: str
    url: str
    state: str  # open | merged | closed
    head_branch: str
    created: bool = False  # this call created it (False: an existing one was found)

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "head_branch": self.head_branch,
            "created": self.created,
        }


@dataclass
class CiStatus:
    state: Literal["success", "pending", "failure", "none"]
    checks: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "checks": self.checks[:50]}


class HostingClient:
    """Provider REST calls for one repository. Every call is authenticated with the connection's
    credential for that repository and goes only to the connection's API host."""

    def __init__(self, conn: GitConnection, ref: RepoRef, client: httpx.Client | None = None) -> None:
        self.conn, self.ref = conn, ref
        self.http = client or httpx.Client()

    def _headers(self) -> dict[str, str]:
        cred = credential(self.conn, self.ref, self.http)
        h = {"Accept": "application/json", "User-Agent": "agentic-sdlc-workspace"}
        if cred is None:
            return h
        if self.conn.provider == "github":
            h["Authorization"] = f"Bearer {cred.secret}"
            h["Accept"] = "application/vnd.github+json"
        elif self.conn.provider == "gitlab":
            h["PRIVATE-TOKEN"] = cred.secret
        elif self.conn.provider == "bitbucket" and cred.username == "x-token-auth":
            h["Authorization"] = f"Bearer {cred.secret}"
        else:
            h["Authorization"] = cred.basic()
        return h

    def _req(self, method: str, url: str, what: str, **kw: Any) -> Any:
        if not url.startswith(self.conn.api + "/"):
            raise GitHostingError("refusing to send credentials outside the connection's API")
        try:
            r = self.http.request(method, url, headers=self._headers(), timeout=self.conn.timeout_s, **kw)
        except httpx.HTTPError as exc:
            raise GitHostingError(f"could not {what}: {exc.__class__.__name__}") from exc
        _raise_for(r, what)
        return r.json() if r.content else {}

    # -- paths
    def _gl_project(self) -> str:
        return f"{self.conn.api}/projects/{quote(self.ref.path, safe='')}"

    def _ado(self) -> tuple[str, str, str]:
        org, project, _, repo = self.ref.path.split("/", 3)
        return org, project, repo

    def _ado_base(self) -> str:
        org, project, repo = self._ado()
        return f"{self.conn.api}/{org}/{project}/_apis/git/repositories/{repo}"

    # -- pull requests
    def find_pull_request(self, branch: str) -> PullRequest | None:
        p = self.conn.provider
        if p == "github":
            owner, repo = self.ref.parts()
            items = self._req(
                "GET",
                f"{self.conn.api}/repos/{owner}/{repo}/pulls",
                "list pull requests",
                params={"head": f"{owner}:{branch}", "state": "all"},
            )
            return _gh_pr(items[0]) if items else None
        if p == "gitlab":
            items = self._req(
                "GET",
                f"{self._gl_project()}/merge_requests",
                "list merge requests",
                params={"source_branch": branch, "state": "all"},
            )
            return _gl_pr(items[0]) if items else None
        if p == "bitbucket":
            data = self._req(
                "GET",
                f"{self.conn.api}/repositories/{self.ref.path}/pullrequests",
                "list pull requests",
                params={"q": f'source.branch.name="{branch}"', "state": ["OPEN", "MERGED", "DECLINED"]},
            )
            values = data.get("values", [])
            return _bb_pr(values[0]) if values else None
        if p == "azure_devops":
            data = self._req(
                "GET",
                f"{self._ado_base()}/pullrequests",
                "list pull requests",
                params={
                    "searchCriteria.sourceRefName": f"refs/heads/{branch}",
                    "searchCriteria.status": "all",
                    "api-version": "7.1",
                },
            )
            values = data.get("value", [])
            return _ado_pr(values[0], self.ref) if values else None
        return None

    def open_pull_request(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        """Idempotent: returns the existing pull request for `branch` if there is one."""
        if not self.conn.allows("pull_request"):
            raise GitHostingError(f"connection '{self.conn.name}' is not allowed to open pull requests")
        existing = self.find_pull_request(branch)
        if existing is not None:
            return existing
        p = self.conn.provider
        if p == "github":
            owner, repo = self.ref.parts()
            data = self._req(
                "POST",
                f"{self.conn.api}/repos/{owner}/{repo}/pulls",
                "open a pull request",
                json={
                    "title": title,
                    "head": branch,
                    "base": base,
                    "body": body,
                    "draft": self.conn.draft_pull_requests,
                },
            )
            pr = _gh_pr(data)
        elif p == "gitlab":
            data = self._req(
                "POST",
                f"{self._gl_project()}/merge_requests",
                "open a merge request",
                json={
                    "source_branch": branch,
                    "target_branch": base,
                    "title": ("Draft: " if self.conn.draft_pull_requests else "") + title,
                    "description": body,
                    "remove_source_branch": False,
                },
            )
            pr = _gl_pr(data)
        elif p == "bitbucket":
            data = self._req(
                "POST",
                f"{self.conn.api}/repositories/{self.ref.path}/pullrequests",
                "open a pull request",
                json={
                    "title": title,
                    "description": body,
                    "source": {"branch": {"name": branch}},
                    "destination": {"branch": {"name": base}},
                    "close_source_branch": False,
                },
            )
            pr = _bb_pr(data)
        elif p == "azure_devops":
            data = self._req(
                "POST",
                f"{self._ado_base()}/pullrequests",
                "open a pull request",
                params={"api-version": "7.1"},
                json={
                    "sourceRefName": f"refs/heads/{branch}",
                    "targetRefName": f"refs/heads/{base}",
                    "title": title,
                    "description": body[:4000],  # Azure DevOps limit
                    "isDraft": self.conn.draft_pull_requests,
                },
            )
            pr = _ado_pr(data, self.ref)
        else:
            raise GitHostingError("this connection's provider has no pull-request API; the branch was pushed")
        pr.created = True
        return pr

    # -- CI
    def ci_status(self, sha: str) -> CiStatus:
        if not self.conn.allows("checks"):
            return CiStatus("none", [])
        p = self.conn.provider
        checks: list[dict[str, str]] = []
        if p == "github":
            owner, repo = self.ref.parts()
            runs = self._req(
                "GET",
                f"{self.conn.api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                "read check runs",
                params={"per_page": 100},
            )
            for c in runs.get("check_runs", []):
                state = c.get("conclusion") if c.get("status") == "completed" else "pending"
                checks.append({"name": str(c.get("name")), "state": _norm_state(state)})
            combined = self._req(
                "GET", f"{self.conn.api}/repos/{owner}/{repo}/commits/{sha}/status", "read commit statuses"
            )
            for st in combined.get("statuses", []):
                checks.append({"name": str(st.get("context")), "state": _norm_state(st.get("state"))})
        elif p == "gitlab":
            for st in self._req(
                "GET", f"{self._gl_project()}/repository/commits/{sha}/statuses", "read commit statuses"
            ):
                checks.append({"name": str(st.get("name")), "state": _norm_state(st.get("status"))})
        elif p == "bitbucket":
            data = self._req(
                "GET",
                f"{self.conn.api}/repositories/{self.ref.path}/commit/{sha}/statuses",
                "read commit statuses",
            )
            for st in data.get("values", []):
                checks.append(
                    {"name": str(st.get("name") or st.get("key")), "state": _norm_state(st.get("state"))}
                )
        elif p == "azure_devops":
            data = self._req(
                "GET",
                f"{self._ado_base()}/commits/{sha}/statuses",
                "read commit statuses",
                params={"api-version": "7.1", "latestOnly": "true"},
            )
            for st in data.get("value", []):
                ctx = st.get("context") or {}
                name = "/".join(x for x in (ctx.get("genre"), ctx.get("name")) if x) or "status"
                checks.append({"name": name, "state": _norm_state(st.get("state"))})
        # Several entries per name (re-runs): the last one reported wins.
        latest = {c["name"]: c for c in checks}
        checks = list(latest.values())
        if not checks:
            return CiStatus("none", [])
        states = {c["state"] for c in checks}
        if "failure" in states:
            return CiStatus("failure", checks)
        if "pending" in states:
            return CiStatus("pending", checks)
        return CiStatus("success", checks)


def _norm_state(raw: Any) -> str:
    s = str(raw or "").lower()
    if s in {"success", "succeeded", "successful", "passed", "neutral", "skipped"}:
        return "success"
    if s in {
        "failure",
        "failed",
        "error",
        "cancelled",
        "canceled",
        "timed_out",
        "action_required",
        "stopped",
        "notapplicable_failure",
    }:
        return "failure"
    return "pending"  # queued, in_progress, running, pending, created, manual, notset, ...


def _gh_pr(d: dict[str, Any]) -> PullRequest:
    state = "merged" if d.get("merged_at") else str(d.get("state", "open"))
    return PullRequest(str(d["number"]), str(d["html_url"]), state, str(d["head"]["ref"]))


def _gl_pr(d: dict[str, Any]) -> PullRequest:
    state = {"opened": "open", "merged": "merged", "closed": "closed"}.get(str(d.get("state")), "open")
    return PullRequest(str(d["iid"]), str(d["web_url"]), state, str(d["source_branch"]))


def _bb_pr(d: dict[str, Any]) -> PullRequest:
    state = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed", "SUPERSEDED": "closed"}.get(
        str(d.get("state")), "open"
    )
    return PullRequest(
        str(d["id"]), str(d["links"]["html"]["href"]), state, str(d["source"]["branch"]["name"])
    )


def _ado_pr(d: dict[str, Any], ref: RepoRef) -> PullRequest:
    state = {"active": "open", "completed": "merged", "abandoned": "closed"}.get(str(d.get("status")), "open")
    url = f"{ref.url}/pullrequest/{d['pullRequestId']}"
    return PullRequest(
        str(d["pullRequestId"]), url, state, str(d.get("sourceRefName", "")).removeprefix("refs/heads/")
    )
