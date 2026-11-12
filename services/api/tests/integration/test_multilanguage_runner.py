"""Real test runs in the sandbox for Go and Node.js repositories (toolchains present on the worker).

Maven, Gradle, .NET, Jest and Vitest need dependencies that cannot be fetched without network; their
report parsing is covered by unit tests with recorded reports, and their execution by an operator
with dependency caches configured (docs/operations/testing.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.execution.test_runner import run_suite

needs_sandbox = pytest.mark.skipif(
    os.geteuid() != 0 or not shutil.which("unshare") or not Path(get_settings().sandbox_python).exists(),
    reason="needs the namespace sandbox",
)
pytestmark = needs_sandbox


def _origin(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "origin"
    repo.mkdir()
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return repo, sha.stdout.strip()


GO_FILES = {
    "go.mod": "module example.com/calc\n\ngo 1.22\n",
    "calc/calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n",
    "calc/calc_test.go": (
        'package calc\n\nimport "testing"\n\n'
        'func TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("bad")\n\t}\n}\n'
    ),
}
GO_ACCEPTANCE = (
    'package calc\n\nimport "testing"\n\n'
    "func TestFR_001_AC1_AddsNegatives(t *testing.T) {\n"
    '\tif got := Add(-1, -2); got != -4 {\n\t\tt.Fatalf("Add(-1,-2) = %d, want -4", got)\n\t}\n}\n'
    'func TestFR_001_AC2_Zero(t *testing.T) {\n\tif Add(0, 0) != 0 {\n\t\tt.Fatal("zero")\n\t}\n}\n'
)


@pytest.mark.skipif(not shutil.which("go"), reason="go toolchain not installed")
def test_go_repository_with_qa_acceptance_file(tmp_path: Path) -> None:
    origin, sha = _origin(tmp_path, GO_FILES)
    dev = run_suite(origin, sha)
    assert dev.profile == "go" and dev.status == "passed", (dev.problems, dev.log[-2000:])
    assert [c["nodeid"] for c in dev.cases] == ["example.com/calc/calc::TestAdd"]

    acc = run_suite(origin, sha, acceptance_files={"calc/asw_acceptance_add_test.go": GO_ACCEPTANCE.encode()})
    assert acc.status == "failed" and acc.problems == []
    by_name = {c["nodeid"].split("::")[1]: c for c in acc.cases}
    assert by_name["TestFR_001_AC1_AddsNegatives"]["outcome"] == "failed"
    assert by_name["TestFR_001_AC1_AddsNegatives"]["criteria"] == ["FR-001-AC1"]
    assert "want -4" in by_name["TestFR_001_AC1_AddsNegatives"]["message"]
    assert by_name["TestFR_001_AC2_Zero"]["outcome"] == "passed"
    assert acc.sandbox["backend"] == "namespace" and acc.sandbox["timed_out"] is False


@pytest.mark.skipif(not shutil.which("go"), reason="go toolchain not installed")
def test_go_build_failure_is_a_failed_run_not_a_crash(tmp_path: Path) -> None:
    files = {**GO_FILES, "calc/calc.go": "package calc\n\nfunc Add(a, b int) int { return a + }\n"}
    origin, sha = _origin(tmp_path, files)
    r = run_suite(origin, sha)
    assert r.status == "failed" and r.problems == []
    assert any(c["outcome"] == "error" and c["nodeid"].endswith("<build>") for c in r.cases)


@pytest.mark.skipif(not shutil.which("go"), reason="go toolchain not installed")
def test_go_tests_cannot_reach_the_network(tmp_path: Path) -> None:
    probe = (
        'package calc\n\nimport (\n\t"net"\n\t"testing"\n\t"time"\n)\n\n'
        "func TestNoNetwork(t *testing.T) {\n"
        '\tif c, err := net.DialTimeout("tcp", "1.1.1.1:443", 2*time.Second); err == nil {\n'
        '\t\tc.Close()\n\t\tt.Fatal("network reachable from the sandbox")\n\t}\n}\n'
    )
    origin, sha = _origin(tmp_path, {**GO_FILES, "calc/net_test.go": probe})
    r = run_suite(origin, sha)
    assert r.status == "passed", (r.problems, [c for c in r.cases if c["outcome"] != "passed"])


NODE_FILES = {
    "package.json": '{"name": "calc", "version": "1.0.0", "type": "module"}\n',
    "calc.js": "export const add = (a, b) => a + b;\n",
    "test/calc.test.js": (
        'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
        'import { add } from "../calc.js";\n\ntest("adds", () => assert.equal(add(1, 2), 3));\n'
    ),
}


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_node_repository_with_qa_acceptance_file(tmp_path: Path) -> None:
    origin, sha = _origin(tmp_path, NODE_FILES)
    dev = run_suite(origin, sha)
    assert dev.profile == "node-test" and dev.status == "passed", (dev.problems, dev.log[-2000:])
    acceptance = (
        'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
        'import { add } from "../calc.js";\n\n'
        'test("[FR-002-AC1] adds strings by concatenation", () => assert.equal(add("a", "b"), "ab"));\n'
        'test("[FR-002-AC2] adds floats", () => assert.equal(add(0.5, 0.25), 0.7));\n'
    )
    acc = run_suite(origin, sha, acceptance_files={"asw_acceptance/calc.test.js": acceptance.encode()})
    assert acc.status == "failed" and acc.problems == [], acc.problems
    by_criteria = {tuple(c["criteria"]): c for c in acc.cases if c["criteria"]}
    assert by_criteria[("FR-002-AC1",)]["outcome"] == "passed"
    assert by_criteria[("FR-002-AC2",)]["outcome"] == "failed"
    assert acc.totals["total"] == 3


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_tests_that_modify_tracked_files_are_flagged(tmp_path: Path) -> None:
    files = {
        **NODE_FILES,
        "test/calc.test.js": (
            'import { test } from "node:test";\nimport { writeFileSync } from "node:fs";\n'
            'test("tampers", () => writeFileSync("calc.js", "export const add = () => 3;\\n"));\n'
        ),
    }
    origin, sha = _origin(tmp_path, files)
    r = run_suite(origin, sha)
    assert r.status == "error" and r.tampered_files == ["calc.js"]


def test_acceptance_files_cannot_overwrite_or_escape(tmp_path: Path) -> None:
    origin, sha = _origin(tmp_path, NODE_FILES)
    over = run_suite(origin, sha, acceptance_files={"asw_acceptance/../calc.js": b"x"})
    assert over.status == "error"
    wrong_place = run_suite(origin, sha, acceptance_files={"src/evil.test.js": b"x"})
    assert wrong_place.status == "error" and "layout" in wrong_place.problems[0]


def test_unrecognised_repository_needs_configuration(tmp_path: Path) -> None:
    origin, sha = _origin(tmp_path, {"README.md": "hello\n"})
    r = run_suite(origin, sha)
    assert r.status == "error" and "configure" in r.problems[0]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_configured_command_setup_and_missing_report(tmp_path: Path) -> None:
    origin, sha = _origin(tmp_path, NODE_FILES)
    # A custom command that writes no report is an error, not a pass.
    r = run_suite(
        origin,
        sha,
        test_config={
            "command": ["node", "-e", "0"],
            "report_format": "junit_xml",
            "report_globs": ["out.xml"],
        },
    )
    assert r.status == "error" and "produced no report" in r.problems[0]
    # A failing setup step stops the run with its output.
    r = run_suite(origin, sha, test_config={"setup": [["node", "-e", "process.exit(4)"]]})
    assert r.status == "error" and r.problems[0].startswith("setup step failed")
    # Explicit profile wins over detection.
    r = run_suite(origin, sha, test_config={"profile": "node-test", "timeout_s": 60})
    assert r.status == "passed" and r.profile == "node-test"


def test_repository_test_profile_api(client, project) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import H

    pid = project["id"]
    repo = client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"name": "svc", "kind": "fixture", "location": "sample-task-service"},
        headers=H("owner"),
    ).json()
    url = f"/api/v1/projects/{pid}/repositories/{repo['id']}"
    detected = client.get(f"{url}/test-profile", headers=H("auditor")).json()
    assert detected["source"] == "detected" and detected["profile"]["name"] == "pytest"
    assert len(detected["commit"]) == 40

    bad = client.put(f"{url}/test-config", json={"test_config": {"profile": "cobol"}}, headers=H("owner"))
    assert bad.status_code == 422
    denied = client.put(f"{url}/test-config", json={"test_config": {}}, headers=H("auditor"))
    assert denied.status_code == 403
    ok = client.put(
        f"{url}/test-config",
        json={"test_config": {"profile": "pytest", "timeout_s": 300}},
        headers=H("owner"),
    )
    assert ok.status_code == 200 and ok.json()["test_config"] == {"profile": "pytest", "timeout_s": 300}
    configured = client.get(f"{url}/test-profile", headers=H("engineer")).json()
    assert configured["source"] == "configured"
    audit = client.get(
        f"/api/v1/projects/{pid}/audit?action=repository.test_config.updated", headers=H("auditor")
    )
    assert audit.json()["items"][0]["details"]["test_config"]["timeout_s"] == 300
