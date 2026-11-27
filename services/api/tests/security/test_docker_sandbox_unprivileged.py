"""The Docker backend with a worker that is not root (macOS with Docker Desktop, rootless Linux).

A non-root worker cannot chown the workspace to the sandbox uid; it used to fail every delivery run
at the implementation step with "Operation not permitted". It now runs the container as the worker's
own unprivileged uid and keeps every other hardening flag.
"""

import os
from pathlib import Path

import pytest

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.execution.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="covers the non-root worker")


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "docker.log"
    exe = tmp_path / "bin" / "docker"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\nif [ "$1" = run ]; then echo ran; fi\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{exe.parent}:{os.environ['PATH']}")
    return log


def test_non_root_worker_runs_the_container_as_itself(tmp_path: Path, fake_docker: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "mod.py").write_text("x = 1\n")
    s = get_settings().model_copy(update={"sandbox_uid": 64000})

    r = DockerSandbox(s).run(["python", "-c", "print(1)"], ws)

    assert r.exit_code == 0 and r.stdout.strip() == "ran"
    args = fake_docker.read_text().split()
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    for flag in ["--read-only", "--cap-drop", "ALL", "no-new-privileges", "--pids-limit", "--memory"]:
        assert flag in args, flag
    assert args[args.index("--network") + 1] == "none"
    # Nothing was handed to another uid; the workspace stays the worker's own.
    assert (ws / "pkg" / "mod.py").stat().st_uid == os.getuid()
    assert (ws / ".tmp").is_dir()
