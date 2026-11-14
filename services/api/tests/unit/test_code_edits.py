"""Whole-file edits become diffs that git applies exactly; QA suites must name their criteria."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.execution.edits import FileEdit, edits_to_diff
from agentic_sdlc.execution.workspace import review_patch
from agentic_sdlc.skills_impl.delivery import build_code_context, suite_policy


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(content.encode())
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", "--allow-empty"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _apply(repo: Path, diff: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "apply", "--index", "-"], cwd=repo, input=diff.encode(), capture_output=True
    )


CASES = [
    ("a\nb\nc\n", "a\nB\nc\n"),
    ("a\nb", "a\nb\nc"),  # old has no final newline
    ("a\nb\n", "a\nb"),  # new has no final newline
    ("x", "y"),
    ("a\r\nb\r\n", "a\r\nc\r\n"),  # CRLF preserved
    ("a\fb\n", "a\fc\n"),  # form feed is not a line break for git
]


@pytest.mark.parametrize(("old", "new"), CASES)
def test_modify_create_delete_apply_exactly(tmp_path: Path, old: str, new: str) -> None:
    repo = _repo(tmp_path, {"f.txt": old, "gone.txt": "bye\n"})
    diff = edits_to_diff(
        [
            FileEdit("f.txt", "modify", new),
            FileEdit("pkg/new.txt", "create", "fresh\n"),
            FileEdit("gone.txt", "delete"),
        ],
        {"f.txt": old, "gone.txt": "bye\n"},
    )
    proc = _apply(repo, diff)
    assert proc.returncode == 0, proc.stderr.decode() + diff
    assert (repo / "f.txt").read_bytes() == new.encode()
    assert (repo / "pkg/new.txt").read_text() == "fresh\n"
    assert not (repo / "gone.txt").exists()


def test_randomized_edits_round_trip_through_git(tmp_path: Path) -> None:
    rng = random.Random(7)
    for i in range(60):
        old = "".join(rng.choice(["a\n", "b\n", "c\n", ""]) for _ in range(rng.randint(0, 15)))
        old += rng.choice(["", "tail"])
        new = "".join(rng.choice(["a\n", "x\n", "c\n", ""]) for _ in range(rng.randint(0, 15)))
        new += rng.choice(["", "end"])
        repo = _repo(tmp_path / str(i), {"f.txt": old})
        diff = edits_to_diff([FileEdit("f.txt", "modify", new)], {"f.txt": old})
        if old == new:
            assert diff == ""
            continue
        assert _apply(repo, diff).returncode == 0, (old, new, diff)
        assert (repo / "f.txt").read_bytes() == new.encode()


def test_inconsistent_edits_are_rejected() -> None:
    with pytest.raises(ValidationFailed, match="already exists"):
        edits_to_diff([FileEdit("a.py", "create", "x")], {"a.py": "y"})
    with pytest.raises(ValidationFailed, match="does not exist"):
        edits_to_diff([FileEdit("a.py", "modify", "x")], {})
    with pytest.raises(ValidationFailed, match="twice"):
        edits_to_diff([FileEdit("a.py", "modify", "x"), FileEdit("a.py", "modify", "z")], {"a.py": "y"})
    with pytest.raises(ValidationFailed):
        edits_to_diff([FileEdit("../escape.py", "create", "x")], {})
    with pytest.raises(ValidationFailed):
        edits_to_diff([FileEdit(".git/config", "create", "x")], {})


SKIPPED = "import pytest\n\n@pytest.mark.skip\ndef test_a():\n    pass\n"


def test_generated_diffs_go_through_the_same_policy_review() -> None:
    diff = edits_to_diff(
        [
            FileEdit(
                "tests/test_a.py", "modify", "import pytest\n\n@pytest.mark.skip\ndef test_a():\n    pass\n"
            )
        ],
        {"tests/test_a.py": "def test_a():\n    assert True\n"},
    )
    review = review_patch(diff, allowed_files={"src/a.py"})
    assert not review.ok and any("skip" in v for v in review.violations)


def test_context_keeps_whole_files_only_and_adds_related_tests() -> None:
    files = {
        "pkg/service.py": "x" * 100,
        "pkg/huge.py": "y" * 50_000,
        "tests/test_service.py": "def test_s(): ...\n",
        "tests/test_unrelated.py": "def test_u(): ...\n",
    }
    current, refs, warnings = build_code_context(
        files.get, sorted(files), ["pkg/service.py", "pkg/huge.py", "pkg/new_module.py"]
    )
    assert set(current) == {"pkg/service.py", "pkg/new_module.py", "tests/test_service.py"}
    assert current["pkg/new_module.py"] is None  # to be created
    assert refs == ["tests/test_service.py"]
    assert warnings == ["pkg/huge.py is too large to rewrite safely (50000 characters); not editable"]


def _suite(content: str) -> dict[str, Any]:
    return {
        "files": [{"path": ".asw/acceptance/test_acceptance.py", "content": content}],
        "coverage": [],
        "notes": [],
    }


INPUTS = {"criteria": [{"key": "FR-001-AC1", "text": "t"}, {"key": "FR-001-AC2", "text": "u"}]}


def test_suite_policy_accepts_tests_that_name_their_criteria() -> None:
    src = (
        "import pytest\n\n"
        "@pytest.mark.criterion('FR-001-AC1')\ndef test_one():\n    assert 1\n\n"
        '@pytest.mark.parametrize("n", [1, 2])\n@pytest.mark.criterion("FR-001-AC2")\n'
        "def test_two(n):\n    assert n\n"
    )
    assert suite_policy(INPUTS, _suite(src), None) == []  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("src", "problem"),
    [
        ("def test_x():\n    assert 1\n", "does not name the criterion"),
        (
            "import pytest\n@pytest.mark.criterion('NFR-9-AC1')\ndef test_x():\n    pass\n",
            "outside the scope",
        ),
        (
            "import pytest\n@pytest.mark.skip\n@pytest.mark.criterion('FR-001-AC1')\n"
            "def test_x():\n    pass\n",
            "skipped",
        ),
        (
            "import subprocess, pytest\n@pytest.mark.criterion('FR-001-AC1')\ndef test_x():\n"
            "    subprocess.run(['ls'])\n",
            "starts processes",
        ),
        ("def test_x(:\n", "syntax error"),
    ],
)
def test_suite_policy_rejects_unsafe_or_untraceable_tests(src: str, problem: str) -> None:
    problems = suite_policy(INPUTS, _suite(src), None)  # type: ignore[arg-type]
    assert any(problem in p for p in problems), problems
