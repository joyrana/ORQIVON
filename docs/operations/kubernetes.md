# Installing on Kubernetes

The platform ships as a Helm chart in [`infrastructure/helm/agentic-sdlc`](../../infrastructure/helm/agentic-sdlc).
One installation serves one customer. It runs three workloads plus a migration hook:

| Workload | Replicas | What it does | Privileges |
|---|---|---|---|
| `api` | 2+ | REST API, server-sent events, SSO, SCIM, metrics | Non-root (uid 10001), read-only root filesystem, no capabilities |
| `worker` | 1+ | Runs workflows, model calls, git, and the test sandbox | See [the worker](#the-worker-and-the-sandbox) |
| `web` | 2+ | Static web app; proxies `/api` to the API | Non-root nginx (uid 101), read-only root filesystem |
| `migrate` | hook | `alembic upgrade head` before every install and upgrade | Non-root, read-only root filesystem |

## Requirements

- Kubernetes 1.30 or newer (the worker uses the pod `appArmorProfile` field).
- PostgreSQL 14 or newer, managed by you (RDS, Cloud SQL, Azure Database, or an operator).
  The platform keeps its job queue, workflow checkpoints, rate-limit counters and worker
  heartbeats there, so every replica shares them. Back it up with point-in-time recovery.
- A `ReadWriteMany` volume for `/data` when you run more than one API or worker pod: uploaded
  documents, local repositories, repository mirrors and release artifacts (EFS, Azure Files,
  Filestore, NFS). The chart refuses `ReadWriteOnce` with more than one pod in total.
- An ingress controller with TLS. The chart routes `/api` and `/scim` to the API and `/` to the
  web app, and raises the read timeout for server-sent events.
- Container images built from `infrastructure/docker/` and pushed to your registry.

## Install

1. **Create the secret.** Secrets never go in values files. At minimum:

   ```bash
   kubectl create namespace agentic-sdlc
   kubectl -n agentic-sdlc create secret generic agentic-sdlc-secrets \
     --from-literal=DATABASE_URL='postgresql+psycopg://asw:<password>@db.internal:5432/asw?sslmode=require'
   ```

   Add the keys you use: `SCIM_BEARER_TOKEN`, `METRICS_TOKEN`, OIDC client secrets (named in
   `identity.yaml`). Put credentials only the worker needs (git hosting tokens, the GitHub App
   private key, `MODEL_ENDPOINT_API_KEY`) in a second secret and set `workerSecret`, so the
   internet-facing API never holds them.

2. **Write a values file.** Start from `values.yaml`. The usual changes:

   ```yaml
   image:
     api: { repository: registry.internal/asw/api, tag: "0.1.0" }
     web: { repository: registry.internal/asw/web, tag: "0.1.0" }
   publicBaseUrl: https://asw.example.com
   webBaseUrl: https://asw.example.com
   workerSecret: agentic-sdlc-worker
   env:
     MODEL_ENDPOINT_URL: http://vllm.models.svc:8000/v1
   ingress:
     host: asw.example.com
     tlsSecret: asw-tls
   config:
     identity: |
       # contents of identity.yaml: see docs/operations/identity.md
   ```

3. **Install.**

   ```bash
   helm install asw infrastructure/helm/agentic-sdlc -n agentic-sdlc -f my-values.yaml
   kubectl -n agentic-sdlc rollout status deploy/asw-agentic-sdlc-api
   curl -s https://asw.example.com/api/v1/ready
   ```

   `/api/v1/ready` reports the database, whether the schema matches the code, whether `/data`
   is writable, and how many workers sent a heartbeat. It returns 503 until all but the worker
   count are healthy.

The chart refuses settings that would install but not work: missing public URLs, `http` URLs
in production, shared `ReadWriteOnce` storage, several pods without persistence, an unknown
sandbox backend.

## The worker and the sandbox

The worker runs the code under test. Pick one of these, in order of isolation:

| Option | Values | Notes |
|---|---|---|
| **Namespace sandbox** (default) | `worker.sandboxBackend: namespace` | Each run gets new mount, PID, network, IPC and UTS namespaces, a minimal root filesystem (`pivot_root`), an unprivileged uid with no capabilities, and resource limits. The worker container needs `SYS_ADMIN`, `SETUID`, `SETGID`, `SETPCAP`, `CHOWN`, `FOWNER`, `DAC_OVERRIDE`, `KILL`, and unconfined seccomp/AppArmor to create those namespaces. CI runs the sandbox tests with exactly this capability set, read from the chart (`scripts/with-worker-caps.sh`). |
| **Namespace sandbox in gVisor** | add `worker.runtimeClassName: gvisor` | The worker itself runs on a user-space kernel, so a kernel exploit from test code hits gVisor, not the node. Requires the gVisor RuntimeClass on the node pool. Recommended where the code under test is not trusted. |
| **Docker backend** | `worker.sandboxBackend: docker` | Each run is a container. The worker needs a Docker daemon (`DOCKER_HOST`, for example a rootless dind sidecar or a dedicated VM) and `SANDBOX_DOCKER_RUNTIME=runsc` for gVisor. The worker pod itself then needs no added capabilities. |

Put workers on their own node pool (`worker.nodeSelector`, `worker.tolerations`) if your
policy keeps privileged workloads apart. The NetworkPolicy admits no inbound traffic to the
worker; restrict its egress (database, model endpoint, git hosts) in your own policy, because
the right destinations depend on your network.

Rollouts: `worker.terminationGracePeriodSeconds` (default 600) lets a running job finish.
Jobs are leased, so a job on a killed worker is picked up by another worker
after its lease expires; workflow state is checkpointed per stage, so it resumes, not restarts.

## Scaling

- **API**: stateless. Scale on CPU or on request latency. Rate limits are kept in PostgreSQL, so
  the per-token budget holds across replicas.
- **Worker**: each worker process handles one job at a time. Scale on `asw_job_queue_lag_seconds`
  (the age of the oldest due job). Sandbox runs are capped by `SANDBOX_MEMORY_BYTES` and
  `SANDBOX_TIMEOUT_S`; size the worker's memory limit above the sandbox limit.
- **Database**: modest. Hundreds of concurrent runs fit on 2 vCPU / 8 GB.

## Monitoring

- **Probes**: API liveness `/api/v1/health`, readiness `/api/v1/ready`; worker liveness runs
  `agentic-sdlc worker-health`, which fails if this pod's worker has not sent a heartbeat within
  two minutes or reports sandbox problems.
- **Metrics**: `GET /api/v1/metrics` (Prometheus text). Set `METRICS_TOKEN` to require a bearer
  token. With the Prometheus Operator, set `metrics.serviceMonitor.enabled: true`.
  Request metrics are per API pod; workflow, job, worker, approval and model-cost gauges are
  read from the database, so any one pod gives the whole picture.
- **Traces**: set `OTEL_TRACES_EXPORTER=otlp` and `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP over HTTP;
  `OTEL_EXPORTER_OTLP_HEADERS` for collector auth). Workflow stages, skills, model calls and
  sandbox runs are spans.
- **Alerts**: examples in [`infrastructure/config/prometheus-rules.example.yaml`](../../infrastructure/config/prometheus-rules.example.yaml):
  no live workers, queue lag, API error rate, approvals waiting too long, model spend.

## Upgrades

1. Read the release notes; take a backup (see [backup and recovery](backup-and-recovery.md)).
2. `helm upgrade asw infrastructure/helm/agentic-sdlc -n agentic-sdlc -f my-values.yaml`.
   The migration hook runs `alembic upgrade head` before new pods start. If it fails, the
   upgrade stops and the old pods keep serving.
3. Watch `/api/v1/ready` and `asw_workers_live`.

Migrations are written to be compatible with the previous release's code (add columns, backfill,
then remove in a later release), so old and new pods can run side by side during the rollout.

## Rollback

- **Code only** (no migration in the release): `helm rollback asw <revision> -n agentic-sdlc`.
- **With a migration**: the previous code may not understand the new schema. Run the previous
  release's `alembic downgrade <revision>` from a one-off pod of the *new* image (it has the
  downgrade scripts), then `helm rollback`. If a downgrade is not possible, restore the backup
  taken before the upgrade.

## Uninstall

`helm uninstall` keeps the data and backup volumes (`helm.sh/resource-policy: keep`) and
never touches the database. Delete those yourself when you mean to.
