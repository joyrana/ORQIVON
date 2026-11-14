# Security scanning of target repositories

After the regression suite passes, the **Security agent** scans the change before it is
published or released. The scan covers the run's cumulative change: everything from the commit
the run started on to the commit about to be released.

```
implement → QA tests → BA verification → regression → security scan → pull request → release readiness
                 ↑                                             │
                 └──────── security defect (repair loop) ──────┘
```

## What is scanned

| Scanner | Covers | Needs |
|---|---|---|
| `secrets` | Every tracked text file at the head commit: private keys; AWS, GitHub, GitLab, Slack, Stripe, Google, Azure and model-provider keys; JWTs; passwords in connection strings; high-entropy values assigned to credential-named fields | Nothing |
| `code` | Dangerous constructs in the files the change touched, in Python, JavaScript/TypeScript, Go, Java/Kotlin and C# (dynamic code execution, shell commands from strings, unsafe deserialization, SQL built with string formatting, TLS verification turned off, HTML injection sinks...) | Nothing |
| `dependencies` | Packages pinned to exact versions: `requirements*.txt`, `poetry.lock`, `uv.lock`, `pyproject.toml` `==` pins, `package-lock.json`, exact versions in `package.json`, `go.mod`, `pom.xml`, `*.csproj` / `Directory.Packages.props`. These are checked against OSV advisories | An advisory source (below) |
| `sarif:<name>` | Any analyser that writes SARIF 2.1.0: Semgrep, CodeQL, Bandit, gosec, SpotBugs, Roslyn analysers... | The tool on the worker, listed in the project's security settings |

The built-in rules are line patterns, not data-flow analysis. They are few and specific,
and they run on every repository with no setup. For deeper analysis, add a SARIF analyser.
Precision and recall of the built-in rules are measured on a labelled dataset
(`evals/security_scanning`) on every CI run. Both are pinned: 100% detection and 0% false
positives on that set.

Secrets are redacted before they leave the scanner. The platform stores and shows only a
masked excerpt (`AKIA************Q2`), never the value.

## What blocks

Only findings **the change introduced** can block: a secret on an added line, a risky call on
an added line, or a vulnerable package version the change pinned. Findings that were already
in the repository are reported, and the release readiness check shows them as a warning, so
the approver sees them. They are not pinned on this change.

The project's policy (Project settings → Security gate) decides:

| Setting | Default | Meaning |
|---|---|---|
| `block_severity` | `high` | Introduced findings at this severity or above block |
| `block_secrets` | `true` | Any introduced secret blocks, whatever its severity |
| `require_dependency_scan` | `false` | With no advisory source configured, the scan is an error, not a pass with a warning |
| `tools` | `[]` | SARIF analysers to run (below) |

What happens on the result:

- **Passed**: the change goes on to publication and release readiness. Readiness requires a
  passing scan of the exact release commit.
- **Blocked**: each blocking finding becomes a defect (with a bug in the backlog) and goes back
  to the Developer agent. This is the same bounded repair loop as failing tests
  (`max_repair_attempts`). When the limit is reached, the run escalates to a person.
- **Error**: a configured analyser failed, or dependency scanning is required but unavailable.
  The run escalates. An incomplete scan is never treated as a pass.

## Accepting a risk

A project owner or release approver can accept a blocking finding as a risk, with a reason of
at least 20 characters (Verification and QA → Security). Accepted findings no longer block
later scans of the project.

- Each acceptance records who accepted it, when, and why, and is written to the audit log.
- An acceptance can be revoked.
- A committed critical credential cannot be accepted. Remove it from the repository and rotate it.

Accepting a finding does not resume a run that has already escalated. Start a new run; it
will honour the acceptance.

## Dependency advisories

Self-hosted installations usually should not send their dependency list to a third party,
so the primary source is a **local mirror of OSV**:

```bash
agentic-sdlc advisories sync --dest /data/advisories          # PyPI npm Go Maven NuGet
export SECURITY_ADVISORY_DIR=/data/advisories                 # API and worker
```

- `sync` downloads each ecosystem's export, keeps only the JSON records, and swaps the
  directory in atomically.
- An export that is empty or corrupt leaves the previous mirror in place.
- `--source` points at an internal mirror of the export bucket.

On Kubernetes, set `advisories.enabled: true`. A nightly CronJob keeps `/data/advisories`
current and the API and worker are configured to read it.

As an alternative, `SECURITY_OSV_API_URL` points at an OSV-compatible API: `https://api.osv.dev`,
or a self-hosted one. It sends package names and versions only, never code.

- With neither source configured, the dependency scanner reports "not configured", and release
  readiness shows it as a warning.
- If an advisory has no published severity, it is treated as **high**.

## Adding a SARIF analyser

```json
[
  {
    "name": "semgrep",
    "command": ["semgrep", "scan", "--config", ".semgrep.yml", "--sarif", "--output", ".asw/semgrep.sarif"],
    "sarif": ".asw/semgrep.sarif",
    "timeout_s": 900
  }
]
```

- The analyser runs in the same sandbox as the tests: no network, an unprivileged user,
  resource limits, and a copy of the repository at the head commit. That means rules and
  databases must be available locally, for example rules checked into the repository or baked
  into the worker image.
- Install the tool on the worker and make it visible in the sandbox (`SANDBOX_TOOL_PATHS`).
- Findings map to severities from the rule's `security-severity` property when present (the
  GitHub convention), otherwise from the SARIF level (`error` → high, `warning` → medium,
  `note` → low).
- A tool that times out or writes no report makes the scan an error.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/projects/{id}/security-scans?run_id=` | anyone who can read quality evidence |
| GET / PUT | `/api/v1/projects/{id}/security-config` | read: readers; write: project owner |
| GET | `/api/v1/projects/{id}/security-acceptances` | readers |
| POST / DELETE | `/api/v1/projects/{id}/security-acceptances` | project owner, release approver (people only) |
