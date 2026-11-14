# ADR-0005: Sandbox backends — Linux namespaces (verified) and Docker (for deployments)

- Status: Accepted
- Date: 2026-10-10

## Context

Generated code must run isolated with no network, no credentials, non-root, resource-limited.
Docker Hub is blocked in the build environment, so container images cannot be pulled.

## Decision

`SandboxBackend` interface with two implementations:

1. **`namespace`** (default here, verified): `unshare --mount --net --pid --fork --mount-proc`;
   tmpfs masks over configured sensitive paths; `setpriv` to a dedicated non-root uid with
   `--no-new-privs`, cleared groups and all inheritable capabilities dropped; `prlimit` for
   address space, CPU seconds, processes, file size and open files; wall-clock timeout that
   kills the process group; environment scrubbed to an allow-list. Requires root (or
   `CAP_SYS_ADMIN`) for the platform worker.
2. **`docker`** (implemented, verified only in CI): `docker run --network none --read-only
   --user 64000 --cap-drop ALL --security-opt no-new-privileges --pids-limit --memory --cpus`
   with only the workspace mounted read-write.

## Consequences

The namespace backend does not hide world-readable host files (it does not pivot root); this
is a recorded residual risk (threat model T8). Production deployments should use the Docker
backend or a stronger isolation runtime (gVisor, Firecracker).

## Update (2026-11-13): isolated root by default

The namespace backend now assembles a minimal root inside its mount namespace and `pivot_root`s
into it (`SANDBOX_ISOLATED_ROOT=true`, the default): only `/usr`, the sandbox toolchains,
configured dependency caches and extra read-only paths, a generated `/etc` (passwd, group, hosts,
nsswitch) plus a read-only allow-list (`SANDBOX_ROOT_ETC`: linker cache, alternatives, JDK
configuration, CA bundle), private `/dev`, `/proc`, `/tmp`, and the single workspace exist. This
closes threat T8: world-readable host files are no longer reachable. Masked paths can never be
exposed through the allow-lists. The Docker backend gained gVisor (`SANDBOX_DOCKER_RUNTIME=runsc`),
memory/ulimit parity, and kills the container (not just the client) on timeout.
