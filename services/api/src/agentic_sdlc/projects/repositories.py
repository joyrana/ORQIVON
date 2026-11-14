"""Read access to a registered repository's default branch for platform features (not agents)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.execution import repo_reader
from agentic_sdlc.orm import ProjectRepository


def local_origin(repo: ProjectRepository) -> Path:
    """The platform-managed git repository agents clone from."""
    if repo.kind != "fixture":
        raise ValidationFailed("this repository has not been synchronised yet")
    return Path(repo.location)


def tree_reader(repo: ProjectRepository) -> tuple[str, list[str], Callable[[str], str | None]]:
    """(head commit, file paths, reader) for the repository's default branch."""
    origin = local_origin(repo)
    rev = repo.default_branch
    commit = repo_reader.head_commit(origin, rev)
    files = [f["path"] for f in repo_reader.list_files(origin, commit)]

    def read(path: str) -> str | None:
        try:
            return repo_reader.read_file(origin, path, commit)
        except Exception:
            return None

    return commit, files, read
