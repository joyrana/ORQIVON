"""Language profiles: detection, configuration, report parsing and language-aware patch review."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_sdlc.execution.edits import FileEdit, edits_to_diff
from agentic_sdlc.execution.test_profiles import (
    PROFILES,
    TestConfig,
    criteria_in,
    detect_profile,
    is_test_path,
    parse_go_json,
    parse_jest_json,
    parse_junit_xml,
    parse_trx,
    resolve_profile,
)
from agentic_sdlc.execution.workspace import review_patch


@pytest.mark.parametrize(
    ("files", "package_json", "expected"),
    [
        (["go.mod", "calc.go"], None, "go"),
        (["pom.xml", "src/main/java/A.java"], None, "maven"),
        (["build.gradle.kts"], None, "gradle"),
        (["Api/Api.csproj", "Api.Tests/Api.Tests.csproj"], None, "dotnet"),
        (["package.json"], {"devDependencies": {"vitest": "^3"}}, "vitest"),
        (["package.json"], {"devDependencies": {"jest": "^30"}}, "jest"),
        (["package.json"], {"scripts": {"test": "node --test"}}, "node-test"),
        (["pyproject.toml", "pkg/a.py"], None, "pytest"),
        (["README.md", "Makefile"], None, None),
    ],
)
def test_detection(files: list[str], package_json: dict | None, expected: str | None) -> None:
    read = (lambda p: json.dumps(package_json)) if package_json is not None else None
    profile = detect_profile(files, read)
    assert (profile.name if profile else None) == expected


def test_explicit_configuration_overrides_detection() -> None:
    p = resolve_profile({"profile": "gradle", "timeout_s": 1200, "env": {"CI": "1"}}, ["pom.xml"])
    assert p is not None and p.name == "gradle" and p.default_timeout_s == 1200 and p.env["CI"] == "1"
    custom = resolve_profile(
        {"command": ["make", "test"], "report_format": "junit_xml", "report_globs": ["out/*.xml"]},
        ["Makefile"],
    )
    assert custom is not None and custom.name == "custom" and custom.command == ("make", "test")
    assert resolve_profile({}, ["Makefile"]) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"profile": "cobol"},
        {"command": []},
        {"report_globs": ["../outside.xml"]},
        {"report_globs": ["/etc/passwd"]},
        {"env": {"LD_PRELOAD": "x"}},
        {"env": {"lower": "x"}},
        {"command": ["make", "test"]},  # custom command without a report format
        {"timeout_s": 99999},
        {"unknown_field": 1},
    ],
)
def test_invalid_configurations_are_rejected(bad: dict) -> None:
    with pytest.raises(ValidationError):
        TestConfig.model_validate(bad)


def test_criterion_keys_are_found_in_any_naming_style() -> None:
    assert criteria_in("[FR-001-AC1] rejects long titles") == ["FR-001-AC1"]
    assert criteria_in("TestFR_001_AC2_AcceptsBoundary") == ["FR-001-AC2"]
    assert criteria_in("NFR_12_AC3_isFast and FR-1-AC1") == ["NFR-12-AC3", "FR-1-AC1"]
    assert criteria_in("ZZFR-001-AC1") == ["ZZFR-001-AC1"]  # any uppercase prefix is a key
    assert criteria_in("9FR-001-AC1") == []  # a digit cannot precede a key
    assert criteria_in("FR-001-AC1234") == []  # at most three digits after AC
    assert criteria_in("version 1.2.3") == []


SUREFIRE = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" name="asw.acceptance.TitleTest"
  time="0.041" tests="3" errors="0" skipped="1" failures="1">
  <properties><property name="java.version" value="21"/></properties>
  <testcase name="FR_001_AC1_rejectsLongTitles" classname="asw.acceptance.TitleTest" time="0.012"/>
  <testcase name="[FR-001-AC2] accepts 120 characters" classname="asw.acceptance.TitleTest" time="0.003">
    <failure message="expected: &lt;120&gt; but was: &lt;119&gt;"
      type="org.opentest4j.AssertionFailedError">org.opentest4j.AssertionFailedError: expected ...
	at asw.acceptance.TitleTest.accepts(TitleTest.java:21)</failure>
  </testcase>
  <testcase name="ignored" classname="asw.acceptance.TitleTest" time="0"><skipped/></testcase>
</testsuite>"""


