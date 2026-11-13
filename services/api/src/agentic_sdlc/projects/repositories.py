"""Registered repositories: the local origin agents work from, mirror sync and branch publishing.

Agents never touch a remote. Every repository has a platform-managed *local origin*:

- `fixture` repositories: a git repository materialized from a shipped sample;
- `git` repositories: a bare mirror under `DATA_DIR/repositories/<id>.git`, refreshed from the
  remote with the repository's connection credential.

Agent workspaces clone the local origin and push their branches back to it. Publishing a verified
branch to the remote and opening a pull request is a separate, audited platform operation.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentic_sdlc.core.db import utcnow
from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.core.gitenv import git_env
from agentic_sdlc.core.logging import redact
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.execution import repo_reader
from agentic_sdlc.integrations.git_hosting import (
    GitConnection,
    GitHostingError,
    RepoRef,
    credential,
    get_connection,
    git_auth_env,
    parse_clone_url,
)
from agentic_sdlc.orm import ProjectRepository

NETWORK_TIMEOUT_S = 600


def mirror_path(repo: ProjectRepository) -> Path:
    return get_settings().data_dir / "repositories" / f"{repo.id}.git"


def local_origin(repo: ProjectRepository) -> Path:
    """The platform-managed git repository agents clone from."""
    if repo.kind == "fixture":
        return Path(repo.location)
    path = mirror_path(repo)
    if not (path / "HEAD").is_file() or not (repo.sync_status or {}).get("head"):
        raise ValidationFailed("this repository has not been synchronised yet")
    return path


def remote(repo: ProjectRepository) -> tuple[GitConnection, RepoRef]:
    if repo.kind != "git":
        raise ValidationFailed("this repository has no remote")
    conn = get_connection(repo.connection)
    return conn, parse_clone_url(repo.location, conn)


def _protocol_args(ref: RepoRef) -> list[str]:
    scheme = ref.url.split("://", 1)[0]
    return [
        "-c",
        "protocol.allow=never",
        "-c",
        f"protocol.{scheme}.allow=always",
        "-c",
        "http.followRedirects=false",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
    ]


def _git_network(args: list[str], cwd: Path, ref: RepoRef, conn: GitConnection) -> str:
    cred = credential(conn, ref)
    env = git_env(**git_auth_env(ref, cred), GIT_ASKPASS="/bin/false", SSH_ASKPASS="/bin/false")
    try:
        proc = subprocess.run(
            ["git", *_protocol_args(ref), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=NETWORK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHostingError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        secret = cred.secret if cred else None
        msg = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        text = redact(" ".join(msg))
        if secret:
            text = text.replace(secret, "[REDACTED]")
        raise GitHostingError(f"git {args[0]} failed: {text[:400]}")
    return proc.stdout


def _git_local(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, timeout=120, check=True
    )
    return proc.stdout.strip()


def sync(repo: ProjectRepository) -> dict[str, Any]:
    """Fetch the remote's branches and tags into the mirror; record the outcome on the repository.

    Branches are fetched without pruning, so platform branches that were never published survive.
    """
    conn, ref = remote(repo)
    if not conn.allows("read"):
        raise ValidationFailed(f"connection '{conn.name}' is not allowed to read repositories")
    path = mirror_path(repo)
    status: dict[str, Any] = {"last_attempt_at": utcnow().isoformat()}
    try:
        if not (path / "HEAD").is_file():
            path.mkdir(parents=True, exist_ok=True)
            _git_local(["init", "--bare", "-q", "-b", repo.default_branch], path)
        _git_network(
            [
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                ref.url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            path,
            ref,
            conn,
        )
        try:
            head = repo_reader.head_commit(path, repo.default_branch)
        except Exception as exc:
            raise GitHostingError(f"the remote has no branch '{repo.default_branch}'") from exc
        _git_local(["symbolic-ref", "HEAD", f"refs/heads/{repo.default_branch}"], path)
        status.update(state="ok", head=head, last_synced_at=utcnow().isoformat(), error=None)
    except GitHostingError as exc:
        status.update(state="error", error=str(exc))
    repo.sync_status = {**(repo.sync_status or {}), **status}
    return repo.sync_status


def publish_branch(repo: ProjectRepository, branch: str, sha: str) -> None:
    """Push one commit from the mirror to `refs/heads/<branch>` on the remote. Never forced."""
    conn, ref = remote(repo)
    if not conn.allows("push"):
        raise GitHostingError(f"connection '{conn.name}' is not allowed to push branches")
    if not branch.startswith("asw/"):
        raise GitHostingError("the platform only publishes branches in the asw/ namespace")
    _git_network(
        ["push", "--quiet", "--no-verify", ref.url, f"{sha}:refs/heads/{branch}"],
        mirror_path(repo),
        ref,
        conn,
    )


def tree_reader(repo: ProjectRepository) -> tuple[str, list[str], Callable[[str], str | None]]:
    """(head commit, file paths, reader) for the repository's default branch."""
    origin = local_origin(repo)
    commit = repo_reader.head_commit(origin, repo.default_branch)
    files = [f["path"] for f in repo_reader.list_files(origin, commit)]

    def read(path: str) -> str | None:
        try:
            return repo_reader.read_file(origin, path, commit)
        except Exception:
            return None

    return commit, files, read
