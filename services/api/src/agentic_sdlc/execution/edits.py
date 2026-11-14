"""Turn whole-file edits into a git-format unified diff.

Models are unreliable at writing unified diffs by hand (wrong hunk counts, stale context lines), so
the LLM coding agent returns complete new file contents and the platform computes the diff. The diff
then goes through exactly the same policy review and `git apply --check` as any other patch.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.execution.repo_reader import safe_relpath

NO_EOL = "\\ No newline at end of file"


@dataclass(frozen=True)
class FileEdit:
    path: str
    action: Literal["create", "modify", "delete"]
    content: str | None = None


def _lines(text: str) -> list[str]:
    # Split on "\n" only: str.splitlines also breaks on \r, \f, \x1c... which git does not.
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])  # last line without a newline
    return lines


def _file_diff(path: str, old: str | None, new: str | None) -> str:
    a = _lines(old) if old is not None else []
    b = _lines(new) if new is not None else []
    if a == b:
        return ""
    header = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        header.append("new file mode 100644\n")
    elif new is None:
        header.append("deleted file mode 100644\n")
    from_name = f"a/{path}" if old is not None else "/dev/null"
    to_name = f"b/{path}" if new is not None else "/dev/null"
    body = difflib.unified_diff(a, b, fromfile=from_name, tofile=to_name, n=3)
    # A final line without a newline comes out of difflib unterminated; git marks it explicitly.
    out = [line if line.endswith("\n") else line + "\n" + NO_EOL + "\n" for line in body]
    return "".join(header + out)


def edits_to_diff(edits: list[FileEdit], current: dict[str, str | None]) -> str:
    """Build a diff for `edits` against `current` (path → content, None if the file does not exist).

    Rejects unsafe paths, duplicate edits, creating a file that exists, and modifying or deleting one
    that does not. Edits that change nothing are dropped.
    """
    seen: set[str] = set()
    parts: list[str] = []
    for e in edits:
        safe_relpath(e.path)
        if e.path in seen:
            raise ValidationFailed(f"file edited twice: {e.path}")
        seen.add(e.path)
        old = current.get(e.path)
        if e.action == "create":
            if old is not None:
                raise ValidationFailed(f"cannot create {e.path}: it already exists")
            if e.content is None:
                raise ValidationFailed(f"create {e.path} needs content")
            parts.append(_file_diff(e.path, None, e.content))
        elif e.action == "modify":
            if old is None:
                raise ValidationFailed(f"cannot modify {e.path}: it does not exist")
            if e.content is None:
                raise ValidationFailed(f"modify {e.path} needs content")
            parts.append(_file_diff(e.path, old, e.content))
        else:
            if old is None:
                raise ValidationFailed(f"cannot delete {e.path}: it does not exist")
            parts.append(_file_diff(e.path, old, None))
    return "".join(p for p in parts if p)
