# Validation Report

What was verified, how, and what was found and fixed along the way. First written for phases 1–8
(2026-10-11); updated 2026-11-13 for the commercial-readiness pull requests (#11–#21), on the tree of
the last of them. CI re-runs all of it on every PR.

## Environment

Linux x86_64, 2 vCPU, 7 GB RAM, no GPU. Python 3.13 locally and 3.12 in CI, Node 22,
PostgreSQL 16, Go 1.24. Docker Hub and the Helm download were blocked in the build environment, so
container images and the Compose stack are verified in GitHub Actions only, and the Helm chart is
rendered with the same Go template engine (`tools/helm-render`) and validated with offline Kubernetes
schemas; CI also runs real `helm lint` and `helm template`.

**CI status for PRs #13–#21:** GitHub did not start the jobs ("recent account payments have failed or
your spending limit needs to be increased"). Everything below was run locally on the final tree; CI
must be re-run once billing is fixed. PRs #11 and #12 passed every CI job.

## Results

| Check | How | Result |
|---|---|---|
| Backend tests | `pytest` as root against PostgreSQL (unit, integration, contract, workflow, security, e2e, eval harness), with fake OIDC, Git hosting, CI/CD, Jira, Azure DevOps, Confluence, Slack/Teams, model and OTLP servers over real HTTP | 359 passed, 0 skipped |
| Frontend unit tests | Vitest | 20 passed |
| Lint and types | ruff, ruff format, mypy, ESLint, Prettier, tsc | clean |
| Migrations | `alembic upgrade head` then `alembic check` on an empty database | no drift |
| API contract | OpenAPI document and typed client regenerated and compared | no drift |
| Browser E2E | Playwright drives all 15 steps through the UI, one signed-in person per role, fresh database | passed (26.8 s) |
| Deployment smoke | `scripts/smoke_slice.py` over HTTP against a running API and worker (CI: the Docker Compose stack) | passed locally; Compose in CI on PRs #11–#12 |
| Helm chart | `make helm-check`: 3 value sets rendered, every manifest validated against Kubernetes 1.30 and 1.33 schemas, least-privilege and routing policy checked; 5 invalid value sets must refuse to render | all passed |
| Licence policy | `scripts/licenses.py --check --verify` over the shipped Python and npm closure | 280 components allowed; notices current |
| Container images | API/worker and web images build | passed in CI |
| Dependency audit | `pip-audit`; `npm audit --omit=dev` | no known vulnerabilities in either (2026-11-13) |
| Offline evaluations | `python -m agentic_sdlc.evals.run --all` against `evals/baselines.json` | all suites within baseline |

### Evaluation metrics (rule-based strategies)

| Suite | Cases | Key results |
|---|---|---|
| requirements (2026.11.1) | 20 | statement recall 1.0, precision 1.0, locator validity 1.0, injection recall 1.0 with 0 false positives, ambiguity-term recall 1.0, contradiction recall 1.0 with 0 false positives |
| security_scanning (2026.11.1) | 55 | detection recall 1.0, false-positive rate 0.0 |
| planning | 7 | requirement coverage 1.0, cycle detection 1.0 |
| patch_policy | 20 | block recall 1.0, allow rate 1.0 |
| release_policy | 16 | exact blocker match 1.0, unsafe releases 0 |
| redaction | 11 | secrets removed 1.0, benign text unchanged 1.0 |

Contradiction recall was 0.5 until PR #21: "attachments ≤ 10 MB" vs "files up to 25 MB" was missed
because the subjects use different nouns. The detector now matches synonyms, converts units and
understands minimum/maximum bounds; six harder cases were added (unit conversion, minimum above
maximum, a valid range, same unit with different subjects, an exact value inside a limit). Twenty
cases is still a small set, so this measures the cases written, not real documents. Two skill
criteria are reported as **not measured offline**: the coding agent's acceptance pass rate
(covered only by the end-to-end tests) and token-estimate accuracy (needs actuals from real
model runs).

## Found and fixed during validation

| # | Finding | How it was found | Fix | Regression test |
|---|---|---|---|---|
| 1 | Platform git read `$HOME/.gitconfig` from the workspace, so a patch adding `.gitconfig` + `.gitattributes` could make git run a command on the platform | Security review; exploit reproduced | Private HOME and `GIT_CONFIG_GLOBAL` for every git process | `test_git_hardening.py` |
| 2 | Sandbox toolchain writable by the sandbox user on CI runners (inherited ACLs on `/opt`) | Isolation test failed in CI | Toolchain root-owned, ACLs stripped; sandbox refuses an unprotected toolchain | `test_sandbox.py` |
| 3 | Root worker could not clone origins created by the API user (git ignores `-c safe.directory` in local-clone upload-pack) | Compose smoke escalated at "implement" | `safe.directory` set in the platform git config | `test_clone_across_users` |
| 4 | Event stream held a database transaction for its whole life; the worker's first job stalled ~3 minutes behind `CREATE INDEX CONCURRENTLY` | Browser E2E timed out; worker stack captured with py-spy | Session released before streaming; checkpoint setup at worker start | `test_event_stream_releases_its_session` |
| 5 | Patch policy allowed weakening tests and committing PEM private keys | `patch_policy` evaluation | Blocks deleted test files, net test-function removal, new skip/xfail, PEM headers | `test_patch_review.py`, eval |
| 6 | In container images the sandbox masked the interpreter prefix instead of the source tree | Review of installed-package paths | Source root detected for checkouts and installed packages | Compose smoke |
| 7 | Release readiness showed "external CI: not configured" as a pass | Browser review | Shown as a warning | release-policy eval |
| 8 | The licence-policy parser read `MPL-2.0 AND (A OR B)` with the wrong precedence | Licence CI job output review | Recursive-descent SPDX parser | `licenses.py --check` in CI |
| 9 | A repository whose first sync failed could be used as an empty origin | Remote-git test | Origin requires a recorded synced head | `test_remote_git.py` |
| 10 | Resuming after an interrupt re-ran the node that scheduled the external-CI poll, queueing duplicates | Remote delivery test counted polls | Waiting moved to its own node (same pattern for deployments) | `test_remote_delivery.py`, `test_deployments.py` |
| 11 | Docker sandbox timeouts killed the client but left the container running | Fake Docker daemon test | Container killed by name on timeout | `test_sandbox.py` |
| 12 | Auto-detected but unsafe toolchains made the sandbox refuse to start | Browser E2E failure in CI (#13) | Detected toolchains are skipped when unsafe; configured ones still refuse | `test_sandbox.py` |
| 13 | Metrics lost the `/api/v1` router prefix in route labels | Metrics test | Prefix recovered from the matched route | `test_operations.py` |
| 14 | Contradictions across different nouns or units were missed | `requirements` evaluation | Synonyms, unit conversion and bound directions | eval + `test_extraction_rules.py` |

## Not verified here

- **A real model.** No GPU or model endpoint was available. The model path is exercised end to end
  against an OpenAI-compatible fake server (structured output, fallback, whole-file edits, test
  generation), and `make evals-live MODEL=…` measures a real one once configured.
- **Real third-party services.** Git hosts, identity providers, CI/CD systems, Jira, Azure DevOps,
  Confluence, Slack and Teams are tested against fakes that follow their documented APIs. Each
  connector needs one check against the real service in a customer or staging environment.
- **A real Kubernetes cluster and gVisor.** The chart is schema- and policy-validated and the
  Docker backend is tested against a fake daemon; neither has run on a cluster here.
- **Production deployment** stays disabled by policy until a customer enables it.
- **CI on PRs #13–#21** (billing; see above).

See [implementation-plan.md](implementation-plan.md) §3a for the remaining gaps against the phase
definitions.
