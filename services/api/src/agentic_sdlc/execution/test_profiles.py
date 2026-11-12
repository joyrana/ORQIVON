"""Language-aware test execution: how to run a repository's tests and read their results.

A *profile* says, for one ecosystem: how to recognise a repository that uses it, which command runs
the tests inside the sandbox, where the machine-readable report is written and how to parse it, where
QA acceptance tests are placed and how they name the criterion they verify, and which source
patterns mark test files, test functions and skipped tests (used by the patch policy).

Results always come from the runner's own report (JUnit XML, `go test -json`, Jest JSON, TRX, or the
pytest evidence plugin) and are cross-checked against the exit code by the test runner; nothing is
inferred from console text.

Criterion keys: pytest tests use `@pytest.mark.criterion("FR-001-AC1")`. Every other ecosystem puts
the key in the test name, e.g. `[FR-001-AC1] rejects long titles` (Jest, Vitest, node:test, JUnit
@DisplayName, xUnit DisplayName) or `TestFR_001_AC1_RejectsLongTitles` (Go, Java, C# method names).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_REPORT_BYTES = 20 * 1024 * 1024
# FR-001-AC1, NFR-12-AC3, BR-7-AC1; underscores accepted where names cannot contain dashes.
# A lowercase letter may precede the key so Go/Java names like TestFR_001_AC1 work; an uppercase
# letter or digit may not (XFR-001-AC1 is not FR-001-AC1).
CRITERION = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{0,9})[-_](\d{1,5})[-_]AC(\d{1,3})(?![0-9])")

ReportFormat = Literal["pytest_evidence", "junit_xml", "go_json", "jest_json", "trx"]


@dataclass(frozen=True)
class TestCase:
    __test__ = False  # not a pytest test class
    nodeid: str
    outcome: Literal["passed", "failed", "skipped", "error"]
    criteria: list[str]
    message: str = ""
    duration_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid[:300],
            "outcome": self.outcome,
            "criteria": self.criteria[:20],
            "message": self.message[:500],
            "duration_s": self.duration_s,
        }


@dataclass(frozen=True)
class TestProfile:
    __test__ = False  # not a pytest test class
    name: str
    language: str
    command: tuple[str, ...]
    report_format: ReportFormat
    report_globs: tuple[str, ...]
    # Acceptance tests written by QA: where they go and an example of naming a criterion.
    acceptance_path: str  # regex every acceptance file path must match
    acceptance_hint: str
    test_path: str  # regex: is this a test file?
    test_def: str  # regex (multiline): a test function/case definition
    skip_mark: str  # regex: a skipped/disabled/focused test
    env: dict[str, str] = field(default_factory=dict)
    address_space_bytes: int | None = None  # some runtimes reserve large virtual memory (JVM, .NET)
    needs_dependencies: bool = False  # cannot run offline without vendored/cached dependencies
    default_timeout_s: int | None = None
    stdout_report: str | None = None  # the runner writes its report to stdout; captured to this path

    def is_test_path(self, path: str) -> bool:
        return re.search(self.test_path, path) is not None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "language": self.language,
            "command": list(self.command),
            "report_format": self.report_format,
            "acceptance_path": self.acceptance_path,
            "acceptance_hint": self.acceptance_hint,
            "needs_dependencies": self.needs_dependencies,
        }


ASW = ".asw"  # platform-owned directory inside a test tree; never part of the repository

PROFILES: dict[str, TestProfile] = {
    p.name: p
    for p in (
        TestProfile(
            name="pytest",
            language="python",
            command=(
                "{python}",
                "-m",
                "pytest",
                "-p",
                "asw_evidence",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                ".",
            ),
            report_format="pytest_evidence",
            report_globs=(f"{ASW}/evidence.jsonl",),
            acceptance_path=r"^\.asw/acceptance/test_[a-z0-9_]{1,60}\.py$",
            acceptance_hint='@pytest.mark.criterion("FR-001-AC1") on each test function',
            test_path=r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$",
            test_def=r"^\s*(async\s+)?def\s+test_\w*\s*\(",
            skip_mark=r"@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.skip\(|\bunittest\.skip",
        ),
        TestProfile(
            name="go",
            language="go",
            command=("go", "test", "-json", "-count=1", "./..."),
            report_format="go_json",
            report_globs=(f"{ASW}/go-test.json",),
            stdout_report=f"{ASW}/go-test.json",
            acceptance_path=r"^([A-Za-z0-9_.-]+/)*asw_acceptance_[a-z0-9_]{1,60}_test\.go$",
            acceptance_hint="func TestFR_001_AC1_<Behaviour>(t *testing.T) in a file named "
            "asw_acceptance_<topic>_test.go inside the package under test",
            test_path=r"_test\.go$",
            test_def=r"^\s*func\s+Test\w*\s*\(\s*\w+\s+\*testing\.T\s*\)",
            skip_mark=r"\bt\.Skip(Now|f)?\(",
            env={
                "GOTOOLCHAIN": "local",
                "GOPROXY": "off",
                "GOFLAGS": "-mod=mod",
                "CGO_ENABLED": "0",
                "GOCACHE": "{work}/.tmp/go-build",
                "GOPATH": "{work}/.tmp/go",
            },
        ),
        TestProfile(
            name="node-test",
            language="javascript",
            command=(
                "node",
                "--test",
                "--test-reporter=junit",
                f"--test-reporter-destination={ASW}/junit.xml",
            ),
            report_format="junit_xml",
            report_globs=(f"{ASW}/junit.xml",),
            acceptance_path=r"^asw_acceptance/[a-z0-9_-]{1,60}\.test\.(js|mjs|cjs)$",
            acceptance_hint='test("[FR-001-AC1] <behaviour>", ...) using node:test and node:assert',
            test_path=r"(^|/)(test|tests|__tests__)/|\.(test|spec)\.[cm]?[jt]sx?$",
            test_def=r"^\s*(test|it)\s*\(",
            skip_mark=r"\b(test|it|describe)\.(skip|only|todo)\s*\(|\bx(it|describe)\s*\(",
        ),
        TestProfile(
            name="jest",
            language="javascript",
            command=(
                "node",
                "node_modules/jest/bin/jest.js",
                "--ci",
                "--json",
                f"--outputFile={ASW}/jest.json",
            ),
            report_format="jest_json",
            report_globs=(f"{ASW}/jest.json",),
            acceptance_path=r"^asw_acceptance/[a-z0-9_-]{1,60}\.test\.(js|jsx|ts|tsx|mjs)$",
            acceptance_hint='test("[FR-001-AC1] <behaviour>", ...)',
            test_path=r"(^|/)(test|tests|__tests__)/|\.(test|spec)\.[cm]?[jt]sx?$",
            test_def=r"^\s*(test|it)\s*\(",
            skip_mark=r"\b(test|it|describe)\.(skip|only|todo)\s*\(|\bx(it|describe|test)\s*\(",
            needs_dependencies=True,
        ),
        TestProfile(
            name="vitest",
            language="javascript",
            command=(
                "node",
                "node_modules/vitest/vitest.mjs",
                "run",
                "--reporter=junit",
                f"--outputFile={ASW}/junit.xml",
            ),
            report_format="junit_xml",
            report_globs=(f"{ASW}/junit.xml",),
            acceptance_path=r"^asw_acceptance/[a-z0-9_-]{1,60}\.test\.(js|jsx|ts|tsx|mjs)$",
            acceptance_hint='test("[FR-001-AC1] <behaviour>", ...) importing from "vitest"',
            test_path=r"(^|/)(test|tests|__tests__)/|\.(test|spec)\.[cm]?[jt]sx?$",
            test_def=r"^\s*(test|it)\s*\(",
            skip_mark=r"\b(test|it|describe)\.(skip|only|todo)\s*\(",
            needs_dependencies=True,
        ),
        TestProfile(
            name="maven",
            language="java",
            command=("mvn", "-o", "-B", "-q", "test", "-Dmaven.repo.local={m2}"),
            report_format="junit_xml",
            report_globs=("target/surefire-reports/TEST-*.xml",),
            acceptance_path=r"^src/test/java/asw/acceptance/[A-Z][A-Za-z0-9]{0,60}Test\.java$",
            acceptance_hint="package asw.acceptance; JUnit 5 methods named FR_001_AC1_<behaviour>() "
            'or annotated @DisplayName("[FR-001-AC1] ...")',
            test_path=r"(^|/)src/test/|(Test|Tests|IT)\.java$",
            test_def=r"@(Test|ParameterizedTest|RepeatedTest)\b",
            skip_mark=r"@(Disabled|Ignore)\b|\bAssumptions\.assume",
            address_space_bytes=8 * 1024**3,
            needs_dependencies=True,
            default_timeout_s=900,
        ),
        TestProfile(
            name="gradle",
            language="java",
            command=("gradle", "test", "--offline", "--no-daemon", "-q"),
            report_format="junit_xml",
            report_globs=("build/test-results/test/TEST-*.xml", "**/build/test-results/test/TEST-*.xml"),
            acceptance_path=r"^src/test/(java|kotlin)/asw/acceptance/[A-Z][A-Za-z0-9]{0,60}Test\.(java|kt)$",
            acceptance_hint="package asw.acceptance; JUnit 5 methods named FR_001_AC1_<behaviour>() "
            'or annotated @DisplayName("[FR-001-AC1] ...")',
            test_path=r"(^|/)src/test/|(Test|Tests|IT)\.(java|kt)$",
            test_def=r"@(Test|ParameterizedTest|RepeatedTest)\b",
            skip_mark=r"@(Disabled|Ignore)\b",
            env={"GRADLE_USER_HOME": "{gradle_home}"},
            address_space_bytes=8 * 1024**3,
            needs_dependencies=True,
            default_timeout_s=900,
        ),
        TestProfile(
            name="dotnet",
            language="csharp",
            command=(
                "dotnet",
                "test",
                "--no-restore",
                "--logger",
                "trx;LogFileName=asw.trx",
                "--results-directory",
                f"{ASW}/trx",
            ),
            report_format="trx",
            report_globs=(f"{ASW}/trx/*.trx",),
            acceptance_path=r"^([A-Za-z0-9_.-]+/)*AswAcceptance/[A-Z][A-Za-z0-9]{0,60}Tests\.cs$",
            acceptance_hint="methods named FR_001_AC1_<Behaviour>() in an AswAcceptance folder of the "
            "existing test project",
            test_path=r"(^|/)[^/]*Tests?(\.[A-Za-z]+)?/|Tests?\.cs$",
            test_def=r"\[(Fact|Theory|Test|TestMethod|TestCase)\b",
            skip_mark=r"\[(Fact|Theory)\s*\(\s*Skip\s*=|\[Ignore\b",
            env={"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "NUGET_PACKAGES": "{nuget}"},
            address_space_bytes=16 * 1024**3,
            needs_dependencies=True,
            default_timeout_s=900,
        ),
    )
}


class TestConfig(BaseModel):
    """Per-repository override of the detected profile (set by an owner or engineer).

    Commands run inside the sandbox like the tests themselves, so they need no extra trust; they are
    still validated so a typo cannot reach outside the test tree.
    """

    __test__ = False  # not a pytest test class
    model_config = ConfigDict(extra="forbid")

    profile: str | None = None  # a PROFILES name; default: detected from the repository
    command: list[str] | None = None
    report_format: ReportFormat | None = None
    report_globs: list[str] | None = None
    setup: list[list[str]] = Field(default_factory=list, max_length=5)  # run first, same sandbox
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: int | None = Field(default=None, ge=10, le=3600)
    acceptance_path: str | None = None

    @field_validator("profile")
    @classmethod
    def _known(cls, v: str | None) -> str | None:
        if v is not None and v not in PROFILES:
            raise ValueError(f"unknown profile '{v}'; one of {sorted(PROFILES)}")
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and (not v or any(not a or "\x00" in a for a in v) or len(v) > 50):
            raise ValueError("command must be a non-empty list of arguments")
        return v

    @field_validator("report_globs")
    @classmethod
    def _globs(cls, v: list[str] | None) -> list[str] | None:
        for g in v or []:
            if g.startswith("/") or ".." in Path(g).parts:
                raise ValueError(f"report path {g!r} must stay inside the test tree")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", k) or k in {
                "PATH",
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
            }:
                raise ValueError(f"environment variable {k!r} is not allowed")
        return v

    @field_validator("acceptance_path")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v

    @model_validator(mode="after")
    def _custom_needs_report(self) -> TestConfig:
        if self.command and not self.profile and not (self.report_format and self.report_globs):
            raise ValueError("a custom command needs a profile, or report_format and report_globs")
        return self


def resolve_profile(config: dict[str, Any] | None, files: list[str], read: Any = None) -> TestProfile | None:
    """The profile for a repository: its explicit configuration applied over detection."""
    cfg = TestConfig.model_validate(config or {})
    base = PROFILES[cfg.profile] if cfg.profile else detect_profile(files, read)
    if base is None:
        if not cfg.command:
            return None
        base = TestProfile(
            name="custom",
            language="unknown",
            command=tuple(cfg.command),
            report_format=cfg.report_format or "junit_xml",
            report_globs=tuple(cfg.report_globs or ()),
            acceptance_path=cfg.acceptance_path or r"^asw_acceptance/[A-Za-z0-9_.-]{1,80}$",
            acceptance_hint="put the criterion key, e.g. [FR-001-AC1], in each test's name",
            test_path=r"(^|/)(test|tests|spec)/|[._-](test|spec)\.",
            test_def=r"\btest\w*\s*\(",
            skip_mark=r"\bskip\b",
        )
    changes: dict[str, Any] = {}
    if cfg.command:
        changes["command"] = tuple(cfg.command)
        changes["stdout_report"] = None
    if cfg.report_format:
        changes["report_format"] = cfg.report_format
    if cfg.report_globs:
        changes["report_globs"] = tuple(cfg.report_globs)
    if cfg.env:
        changes["env"] = {**base.env, **cfg.env}
    if cfg.timeout_s:
        changes["default_timeout_s"] = cfg.timeout_s
    if cfg.acceptance_path:
        changes["acceptance_path"] = cfg.acceptance_path
    return replace(base, **changes) if changes else base


def detect_profile(files: list[str], read: Any = None) -> TestProfile | None:
    """Choose a profile from the repository's files (paths relative to the root).

    `read(path) -> str | None` is used to look inside package.json. Returns None when nothing matches;
    the repository then needs an explicit test configuration.
    """
    names = set(files)
    if "go.mod" in names:
        return PROFILES["go"]
    if "pom.xml" in names:
        return PROFILES["maven"]
    if names & {"build.gradle", "build.gradle.kts"}:
        return PROFILES["gradle"]
    if any(f.endswith((".csproj", ".sln")) for f in names):
        return PROFILES["dotnet"]
    if "package.json" in names:
        pkg: dict[str, Any] = {}
        if read is not None:
            try:
                pkg = json.loads(read("package.json") or "{}")
            except (ValueError, TypeError):
                pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        script = str(pkg.get("scripts", {}).get("test", ""))
        if "vitest" in deps or "vitest" in script:
            return PROFILES["vitest"]
        if "jest" in deps or "jest" in script:
            return PROFILES["jest"]
        return PROFILES["node-test"]
    py_markers = {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini"}
    if names & py_markers or any(f.endswith(".py") for f in names):
        return PROFILES["pytest"]
    return None


_EXT_PROFILE = {
    ".py": "pytest",
    ".go": "go",
    ".js": "jest",
    ".jsx": "jest",
    ".mjs": "jest",
    ".cjs": "jest",
    ".ts": "jest",
    ".tsx": "jest",
    ".java": "maven",
    ".kt": "gradle",
    ".cs": "dotnet",
}


def profile_for_path(path: str) -> TestProfile | None:
    """The ecosystem a source file belongs to (by extension), for language-aware patch review."""
    return PROFILES.get(_EXT_PROFILE.get(Path(path).suffix.lower(), ""))


def is_test_path(path: str) -> bool:
    profile = profile_for_path(path)
    if profile is not None:
        return profile.is_test_path(path)
    return re.search(r"(^|/)(tests?|spec|__tests__)/", path) is not None


def criteria_in(*texts: str) -> list[str]:
    found = []
    for text in texts:
        for prefix, number, ac in CRITERION.findall(text or ""):
            key = f"{prefix}-{number}-AC{ac}"
            if key not in found:
                found.append(key)
    return found


# --- report parsers ------------------------------------------------------------------------------------


def _read_capped(path: Path) -> str:
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError(f"report {path.name} exceeds {MAX_REPORT_BYTES} bytes")
    return path.read_text(errors="replace")


def _xml(text: str) -> ET.Element:
    if re.search(r"<!DOCTYPE|<!ENTITY", text[:4096], re.I):
        raise ValueError("report contains a DOCTYPE or entity declaration")
    return ET.fromstring(text)  # noqa: S314 - DOCTYPE refused above


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_junit_xml(text: str) -> list[TestCase]:
    """JUnit XML as written by Surefire, Gradle, Vitest, node:test, pytest and most CI tools."""
    root = _xml(text)
    cases: list[TestCase] = []
    for tc in root.iter():
        if _local(tc.tag) != "testcase":
            continue
        name, cls = tc.get("name", ""), tc.get("classname", "")
        children = {_local(c.tag): c for c in tc}
        props = [
            p.get("value", "")
            for c in tc
            if _local(c.tag) == "properties"
            for p in c
            if p.get("name") in ("criterion", "criteria")
        ]
        if "failure" in children:
            outcome, el = "failed", children["failure"]
        elif "error" in children:
            outcome, el = "error", children["error"]
        elif "skipped" in children:
            outcome, el = "skipped", children["skipped"]
        else:
            outcome, el = "passed", None
        message = ""
        if el is not None:
            text_lines = (el.text or "").strip().splitlines()
            message = el.get("message") or (text_lines[0] if text_lines else "")
        try:
            duration = round(float(tc.get("time", "")), 4)
        except ValueError:
            duration = None
        cases.append(
            TestCase(
                nodeid=f"{cls}::{name}" if cls else name,
                outcome=outcome,  # type: ignore[arg-type]
                criteria=criteria_in(*props, name, cls),
                message=message,
                duration_s=duration,
            )
        )
    return cases


def parse_go_json(text: str) -> list[TestCase]:
    """`go test -json` event stream. Package-level failures (build errors) become error cases."""
    outcome: dict[tuple[str, str], str] = {}
    output: dict[tuple[str, str], list[str]] = {}
    elapsed: dict[tuple[str, str], float] = {}
    package_failed: dict[str, list[str]] = {}
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        pkg, test, action = str(ev.get("Package", "")), ev.get("Test"), ev.get("Action")
        if test is None:
            if action == "output":
                package_failed.setdefault(pkg, []).append(str(ev.get("Output", "")))
            if action == "fail":
                package_failed.setdefault(pkg, []).append("<package failed>")
            if action == "pass":
                package_failed.pop(pkg, None)
            continue
        key = (pkg, str(test))
        if action == "output":
            output.setdefault(key, []).append(str(ev.get("Output", "")))
        elif action in ("pass", "fail", "skip"):
            outcome[key] = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
            if isinstance(ev.get("Elapsed"), int | float):
                elapsed[key] = float(ev["Elapsed"])
    cases = []
    for (pkg, test), result in outcome.items():
        lines = [ln.strip() for ln in output.get((pkg, test), []) if ln.strip()]
        detail = [ln for ln in lines if not ln.startswith(("=== ", "--- "))]
        cases.append(
            TestCase(
                nodeid=f"{pkg}::{test}",
                outcome=result,  # type: ignore[arg-type]
                criteria=criteria_in(test),
                message=(detail[-1] if detail else "") if result == "failed" else "",
                duration_s=elapsed.get((pkg, test)),
            )
        )
    tested_packages = {pkg for pkg, _ in outcome}
    for pkg, lines in package_failed.items():
        if "<package failed>" in lines and pkg not in tested_packages:
            text_lines = [ln.strip() for ln in lines if ln.strip() and ln != "<package failed>"]
            cases.append(
                TestCase(
                    nodeid=f"{pkg}::<build>",
                    outcome="error",
                    criteria=[],
                    message=(text_lines[0] if text_lines else "package failed to build"),
                )
            )
    return cases


def parse_jest_json(text: str) -> list[TestCase]:
    data = json.loads(text)
    cases = []
    for suite in data.get("testResults", []):
        file = str(suite.get("name", suite.get("testFilePath", "")))
        results = suite.get("assertionResults") or suite.get("testResults") or []
        if not results and suite.get("status") == "failed":
            cases.append(
                TestCase(
                    nodeid=f"{file}::<suite>",
                    outcome="error",
                    criteria=[],
                    message=str(suite.get("message", ""))[:500],
                )
            )
        for r in results:
            status = r.get("status")
            outcome = {
                "passed": "passed",
                "failed": "failed",
                "pending": "skipped",
                "skipped": "skipped",
                "todo": "skipped",
                "disabled": "skipped",
            }.get(status, "error")
            title = str(r.get("fullName") or r.get("title", ""))
            msgs = r.get("failureMessages") or []
            cases.append(
                TestCase(
                    nodeid=f"{file}::{title}",
                    outcome=outcome,  # type: ignore[arg-type]
                    criteria=criteria_in(title, *[str(a) for a in r.get("ancestorTitles", [])]),
                    message=(str(msgs[0]).strip().splitlines() or [""])[0] if msgs else "",
                    duration_s=(r["duration"] / 1000) if isinstance(r.get("duration"), int | float) else None,
                )
            )
    return cases


def parse_trx(text: str) -> list[TestCase]:
    """Visual Studio TRX (`dotnet test --logger trx`)."""
    root = _xml(text)
    cases = []
    for el in root.iter():
        if _local(el.tag) != "UnitTestResult":
            continue
        name = el.get("testName", "")
        status = el.get("outcome", "")
        outcome = {
            "Passed": "passed",
            "Failed": "failed",
            "NotExecuted": "skipped",
            "Error": "error",
            "Timeout": "error",
            "Aborted": "error",
        }.get(status, "error")
        message = ""
        for m in el.iter():
            if _local(m.tag) == "Message" and m.text:
                message = m.text.strip().splitlines()[0]
                break
        cases.append(
            TestCase(
                nodeid=name,
                outcome=outcome,  # type: ignore[arg-type]
                criteria=criteria_in(name),
                message=message,
            )
        )
    return cases


def parse_pytest_evidence(text: str) -> list[TestCase]:
    cases = []
    for line in text.splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(c, dict) and {"nodeid", "outcome"} <= set(c):
            outcome = str(c["outcome"])
            if outcome not in ("passed", "failed", "skipped", "error"):
                outcome = "error"
            marked = [str(k) for k in c.get("criteria", [])]
            cases.append(
                TestCase(
                    nodeid=str(c["nodeid"]),
                    outcome=outcome,  # type: ignore[arg-type]
                    criteria=marked or criteria_in(str(c["nodeid"])),
                    message=str(c.get("message", "")),
                    duration_s=c.get("duration_s"),
                )
            )
    return cases


PARSERS = {
    "junit_xml": parse_junit_xml,
    "go_json": parse_go_json,
    "jest_json": parse_jest_json,
    "trx": parse_trx,
    "pytest_evidence": parse_pytest_evidence,
}
