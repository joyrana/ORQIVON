"""Coding agent capability registry and selection.

| kind      | when selectable                                                         |
|-----------|-------------------------------------------------------------------------|
| llm       | the router finds a model with code + structured_output capabilities     |
| scripted  | the repository is a shipped sample fixture with a coding scenario       |
| none      | otherwise — the task cannot be executed and is escalated to a human      |

`scripted` exists so the full lifecycle can be demonstrated and tested without inference hardware.
It replays fixture patches and is labeled as such everywhere; it is never selected for a real
repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.models.router import RoutingDecision
from agentic_sdlc.orm import ProjectRepository


def scenario_dir(repo: ProjectRepository) -> Path | None:
    if not repo.is_sample or not repo.fixture:
        return None
    path = get_settings().fixtures_dir / "coding" / repo.fixture
    return path if (path / "scenario.yaml").is_file() else None


def load_scenario(repo: ProjectRepository) -> dict[str, Any] | None:
    d = scenario_dir(repo)
    if d is None:
        return None
    data: dict[str, Any] = yaml.safe_load((d / "scenario.yaml").read_text())
    data["_dir"] = str(d)
    return data


def select_coding_agent(decision: RoutingDecision, repo: ProjectRepository | None) -> dict[str, Any]:
    if decision.model_id:
        return {"kind": "llm", "model_ref": decision.model_ref, "is_mock": decision.is_mock}
    if repo is not None and scenario_dir(repo) is not None:
        return {
            "kind": "scripted",
            "fixture": repo.fixture,
            "reason": "no code model available; sample repository has a scripted scenario",
        }
    return {
        "kind": "none",
        "reason": "no code model available and the repository has no scripted scenario",
    }


def acceptance_suite_dir(repo: ProjectRepository) -> Path | None:
    """QA-owned acceptance tests for a sample repository (samples/acceptance/<fixture>)."""
    if not repo.is_sample or not repo.fixture:
        return None
    path = get_settings().fixtures_dir / "acceptance" / repo.fixture
    return path if path.is_dir() else None


def generated_suite(run_outcome: dict[str, Any]) -> dict[str, Any] | None:
    suite = (run_outcome or {}).get("acceptance_suite")
    return suite if isinstance(suite, dict) and suite.get("files") else None


def load_generated_suite(suite: dict[str, Any]) -> dict[str, bytes]:
    """The stored (content-addressed) QA acceptance files, keyed by their path in the test tree.

    Suites recorded before acceptance files carried full paths stored bare pytest file names; those
    live under `.asw/acceptance/`.
    """
    from agentic_sdlc.core.storage import get_storage

    storage = get_storage()
    files: dict[str, bytes] = {}
    for name, key in suite["files"].items():
        path = name if "/" in name else f".asw/acceptance/{name}"
        files[path] = storage.get(key)
    return files
