# Threat Model

Method: STRIDE over the trust boundaries below. Each threat lists the control and its
verification status. "Residual" lists what is not mitigated.

## Assets

Source documents (may contain confidential business information), requirements and design
artifacts, repository contents, credentials (Git tokens, deployment tokens, model API keys),
approval records, audit log, deployment targets.

## Trust boundaries

1. Browser ↔ API (authenticated users of varying roles).
2. API/worker ↔ untrusted content (uploaded documents, repository files, model output).
3. Worker ↔ sandbox (generated/untrusted code execution).
4. Platform ↔ external systems (Git hosting, CI/CD, model endpoints).

## Threats and controls

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | Spoofed user / stolen token | Opaque tokens, sha256-hashed at rest, expiry + revocation; HTTPS required outside dev | Implemented; OIDC SSO with PKCE, full ID-token validation, single-use state, verified email, no account takeover by email; SCIM deactivation revokes all sessions (`test_sso_scim.py`) |
| T2 | Agent approves its own work | Approvals require a *human* principal holding the gate role; service accounts have no roles | Verified by tests |
| T3 | Approval for a different artifact than reviewed | Approval binds to content hash; changed artifact → request `stale`, decision 409 | Verified by tests |
| T4 | Cross-project data access | Every route resolves project membership; deny by default | Verified by tests |
| T5 | Prompt injection in documents | Content is passed to models only inside delimited data sections; system policy never sourced from documents; injection-like phrases flagged as warnings; extracted items must quote text that exists at the cited locator | Verified (heuristic + locator check) |
| T6 | Malicious DOCX (zip bomb, XXE, path traversal) | Size limit, zip entry count/uncompressed-size limits, magic check, `python-docx` (lxml with entity resolution disabled), no archive extraction to disk | Verified by tests |
| T7 | Generated code escapes / exfiltrates | Sandbox: separate network namespace (no network), PID namespace, non-root uid, `no_new_privs`, all capabilities dropped, masked secret paths, scrubbed environment, CPU/memory/file/process limits, wall-clock timeout | Verified by tests (namespace backend) |
| T8 | Sandbox reads host files | Namespace backend builds a fresh root (read-only `/usr`, toolchains, dependency caches, a minimal generated `/etc` plus a short read-only allow-list, private `/dev` `/proc` `/tmp`, the one workspace) and `pivot_root`s into it; the old root is detached. Docker backend: read-only image root, optional gVisor (`SANDBOX_DOCKER_RUNTIME=runsc`) | Verified by tests: a world-readable host file and another run's workspace are not visible; root, `/usr` and toolchain are read-only. The masking-only fallback (`SANDBOX_ISOLATED_ROOT=false`) is shown by a test to be weaker |
| T9 | Path traversal via patches / filenames | Patch paths validated (no absolute, no `..`, no `.git/`); upload filenames never used as paths | Verified by tests |
| T10 | Secret leakage to logs/events/prompts | Redaction filter on logs and tool-invocation arguments; secrets never placed in model prompts or sandbox env | Verified for redaction; review-based elsewhere |
| T11 | Tampering with audit log | DB trigger rejects UPDATE/DELETE on `audit_events` | Verified by tests |
| T12 | Unauthorized deployment | Production adapter disabled by default; release gate requires `release_approver`; deploy adapter refuses without an approved, non-stale decision | Verified by tests |
| T13 | Resource exhaustion (runaway loops, cost) | Bounded repair/revision loops; per-run token/cost/call/time budgets; per-token API rate limiting | Verified by tests (loops, budgets, rate limit) |
| T14 | Replayed / duplicate commands | Job dedup keys; single active run per project; spurious resumes do not advance a gate | Verified by tests |
| T15 | Supply chain | Pinned dependency ranges; `pip-audit` and `npm audit --audit-level=high` in CI | Running in CI; two low-severity npm advisories open (KaTeX via Mermaid, no fix without a Mermaid downgrade) |
| T16 | Privileged skill modification by untrusted input | Skills load only from the repository `skills/` directory at startup; no API to install skills | Verified by design (no endpoint) |
| T17 | Repository content configures platform git (e.g. a committed `.gitconfig` + `.gitattributes` filter runs a command on `git add`) | Every platform git process uses a private HOME and `GIT_CONFIG_GLOBAL` written by the platform; system config ignored; hooks disabled | Verified by tests (`test_git_hardening.py`); found during Phase 8 review, exploit reproduced on the earlier code |
| T18 | Sandboxed code modifies the sandbox toolchain to persist into later runs | Toolchain owned by root, no group/other write, no POSIX ACLs; the sandbox refuses to start otherwise | Verified by tests; found when CI runners granted write via inherited ACLs |
| T19 | Patch weakens the test suite to pass verification | Patch policy rejects deleted test files, net removal of test functions and new skip/xfail/disabled/focus markers in Python, Go, JavaScript, Java and C#; acceptance tests are written by QA from the criteria before any code exists and never shown to the developer agent | Verified by tests and the `patch_policy` evaluation |
| T20 | Git hosting credential leaks or is misused | Credentials only in the worker's environment/files, never in the DB; passed to git as a per-URL extraHeader via `GIT_CONFIG_*` env (not argv, not disk); sent only to the connection's own host (clone-URL host check, API base check, no redirects); redacted from errors; GitHub App tokens are per repository, least permission, one hour | Verified by tests (`test_remote_git.py`) |
| T21 | Platform pushes over, or merges into, protected code | Pushes only `asw/` branches, never forced; no merge operation exists; per-connection `operations` allow-list (read / push / pull_request / checks) | Verified by tests |
| T22 | Release on code the host's CI has not passed | Release readiness requires the host's CI to be green on the exact released commit (configurable to a warning per project); red CI escalates | Verified by tests |
| T23 | Model endpoint receives project content it should not | `MODEL_PRIVACY_POLICY=self_hosted_only` by default; external endpoints must be declared and their models labelled external; projects can narrow, not widen | Verified by tests |
| T24 | Worker pod privileges abused on Kubernetes | The worker gets only the capabilities namespace creation needs (SYS_ADMIN, SETUID, SETGID, SETPCAP, CHOWN, FOWNER, DAC_OVERRIDE, KILL); code under test gets none, runs as an unprivileged uid in its own root filesystem; no service-account token; no inbound traffic; optional gVisor RuntimeClass; worker-only secrets kept off the API | Chart policy checked in CI; sandbox tests run under exactly the chart's capability set |
| T25 | Backup leaks or is tampered with | Backups written owner-only and atomically; SHA-256 per part checked before restore touches anything; restore refuses non-empty targets and newer schemas; DB password passed via PGPASSWORD, not argv; off-cluster copy and encryption at rest documented | Verified by tests (`test_backup.py`) |
| T26 | An agent (or a person) ships a secret, injectable code or a vulnerable dependency | Security agent scans the cumulative change after regression; introduced findings at or above the project's severity (and any new secret) block and return as defects through the bounded repair loop; release requires a passing scan of the exact release commit; existing findings are shown to the approver; an incomplete scan escalates | Verified by tests (`test_repo_scanning.py`, `test_security_gate.py`) and the `security_scanning` evaluation (recall 1.0, false positives 0.0) |
| T27 | Risk acceptance used to wave findings through | Only project owners and release approvers, only people, only for findings a scan of the project actually reported, with a recorded reason; audited and revocable; committed critical credentials cannot be accepted | Verified by tests |

## Residual risks (honest list)

- The namespace sandbox shares the host kernel; a kernel vulnerability could break isolation.
  For untrusted code on shared hosts, use the Docker backend with gVisor (`runsc`).
- Dev authentication mode seeds known tokens; it refuses to start when `APP_ENV=production`.
- Heuristic injection detection is a signal, not a guarantee; the real control is that
  documents cannot change policy, tools or permissions.
- GitHub Actions deployment adapter is not exercised in this environment.
- Built-in code rules are line patterns, not data-flow analysis; add a SARIF analyser (Semgrep, CodeQL) for depth.
- Contradiction detection is heuristic (0.5 recall on the evaluation set): different nouns for
  the same subject are not linked.
