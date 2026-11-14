# Running a Repository's Tests

Every test run, whether it is the developer's own suite, QA's acceptance suite, or regression,
executes inside the sandbox with no network and no credentials. The platform reads results from the
test runner's own machine-readable report and cross-checks them against the runner's exit code.
Console output is never used as evidence.

## Supported ecosystems

| Profile | Detected by | Command | Report | Verified in CI |
|---|---|---|---|---|
| `pytest` | `pyproject.toml`, `setup.py`, `*.py` | `python -m pytest` + evidence plugin | JSON lines | **runs** |
| `go` | `go.mod` | `go test -json -count=1 ./...` | `go test -json` | **runs** (incl. a full delivery) |
| `node-test` | `package.json` without Jest/Vitest | `node --test` | JUnit XML | **runs** |
| `jest` | `jest` in `package.json` | `node node_modules/jest/bin/jest.js --ci --json` | Jest JSON | parser only |
| `vitest` | `vitest` in `package.json` | `node node_modules/vitest/vitest.mjs run --reporter=junit` | JUnit XML | parser only |
| `maven` | `pom.xml` | `mvn -o test` | Surefire JUnit XML | parser only |
| `gradle` | `build.gradle(.kts)` | `gradle test --offline` | JUnit XML | parser only |
| `dotnet` | `*.csproj`, `*.sln` | `dotnet test --no-restore --logger trx` | TRX | parser only |

"Parser only" means report parsing is tested against recorded reports, but the toolchain was not
run in the build environment because its dependencies cannot be fetched without network access.
Verify these on your own runners before relying on them.

## Configuring a repository

Detection covers the common layouts. To override it, an owner or engineer sets the repository's
test configuration:

```http
PUT /api/v1/projects/{project}/repositories/{repository}/test-config
{"test_config": {"profile": "gradle", "timeout_s": 1200, "env": {"CI": "1"}}}
```

`GET .../test-profile` shows what will run and whether the setting was configured or detected.

| Field | Meaning |
|---|---|
| `profile` | One of the profiles above |
| `command` | Replace the profile's command. A custom command with no profile also needs `report_format` and `report_globs` |
| `report_format` | `junit_xml`, `go_json`, `jest_json`, `trx` or `pytest_evidence` |
| `report_globs` | Report paths inside the test tree |
| `setup` | Up to 5 commands run before the tests, in the same sandbox (e.g. `["npm", "ci", "--offline"]`) |
| `env` | Extra environment variables (`PATH` and loader variables are refused) |
| `timeout_s` | 10–3600 |
| `acceptance_path` | Regular expression for where QA acceptance files may be placed |

## Dependencies without network access

Tests never get network access. A repository's dependencies must be either vendored (committed
`vendor/` or `node_modules/`), or present in a read-only cache on the worker that the sandbox user
can read:

```bash
SANDBOX_DEPENDENCY_CACHES='{"m2": "/srv/asw-caches/m2", "gradle": "/srv/asw-caches/gradle",
                            "nuget": "/srv/asw-caches/nuget", "npm": "/srv/asw-caches/npm"}'
```

Profiles reference these as `{m2}`, `{gradle_home}`, `{nuget}` and `{npm}`. Populate the caches from
your artifact repository (Artifactory, Nexus) on a schedule outside the sandbox.

Toolchains (`go`, `node`, `java`, `mvn`, `gradle`, `dotnet`) are found on the worker's `PATH`, or
listed in `SANDBOX_TOOL_PATHS`. The sandbox refuses to run if a toolchain directory, or its root
(`GOROOT`, `JAVA_HOME`), is writable by the sandbox user or carries POSIX ACLs. A writable toolchain
would let one run plant code that a later run executes.

## How tests are tied to acceptance criteria

| Ecosystem | How a test names its criterion |
|---|---|
| pytest | `@pytest.mark.criterion("FR-001-AC1")` |
| Go, Java, C# | the key in the test name, e.g. `TestFR_001_AC1_RejectsLongTitles` |
| JavaScript, JUnit display names | the key in the title, e.g. `test("[FR-001-AC1] rejects long titles")` |

The QA agent writes acceptance tests in the repository's own language and places them where the
profile allows: for example `asw_acceptance_<topic>_test.go` next to the Go package under test,
`asw_acceptance/*.test.js`, `src/test/java/asw/acceptance/*Test.java`, or an `AswAcceptance/` folder
in a .NET test project. They are added to the test tree only for acceptance and regression runs;
they are never committed to the repository and never shown to the Developer agent.

## Patch policy across languages

Before a change is applied, the policy blocks deleted test files, a net removal of test functions,
and newly skipped, disabled or focused tests:

- Python: `skip`, `xfail`
- Go: `t.Skip`
- JavaScript: `.skip`, `.only`, `xit`
- Java: `@Disabled`, `@Ignore`
- C#: `Skip =`, `[Ignore]`

Test files in any language may be added or changed outside the approved file list. Other files may
not.
