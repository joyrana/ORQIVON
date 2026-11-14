"""Sandbox backends for untrusted code (ADR-0005).

`NamespaceSandbox` (verified here): separate mount, network and PID namespaces; tmpfs masks over
sensitive paths (configured paths plus the platform's own data and source directories); drop to a
dedicated non-root uid with no_new_privs, no supplementary groups and no capabilities; rlimits for
address space, CPU time, processes, file size and open files; wall-clock timeout that kills the whole
process tree; scrubbed environment. The workspace is the only writable location.

`DockerSandbox`: --network none, read-only root, non-root user, all capabilities dropped,
no-new-privileges, pids/memory/cpu limits, only the workspace mounted. Verified in CI only.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from agentic_sdlc.core.logging import log, redact
from agentic_sdlc.core.settings import REPO_ROOT, Settings, get_settings

logger = logging.getLogger("agentic_sdlc.sandbox")

MAX_CAPTURE = 64 * 1024


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    backend: str
    limits: dict[str, int] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        d = asdict(self)
        d.pop("stdout")
        d.pop("stderr")
        return d


class SandboxError(Exception):
    pass


class SandboxBackend(Protocol):
    name: str

    def run(
        self,
        command: list[str],
        workdir: Path,
        *,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        address_space_bytes: int | None = None,
    ) -> SandboxResult: ...


TOOLS = ("go", "node", "java", "mvn", "gradle", "dotnet")


def tool_paths(settings: Settings) -> list[str]:
    """Toolchain directories added to the sandbox PATH.

    Configured directories (`SANDBOX_TOOL_PATHS`) are used as given and checked strictly: an unsafe
    one stops the sandbox. Detected ones (the directories of go, node, java... on the worker's PATH)
    are skipped when unsafe, writable by the sandbox user or hidden by a masked path, so an
    unrelated tool install can never disable the sandbox or slip in an unsafe toolchain.
    """
    import shutil

    if settings.sandbox_tool_paths is not None:
        return [p for p in settings.sandbox_tool_paths if Path(p).is_dir()]
    masked = [
        Path(c).resolve()
        for c in [*settings.sandbox_masked_paths, str(settings.data_dir), str(_source_root(settings))]
        if Path(c).exists()
    ]
    found: list[str] = []
    for tool in TOOLS:
        exe = shutil.which(tool)
        if exe is None:
            continue
        d = Path(exe).parent
        if str(d) in found or str(d) in ("/usr/local/bin", "/usr/bin", "/bin"):
            continue
        if any(d.resolve().is_relative_to(m) for m in masked):
            log(logger, logging.INFO, "sandbox_tool_skipped", path=str(d), reason="under a masked path")
            continue
        problems = [p for x in _tool_dirs(d) for p in _dir_problems(x, settings)]
        if problems:
            log(logger, logging.WARNING, "sandbox_tool_skipped", path=str(d), reason=problems[0])
            continue
        found.append(str(d))
    return found


def _tool_dirs(bin_dir: Path) -> set[Path]:
    # The bin directory and the toolchain root (GOROOT, JAVA_HOME...): tools load code from both.
    return {bin_dir, bin_dir.resolve(), bin_dir.resolve().parent}


def _dir_problems(d: Path, settings: Settings) -> list[str]:
    try:
        st = d.stat()
    except OSError:
        return []
    problems = []
    if st.st_uid == settings.sandbox_uid:
        problems.append(f"{d} is owned by the sandbox user")
    if st.st_mode & 0o002 or (st.st_mode & 0o020 and st.st_gid == settings.sandbox_uid):
        problems.append(f"{d} is writable by the sandbox user (mode {oct(st.st_mode & 0o777)})")
    try:
        # Mode bits do not show POSIX ACL grants; any extended ACL is treated as unsafe.
        if os.getxattr(d, "system.posix_acl_access"):
            problems.append(f"{d} carries POSIX ACLs (remove them with `setfacl -b`)")
    except OSError:
        pass
    return problems


def _tail(path: Path) -> str:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - MAX_CAPTURE))
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    return redact(("[…truncated…]\n" if size > MAX_CAPTURE else "") + text)


def ensure_traversable(path: Path) -> None:
    """Grant search (o+x, never read) on `path` and its ancestors so the sandbox uid can reach the
    workspace. Listing stays forbidden; only the workspace itself is owned by the sandbox uid."""
    for p in [path, *path.parents]:
        if p == Path("/"):
            break
        mode = p.stat().st_mode
        if not mode & 0o001:
            os.chmod(p, (mode & 0o7777) | 0o001)


def _base_env(workdir: Path, settings: Settings, extra: dict[str, str] | None) -> dict[str, str]:
    toolchain_bin = str(Path(settings.sandbox_python).parent)
    path = [toolchain_bin, *tool_paths(settings), "/usr/local/bin", "/usr/bin", "/bin"]
    env = {
        "PATH": ":".join(dict.fromkeys(path)),
        "HOME": str(workdir),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": str(workdir / ".tmp"),
    }
    for k, v in (extra or {}).items():
        if k.upper() in ("PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME") or not k.isidentifier():
            raise SandboxError(f"environment variable {k} is not allowed")
        env[k] = v
    return env


def _source_root(settings: Settings) -> Path:
    """The platform's own source tree. In a checkout that is the repository root; in an installed
    package (container image) REPO_ROOT points into the interpreter prefix, so use the directory
    that holds the skills instead."""
    if (REPO_ROOT / "skills").is_dir() and (REPO_ROOT / "services").is_dir():
        return REPO_ROOT
    return settings.skills_dir.resolve().parent


def toolchain_problems(settings: Settings) -> list[str]:
    """The sandbox interpreter must not be modifiable by the sandbox user: a writable toolchain
    would let one run plant code that a later run (or the platform's test evidence) executes."""
    python = Path(settings.sandbox_python)
    dirs = {python.resolve().parent, python.resolve().parent.parent, python.parent, python.parent.parent}
    for t in tool_paths(settings):
        dirs |= _tool_dirs(Path(t))
    problems = [p for d in dirs for p in _dir_problems(d, settings)]
    return sorted(problems)


def _masked_paths(settings: Settings, workdir: Path) -> list[str]:
    candidates = [*settings.sandbox_masked_paths, str(settings.data_dir), str(_source_root(settings))]
    out = []
    for c in candidates:
        p = Path(c).resolve()
        if not p.exists() or not p.is_dir():
            continue
        if workdir.resolve().is_relative_to(p) or Path(settings.sandbox_python).resolve().is_relative_to(p):
            raise SandboxError(f"masked path {p} would hide the workspace or the sandbox toolchain")
        hidden = [t for t in tool_paths(settings) if Path(t).resolve().is_relative_to(p)]
        if hidden:
            raise SandboxError(
                f"masked path {p} would hide sandbox toolchain {hidden[0]}; move the toolchain"
            )
        out.append(str(p))
    return sorted(set(out))


SCRATCH = ("/tmp", "/var/tmp", "/dev/shm")  # noqa: S108 - these are masked with private tmpfs


def _scratch_paths(workdir: Path) -> list[str]:
    """Shared temp dirs get a private, empty tmpfs unless the workspace lives inside them."""
    return [p for p in SCRATCH if Path(p).is_dir() and not workdir.is_relative_to(Path(p).resolve())]


INNER = """#!/bin/sh
# Runs as root inside fresh mount/net/pid namespaces, masks paths, then drops privileges for good.
set -e
for p in {masks}; do
  if [ -d "$p" ]; then mount -t tmpfs -o size=1m,mode=000 tmpfs "$p"; fi
done
for p in {scratch}; do
  if [ -d "$p" ]; then mount -t tmpfs -o size=64m,mode=1777 tmpfs "$p"; fi
done
exec setpriv --reuid={uid} --regid={uid} --clear-groups --no-new-privs --inh-caps=-all --bounding-set=-all \\
  prlimit --as={mem} --cpu={cpu} --nproc={nproc} --fsize={fsize} --nofile=256 -- "$@"
"""


# Isolated root (default): a fresh tmpfs root holding only what tests need, then pivot_root into it,
# so nothing else on the host filesystem is visible at all (not merely masked).
INNER_ISOLATED = """#!/bin/sh
set -e
R={root}
mount -t tmpfs -o size=16m,mode=755,nosuid,nodev tmpfs "$R"
mkdir -p "$R/proc" "$R/dev" "$R/tmp" "$R/var/tmp" "$R/etc" "$R/.oldroot"
for d in {ro_dirs}; do
  [ -n "$d" ] || continue
  mkdir -p "$R$d"
  mount --bind "$d" "$R$d"
  mount -o remount,bind,ro,nosuid,nodev "$R$d"
done
for f in {ro_files}; do
  [ -n "$f" ] || continue
  mkdir -p "$R$(dirname "$f")"; touch "$R$f"
  mount --bind "$f" "$R$f"
  mount -o remount,bind,ro,nosuid,nodev "$R$f"
done
{links}
printf 'root:x:0:0::/:/bin/false\\nsandbox:x:{uid}:{uid}::/tmp:/bin/false\\n' > "$R/etc/passwd"
printf 'root:x:0:\\nsandbox:x:{uid}:\\n' > "$R/etc/group"
printf '127.0.0.1 localhost\\n::1 localhost\\n' > "$R/etc/hosts"
printf 'passwd: files\\ngroup: files\\nhosts: files\\n' > "$R/etc/nsswitch.conf"
mount -t tmpfs -o size=1m,mode=755,nosuid tmpfs "$R/dev"
for n in null zero full random urandom tty; do
  touch "$R/dev/$n"; mount --bind "/dev/$n" "$R/dev/$n"
done
ln -s /proc/self/fd "$R/dev/fd"
ln -s fd/0 "$R/dev/stdin"; ln -s fd/1 "$R/dev/stdout"; ln -s fd/2 "$R/dev/stderr"
mkdir -p "$R/dev/shm"; mount -t tmpfs -o size=64m,mode=1777,nosuid,nodev tmpfs "$R/dev/shm"
mount -t tmpfs -o size=256m,mode=1777,nosuid,nodev tmpfs "$R/tmp"
mount -t tmpfs -o size=64m,mode=1777,nosuid,nodev tmpfs "$R/var/tmp"
mount -t proc -o nosuid,nodev,noexec proc "$R/proc"
mkdir -p "$R{work}"
mount --bind {work} "$R{work}"
mount -o remount,bind,nosuid,nodev "$R{work}"
mount -o remount,ro,nosuid,nodev "$R"
cd "$R"
{pivot_root} . .oldroot
umount -l /.oldroot
cd {work}
exec setpriv --reuid={uid} --regid={uid} --clear-groups --no-new-privs --inh-caps=-all --bounding-set=-all \\
  prlimit --as={mem} --cpu={cpu} --nproc={nproc} --fsize={fsize} --nofile=256 -- "$@"
"""


def _tool_path(name: str) -> str:
    import shutil

    found = shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")
    if not found:
        raise SandboxError(f"the isolated sandbox root needs `{name}` (util-linux)")
    return found


def root_layout(settings: Settings, workdir: Path) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """(read-only directories, read-only files, symlinks) that make up the isolated root.

    Every path is bound at its real location. A configured path that is a symlink (e.g. /usr/local/go
    pointing at a versioned directory) is recreated as a symlink unless an already-bound directory
    contains it. Masked paths are never exposed.
    """
    import glob

    wanted = [
        "/usr",
        str(Path(settings.sandbox_python).parent.parent),
        *(str(Path(t).parent) for t in tool_paths(settings)),
        *settings.sandbox_dependency_caches.values(),
        *settings.sandbox_extra_ro_paths,
    ]
    etc = [m for pattern in settings.sandbox_root_etc for m in sorted(glob.glob(pattern))]
    masked = [
        Path(m).resolve()
        for m in [*settings.sandbox_masked_paths, str(settings.data_dir)]
        if Path(m).exists()
    ]
    dirs: list[str] = []
    files: list[str] = []
    links: list[tuple[str, str]] = []

    def covered(path: Path) -> bool:
        return any(path.is_relative_to(d) for d in map(Path, dirs))

    for p in [*wanted, *etc]:
        src = Path(p)
        if not src.exists():
            continue
        real = src.resolve()
        if real == workdir or any(real.is_relative_to(m) for m in masked):
            raise SandboxError(f"sandbox root would expose masked path {real}")
        if not covered(real):
            (dirs if real.is_dir() else files).append(str(real))
        if src.is_symlink() and str(src) != str(real) and not covered(src.parent.resolve()):
            links.append((str(src), str(real)))
    # Merged-/usr systems: /bin, /lib... are symlinks into /usr.
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        top = Path("/") / name
        if top.is_symlink():
            links.append((str(top), os.readlink(top)))
        elif top.is_dir() and not covered(top):
            dirs.append(str(top))
    dirs = sorted(set(dirs), key=lambda d: (d.count("/"), d))
    return dirs, sorted(set(files)), links


def _address_space(s: Settings, requested: int | None) -> int:
    return min(max(requested or 0, s.sandbox_memory_bytes), s.sandbox_max_address_space_bytes)


class NamespaceSandbox:
    name = "namespace"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def limits(self) -> dict[str, int]:
        s = self.settings
        return {
            "memory_bytes": s.sandbox_memory_bytes,
            "cpu_seconds": s.sandbox_cpu_seconds,
            "max_processes": s.sandbox_max_processes,
            "max_file_bytes": s.sandbox_max_file_bytes,
            "timeout_s": s.sandbox_timeout_s,
            "uid": s.sandbox_uid,
        }

    def prepare_workdir(self, workdir: Path) -> None:
        """Give the sandbox uid ownership of the workspace (its only writable location)."""
        (workdir / ".tmp").mkdir(exist_ok=True)
        for root, dirs, files in os.walk(workdir):
            for name in [*dirs, *files]:
                os.lchown(os.path.join(root, name), self.settings.sandbox_uid, self.settings.sandbox_uid)
        os.chown(workdir, self.settings.sandbox_uid, self.settings.sandbox_uid)
        ensure_traversable(workdir.parent)

    def run(
        self,
        command: list[str],
        workdir: Path,
        *,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        address_space_bytes: int | None = None,
    ) -> SandboxResult:
        s = self.settings
        if os.geteuid() != 0:
            raise SandboxError("the namespace sandbox requires root or CAP_SYS_ADMIN for the worker")
        workdir = workdir.resolve()
        problems = toolchain_problems(s)
        if problems:
            raise SandboxError("refusing to run: sandbox toolchain is not protected: " + "; ".join(problems))
        masks = _masked_paths(s, workdir)
        self.prepare_workdir(workdir)
        timeout = timeout_s or s.sandbox_timeout_s
        with tempfile.TemporaryDirectory(prefix="asw-sbx-ctl-") as ctl:
            ctl_dir = Path(ctl)
            os.chmod(ctl_dir, 0o700)
            inner = ctl_dir / "inner.sh"
            common = {
                "uid": s.sandbox_uid,
                "mem": _address_space(s, address_space_bytes),
                "cpu": s.sandbox_cpu_seconds,
                "nproc": s.sandbox_max_processes,
                "fsize": s.sandbox_max_file_bytes,
            }
            if s.sandbox_isolated_root:
                root = ctl_dir / "root"
                root.mkdir()
                dirs, files, links = root_layout(s, workdir)
                # "$R"'<path>': the root variable, then the shell-quoted absolute path.
                link_cmds = "\n".join(
                    f'mkdir -p "$R"{shlex.quote(str(Path(src).parent))}; '
                    f'ln -sfn {shlex.quote(dst)} "$R"{shlex.quote(src)}'
                    for src, dst in links
                )
                script = INNER_ISOLATED.format(
                    root=shlex.quote(str(root)),
                    ro_dirs=" ".join(shlex.quote(d) for d in dirs) or "''",
                    ro_files=" ".join(shlex.quote(f) for f in files) or "''",
                    links=link_cmds,
                    work=shlex.quote(str(workdir)),
                    pivot_root=_tool_path("pivot_root"),
                    **common,
                )
            else:
                script = INNER.format(
                    masks=" ".join(shlex.quote(m) for m in masks) or "''",
                    scratch=" ".join(shlex.quote(p) for p in _scratch_paths(workdir)) or "''",
                    **common,
                )
            inner.write_text(script)
            inner.chmod(0o700)
            out_path, err_path = ctl_dir / "stdout", ctl_dir / "stderr"
            argv = [
                "unshare",
                "--mount",
                "--net",
                "--pid",
                "--fork",
                "--mount-proc",
                "--propagation",
                "private",
                str(inner),
                *command,
            ]
            start = time.monotonic()
            timed_out = False
            with out_path.open("wb") as out, err_path.open("wb") as err:
                proc = subprocess.Popen(
                    argv,
                    cwd=workdir,
                    env=_base_env(workdir, s, env),
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            return SandboxResult(
                exit_code=proc.returncode,
                stdout=_tail(out_path),
                stderr=_tail(err_path),
                duration_s=round(time.monotonic() - start, 3),
                timed_out=timed_out,
                backend=self.name,
                limits=self.limits(),
                command=command,
            )


class DockerSandbox:
    """Container backend: read-only image root, no network, non-root, all capabilities dropped.

    `SANDBOX_DOCKER_RUNTIME=runsc` runs each container under gVisor, which intercepts system calls
    in a user-space kernel and is the recommended isolation for untrusted customer code on shared
    hosts. The image must contain the toolchains the repositories need.
    """

    name = "docker"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def argv(
        self,
        command: list[str],
        workdir: Path,
        name: str,
        env: dict[str, str] | None,
        address_space_bytes: int | None,
    ) -> list[str]:
        s = self.settings
        uid = s.sandbox_uid
        env_args = [
            a
            for k, v in _base_env(Path("/workspace"), s, env).items()
            if k != "PATH"
            for a in ("-e", f"{k}={v}")
        ]
        runtime = ["--runtime", s.sandbox_docker_runtime] if s.sandbox_docker_runtime else []
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            *runtime,
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{uid}:{uid}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(s.sandbox_max_processes),
            "--memory",
            str(_address_space(s, address_space_bytes)),
            "--memory-swap",
            str(_address_space(s, address_space_bytes)),
            "--cpus",
            "1",
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            f"fsize={s.sandbox_max_file_bytes}:{s.sandbox_max_file_bytes}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",  # noqa: S108 - container-private tmpfs
            "-v",
            f"{workdir.resolve()}:/workspace:rw",
            "-w",
            "/workspace",
            *env_args,
            s.sandbox_docker_image,
            *command,
        ]

    def run(
        self,
        command: list[str],
        workdir: Path,
        *,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        address_space_bytes: int | None = None,
    ) -> SandboxResult:
        import uuid as _uuid

        s = self.settings
        NamespaceSandbox(s).prepare_workdir(workdir.resolve())
        name = f"asw-sbx-{_uuid.uuid4().hex[:12]}"
        argv = self.argv(command, workdir, name, env, address_space_bytes)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout_s or s.sandbox_timeout_s, check=False
            )
            return SandboxResult(
                proc.returncode,
                redact(proc.stdout.decode(errors="replace")[-MAX_CAPTURE:]),
                redact(proc.stderr.decode(errors="replace")[-MAX_CAPTURE:]),
                round(time.monotonic() - start, 3),
                False,
                self.name,
                {
                    "memory_bytes": _address_space(s, address_space_bytes),
                    "max_processes": s.sandbox_max_processes,
                },
                command,
            )
        except subprocess.TimeoutExpired:
            # Killing the docker client does not stop the container: kill it by name.
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30, check=False)
            return SandboxResult(
                -9, "", "timed out", round(time.monotonic() - start, 3), True, self.name, {}, command
            )


class _Logged:
    """Logs every sandboxed command at DEBUG (what ran, how it ended); output stays in the evidence."""

    def __init__(self, inner: SandboxBackend) -> None:
        self.inner = inner
        self.name = inner.name

    def run(
        self,
        command: list[str],
        workdir: Path,
        *,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        address_space_bytes: int | None = None,
    ) -> SandboxResult:
        result = self.inner.run(
            command, workdir, timeout_s=timeout_s, env=env, address_space_bytes=address_space_bytes
        )
        log(
            logger,
            logging.DEBUG,
            "sandbox command",
            backend=result.backend,
            command=redact(shlex.join(command))[:300],
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_s=result.duration_s,
        )
        return result


def get_sandbox(settings: Settings | None = None) -> SandboxBackend:
    s = settings or get_settings()
    if s.sandbox_backend == "namespace":
        return _Logged(NamespaceSandbox(s))
    if s.sandbox_backend == "docker":
        return _Logged(DockerSandbox(s))
    raise SandboxError("no sandbox backend configured; refusing to execute untrusted code")
