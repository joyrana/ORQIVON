"""Run a repository's tests (and optionally an independent acceptance suite) in the sandbox.

The commit is exported with `git archive` (no `.git`), extracted with path checks, given to the
sandbox, and deleted afterwards. Evidence comes from the asw_evidence pytest plugin and is
cross-checked against pytest's exit code. Changes to tracked files during the run are detected by
hashing, not by running git in the sandbox-written tree.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_sdlc.core.errors import DomainError
from agentic_sdlc.core.gitenv import git_env
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.execution.repo_reader import safe_relpath
from agentic_sdlc.execution.sandbox import SandboxResult, get_sandbox
from agentic_sdlc.execution.test_profiles import PARSERS, TestProfile, _read_capped, resolve_profile
from agentic_sdlc.execution.workspace import GIT_SAFE, new_dir

PLUGIN = Path(__file__).with_name("pytest_evidence.py")
EXIT = {0: "passed", 1: "failed", 2: "interrupted", 3: "internal_error", 4: "usage_error", 5: "no_tests"}


@dataclass
class SuiteResult:
    status: str  # passed | failed | error
    totals: dict[str, int]
    cases: list[dict[str, Any]]
    log: str
    command: list[str]
    sandbox: dict[str, Any]
    commit: str
    tampered_files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    profile: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def export_tree(origin: Path, commit: str, dest: Path) -> list[str]:
    """Extract `commit` into dest without `.git`. Rejects links and paths escaping dest."""
    archive = subprocess.run(
        ["git", *GIT_SAFE, "archive", "--format=tar", commit],
        cwd=origin,
        capture_output=True,
        check=True,
        timeout=300,
        env=git_env(),
    ).stdout
    names = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not target.is_relative_to(dest.resolve()) or m.issym() or m.islnk() or m.isdev():
                raise ValueError(f"unsafe archive member {m.name}")
            if m.isfile():
                names.append(m.name)
        tar.extractall(dest, filter="data")
    return names


def _hash_files(root: Path, names: list[str]) -> dict[str, str]:
    out = {}
    for n in names:
        p = root / n
        out[n] = (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else "<missing>"
        )
    return out


def _placeholders(settings: Any, tree: Path) -> dict[str, str]:
    caches = settings.sandbox_dependency_caches
    return {
        "python": settings.sandbox_python,
        "work": str(tree),
        "m2": caches.get("m2", str(tree / ".tmp" / "m2")),
        "gradle_home": caches.get("gradle", str(tree / ".tmp" / "gradle")),
        "nuget": caches.get("nuget", str(tree / ".tmp" / "nuget")),
        "npm": caches.get("npm", str(tree / ".tmp" / "npm")),
    }


def _fill(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def _place_acceptance(tree: Path, files: dict[str, bytes], profile: TestProfile, tracked: list[str]) -> None:
    """Write QA acceptance files into the tree at their declared paths (never over repository files)."""
    for rel, data in files.items():
        safe_relpath(rel)
        if not re.fullmatch(profile.acceptance_path, rel):
            raise ValueError(f"acceptance file {rel!r} does not match the {profile.name} layout")
        if rel in tracked or (tree / rel).exists():
            raise ValueError(f"acceptance file {rel!r} would overwrite a repository file")
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _error(commit: str, profile: str, problem: str, command: list[str] | None = None) -> SuiteResult:
    return SuiteResult(
        status="error",
        totals={"passed": 0, "failed": 0, "skipped": 0, "error": 0, "total": 0},
        cases=[],
        log="",
        command=command or [],
        sandbox={"exit_meaning": "not run"},
        commit=commit,
        problems=[problem],
        profile=profile,
    )


def run_suite(
    origin: Path,
    commit: str,
    *,
    acceptance_dir: Path | None = None,
    acceptance_files: dict[str, bytes] | None = None,
    test_config: dict[str, Any] | None = None,
    extra_targets: list[str] | None = None,
    timeout_s: int | None = None,
) -> SuiteResult:
    """Run the repository's tests for `commit` in the sandbox and read the runner's own report.

    `acceptance_dir` is a shipped pytest acceptance suite (sample repositories); `acceptance_files`
    are QA-written acceptance tests placed at their profile-defined paths. The profile comes from the
    repository's `test_config`, or is detected from its files.
    """
    settings = get_settings()
    work = new_dir("tree")
    tree = work / "tree"
    tree.mkdir()
    try:
        tracked = export_tree(origin, commit, tree)

        def read(rel: str) -> str | None:
            p = tree / rel
            return p.read_text(errors="replace") if p.is_file() and p.stat().st_size < 1_000_000 else None

        try:
            profile = resolve_profile(test_config, tracked, read)
        except ValueError as exc:
            return _error(commit, "invalid", f"invalid test configuration: {exc}")
        if profile is None:
            return _error(
                commit, "none", "no test runner recognised for this repository; configure one for it"
            )
        asw = tree / ".asw"
        asw.mkdir()
        targets: list[str] = []
        if profile.report_format == "pytest_evidence":
            shutil.copy(PLUGIN, asw / "asw_evidence.py")
        if acceptance_dir is not None:
            if profile.name != "pytest":
                return _error(commit, profile.name, "a shipped acceptance directory needs the pytest profile")
            shutil.copytree(
                acceptance_dir, asw / "acceptance", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        if acceptance_files:
            try:
                _place_acceptance(tree, acceptance_files, profile, tracked)
            except (ValueError, DomainError) as exc:
                return _error(commit, profile.name, f"acceptance suite rejected: {exc}")
        if profile.name == "pytest" and (asw / "acceptance").is_dir():
            targets.append(".asw/acceptance")
            if (tree / "tests").is_dir():
                targets.append("tests")
        targets += extra_targets or []
        values = _placeholders(settings, tree)
        env = {k: _fill(v, values) for k, v in profile.env.items()}
        env.update({"PYTHONPATH": ".asw:.", "ASW_EVIDENCE_FILE": ".asw/evidence.jsonl"})
        timeout = timeout_s or profile.default_timeout_s
        sandbox = get_sandbox()
        setup_log = ""
        cfg = test_config or {}
        for step in cfg.get("setup", []):
            argv = [_fill(a, values) for a in step]
            r = sandbox.run(
                argv, tree, timeout_s=timeout, env=env, address_space_bytes=profile.address_space_bytes
            )
            setup_log += f"$ {' '.join(argv)}\n{r.stdout[-4000:]}{r.stderr[-4000:]}\n"
            if r.exit_code != 0 or r.timed_out:
                failed_setup = _error(
                    commit, profile.name, f"setup step failed: {' '.join(argv)[:200]}", argv
                )
                failed_setup.log = setup_log[-20000:]
                return failed_setup
        command = [_fill(a, values) for a in profile.command] + targets
        argv = command
        if profile.stdout_report:
            # Capture the report to a file: console output is truncated and redacted for logs.
            argv = ["sh", "-c", f'exec "$@" > {profile.stdout_report}', "sh", *command]
        before = _hash_files(tree, tracked)
        result: SandboxResult = sandbox.run(
            argv, tree, timeout_s=timeout, env=env, address_space_bytes=profile.address_space_bytes
        )
        cases: list[dict[str, Any]] = []
        problems: list[str] = []
        reports = sorted({p for g in profile.report_globs for p in tree.glob(g) if p.is_file()})
        if not reports:
            problems.append(
                f"the {profile.name} runner produced no report ({', '.join(profile.report_globs)})"
            )
        for report in reports:
            try:
                cases += [c.as_dict() for c in PARSERS[profile.report_format](_read_capped(report))]
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(f"unreadable report {report.relative_to(tree)}: {str(exc)[:200]}")
        after = _hash_files(tree, tracked)
        tampered = sorted(n for n in tracked if before[n] != after[n])
        totals = {
            k: sum(1 for c in cases if c["outcome"] == k) for k in ("passed", "failed", "skipped", "error")
        }
        totals["total"] = len(cases)
        failing = totals["failed"] + totals["error"]
        if profile.name == "pytest":
            exit_meaning = EXIT.get(result.exit_code, f"exit {result.exit_code}")
            if result.exit_code not in (0, 1) and not result.timed_out:
                problems.append(f"pytest did not complete normally ({exit_meaning})")
        else:
            exit_meaning = "ok" if result.exit_code == 0 else f"exit {result.exit_code}"
            if result.exit_code != 0 and not failing and not result.timed_out:
                problems.append(f"the runner failed ({exit_meaning}) without reporting a failing test")
        if result.timed_out:
            problems.append("test run timed out")
        if result.exit_code == 0 and failing:
            problems.append("the report shows failures but the runner exited 0")
        if result.exit_code != 0 and not failing and profile.name == "pytest" and result.exit_code == 1:
            problems.append("pytest reported failures that the evidence does not show")
        if tampered:
            problems.append("tests modified tracked files")
        if problems:
            status = "error"
        elif result.exit_code == 0 and totals["total"] > 0:
            status = "passed"
        else:
            status = "failed"
        log = (
            setup_log
            + result.stdout
            + ("\n--- stderr ---\n" + result.stderr if result.stderr.strip() else "")
        )
        return SuiteResult(
            status=status,
            totals=totals,
            cases=cases,
            log=log[-20000:],
            command=command,
            sandbox={**result.summary(), "exit_meaning": exit_meaning},
            commit=commit,
            tampered_files=tampered,
            problems=problems,
            profile=profile.name,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