def test_surefire_and_gradle_junit_xml() -> None:
    cases = parse_junit_xml(SUREFIRE)
    assert [(c.outcome, c.criteria) for c in cases] == [
        ("passed", ["FR-001-AC1"]),
        ("failed", ["FR-001-AC2"]),
        ("skipped", []),
    ]
    assert cases[1].message == "expected: <120> but was: <119>"
    assert cases[0].nodeid == "asw.acceptance.TitleTest::FR_001_AC1_rejectsLongTitles"


def test_junit_xml_from_vitest_and_node_test_with_errors_and_properties() -> None:
    xml = """<testsuites name="vitest tests"><testsuite name="asw_acceptance/title.test.ts">
      <testcase classname="asw_acceptance/title.test.ts" name="title &gt; [FR-002-AC1] trims" time="0.002">
        <properties><property name="criterion" value="FR-002-AC2"/></properties>
      </testcase>
      <testcase classname="asw_acceptance/title.test.ts" name="setup" time="0">
        <error message="Cannot find module './title'">Error: Cannot find module</error>
      </testcase></testsuite></testsuites>"""
    cases = parse_junit_xml(xml)
    assert cases[0].outcome == "passed" and cases[0].criteria == ["FR-002-AC2", "FR-002-AC1"]
    assert cases[1].outcome == "error" and "Cannot find module" in cases[1].message


def test_xml_reports_with_doctype_are_refused() -> None:
    evil = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="&a;"/></testsuite>'
    )
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_junit_xml(evil)


def test_go_json_including_build_failures() -> None:
    events = [
        {"Action": "run", "Package": "ex/calc", "Test": "TestFR_001_AC1_Adds"},
        {"Action": "output", "Package": "ex/calc", "Test": "TestFR_001_AC1_Adds", "Output": "=== RUN\n"},
        {
            "Action": "output",
            "Package": "ex/calc",
            "Test": "TestFR_001_AC1_Adds",
            "Output": "  calc_test.go:9: got 3\n",
        },
        {"Action": "fail", "Package": "ex/calc", "Test": "TestFR_001_AC1_Adds", "Elapsed": 0.01},
        {"Action": "pass", "Package": "ex/calc", "Test": "TestOther", "Elapsed": 0},
        {"Action": "skip", "Package": "ex/calc", "Test": "TestSkipped"},
        {"Action": "fail", "Package": "ex/calc", "Elapsed": 0.2},
        {"Action": "output", "Package": "ex/broken", "Output": "broken/x.go:3:1: syntax error\n"},
        {"Action": "fail", "Package": "ex/broken", "Elapsed": 0},
    ]
    cases = parse_go_json("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
    by_id = {c.nodeid: c for c in cases}
    failed = by_id["ex/calc::TestFR_001_AC1_Adds"]
    assert failed.outcome == "failed" and failed.criteria == ["FR-001-AC1"]
    assert failed.message == "calc_test.go:9: got 3"
    assert by_id["ex/calc::TestOther"].outcome == "passed"
    assert by_id["ex/calc::TestSkipped"].outcome == "skipped"
    assert (
        by_id["ex/broken::<build>"].outcome == "error"
        and "syntax error" in by_id["ex/broken::<build>"].message
    )


def test_jest_json() -> None:
    report = {
        "testResults": [
            {
                "name": "/w/asw_acceptance/title.test.js",
                "assertionResults": [
                    {
                        "fullName": "title [FR-001-AC1] rejects",
                        "status": "passed",
                        "duration": 4,
                        "ancestorTitles": [],
                    },
                    {
                        "fullName": "title accepts boundary",
                        "status": "failed",
                        "ancestorTitles": ["FR-001-AC2"],
                        "failureMessages": ["Error: expect(received).toBe(expected)\n  at ..."],
                    },
                    {"fullName": "later", "status": "todo"},
                ],
            },
            {
                "name": "/w/broken.test.js",
                "status": "failed",
                "message": "SyntaxError",
                "assertionResults": [],
            },
        ]
    }
    cases = parse_jest_json(json.dumps(report))
    assert [(c.outcome, c.criteria) for c in cases] == [
        ("passed", ["FR-001-AC1"]),
        ("failed", ["FR-001-AC2"]),
        ("skipped", []),
        ("error", []),
    ]
    assert cases[0].duration_s == 0.004 and cases[1].message == "Error: expect(received).toBe(expected)"


def test_trx() -> None:
    trx = """<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Results>
  <UnitTestResult testName="Api.Tests.AswAcceptance.TitleTests.FR_001_AC1_RejectsLong" outcome="Passed"/>
  <UnitTestResult testName="Api.Tests.AswAcceptance.TitleTests.FR_001_AC2_Boundary" outcome="Failed">
    <Output><ErrorInfo><Message>Assert.Equal() Failure
Expected: 120</Message></ErrorInfo></Output></UnitTestResult>
  <UnitTestResult testName="Api.Tests.Other" outcome="NotExecuted"/>
</Results></TestRun>"""
    cases = parse_trx(trx)
    assert [(c.outcome, c.criteria) for c in cases] == [
        ("passed", ["FR-001-AC1"]),
        ("failed", ["FR-001-AC2"]),
        ("skipped", []),
    ]
    assert cases[1].message == "Assert.Equal() Failure"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("calc/calc_test.go", True),
        ("calc/calc.go", False),
        ("src/title.test.ts", True),
        ("src/__tests__/x.js", True),
        ("src/title.ts", False),
        ("src/test/java/a/TitleTest.java", True),
        ("src/main/java/a/Title.java", False),
        ("Api.Tests/TitleTests.cs", True),
        ("Api/Title.cs", False),
        ("tests/test_a.py", True),
        ("pkg/a.py", False),
    ],
)
def test_test_paths_per_language(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


@pytest.mark.parametrize(
    ("path", "old", "new", "violation"),
    [
        (
            "calc/calc_test.go",
            "package calc\n\nfunc TestA(t *testing.T) {}\n",
            'package calc\n\nfunc TestA(t *testing.T) { t.Skip("later") }\n',
            "skip",
        ),
        (
            "calc/calc_test.go",
            "package calc\n\nfunc TestA(t *testing.T) {}\nfunc TestB(t *testing.T) {}\n",
            "package calc\n\nfunc TestA(t *testing.T) {}\n",
            "removes 1 test function",
        ),
        ("src/a.test.js", "test('a', () => {});\n", "test.only('a', () => {});\n", "skip"),
        ("src/test/java/ATest.java", "@Test\nvoid a() {}\n", "@Disabled\n@Test\nvoid a() {}\n", "skip"),
        (
            "Api.Tests/ATests.cs",
            "[Fact]\npublic void A() {}\n",
            '[Fact(Skip = "flaky")]\npublic void A() {}\n',
            "skip",
        ),
    ],
)
def test_patch_review_catches_weakened_tests_in_every_language(
    path: str, old: str, new: str, violation: str
) -> None:
    review = review_patch(edits_to_diff([FileEdit(path, "modify", new)], {path: old}), allowed_files=set())
    assert any(violation in v for v in review.violations), review.violations


def test_tests_in_any_language_may_be_added_outside_the_plan() -> None:
    diff = edits_to_diff(
        [
            FileEdit("calc/calc.go", "modify", "package calc\n// changed\n"),
            FileEdit("calc/extra_test.go", "create", "package calc\n"),
            FileEdit("calc/other.go", "create", "package calc\n"),
        ],
        {"calc/calc.go": "package calc\n"},
    )
    review = review_patch(diff, allowed_files={"calc/calc.go"})
    assert review.violations == ["changes files outside the approved plan: calc/other.go"]


def test_every_profile_has_working_patterns() -> None:
    import re

    for p in PROFILES.values():
        for pattern in (p.acceptance_path, p.test_path, p.test_def, p.skip_mark):
            re.compile(pattern)
        assert p.report_globs and p.command
