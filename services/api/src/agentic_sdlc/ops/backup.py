"""Consistent backup and restore of a self-hosted installation.

A backup is one tar file holding:

* ``database.dump`` - ``pg_dump --format=custom`` of the application database;
* ``data.tar.gz``   - the data directory (uploaded documents, local repositories and mirrors,
  release staging artifacts);
* ``manifest.json`` - format version, app version, schema revision, creation time and the
  SHA-256 of both parts, so a damaged or tampered backup is refused before anything is touched.

The database is dumped first and the files are copied after it: stored objects are only ever
added, so every object the database references is in the archive (files written in between are
unreferenced and harmless). Restore refuses a non-empty target unless ``force`` is set, refuses a
backup from a newer schema than the code knows, and leaves the schema to ``alembic upgrade head``.

Credentials never appear on a command line: the password goes to pg_dump through PGPASSWORD.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from agentic_sdlc.core.db import utcnow
from agentic_sdlc.core.settings import get_settings

FORMAT = 1
SKIP = {".ready-probe"}


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Libpq:
    args: list[str]
    env: dict[str, str]


def libpq(database_url: str) -> Libpq:
    """Connection arguments for pg_dump/pg_restore, with the password kept out of argv."""
    url = make_url(database_url)
    env = {**os.environ}
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    args = ["--dbname", url.database or ""]
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    sslmode = url.query.get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = str(sslmode)
    return Libpq(args, env)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise BackupError(f"{name} is not installed (install the PostgreSQL client tools)")
    return found


def _run(argv: list[str], env: dict[str, str]) -> None:
    p = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise BackupError(f"{Path(argv[0]).name} failed: {p.stderr.strip()[-2000:]}")


def _schema_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            return conn.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def _code_revisions() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(get_settings().migrations_dir))
    return {s.revision for s in ScriptDirectory.from_config(cfg).walk_revisions()}


def create_backup(out_dir: Path, *, database_url: str | None = None, data_dir: Path | None = None) -> Path:
    settings = get_settings()
    database_url = database_url or settings.database_url
    data_dir = data_dir or settings.data_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"asw-backup-{stamp}.tar"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        work = Path(tmp)
        pq = libpq(database_url)
        revision = _schema_revision(database_url)
        dump = work / "database.dump"
        _run(
            [_tool("pg_dump"), "--format=custom", "--no-owner", "--no-acl", "--file", str(dump), *pq.args],
            pq.env,
        )
        files = work / "data.tar.gz"
        count = 0
        with tarfile.open(files, "w:gz") as tar:
            if data_dir.is_dir():
                for path in sorted(data_dir.rglob("*")):
                    rel = path.relative_to(data_dir)
                    if rel.parts[0] in SKIP or path.is_symlink() or not (path.is_file() or path.is_dir()):
                        continue
                    tar.add(path, arcname=str(rel), recursive=False)
                    count += path.is_file()
        manifest: dict[str, Any] = {
            "format": FORMAT,
            "app_version": metadata.version("agentic-sdlc"),
            "schema_revision": revision,
            "created_at": utcnow().isoformat(),
            "files": count,
            "sha256": {"database.dump": _sha256(dump), "data.tar.gz": _sha256(files)},
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        partial = target.with_suffix(".tar.partial")
        with tarfile.open(partial, "w") as tar:
            for name in ("manifest.json", "database.dump", "data.tar.gz"):
                tar.add(work / name, arcname=name)
        partial.rename(target)  # a backup that exists is complete
    target.chmod(0o600)
    return target


def read_manifest(archive: Path, into: Path) -> dict[str, Any]:
    """Extract and verify a backup; returns its manifest. Nothing is restored."""
    with tarfile.open(archive, "r") as tar:
        names = set(tar.getnames())
        if names != {"manifest.json", "database.dump", "data.tar.gz"}:
            raise BackupError(f"not a backup archive (contains {sorted(names)})")
        tar.extractall(into, filter="data")
    manifest: dict[str, Any] = json.loads((into / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')!r}")
    for name, digest in manifest["sha256"].items():
        if _sha256(into / name) != digest:
            raise BackupError(f"{name} does not match its checksum: the backup is damaged or altered")
    return manifest


def verify_backup(archive: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = read_manifest(archive, Path(tmp))
        _run([_tool("pg_restore"), "--list", str(Path(tmp) / "database.dump")], dict(os.environ))
        with tarfile.open(Path(tmp) / "data.tar.gz") as tar:
            files = sum(1 for m in tar.getmembers() if m.isfile())
        if files != manifest["files"]:
            raise BackupError(f"data archive holds {files} files, manifest says {manifest['files']}")
    return manifest


def restore_backup(
    archive: Path, *, force: bool = False, database_url: str | None = None, data_dir: Path | None = None
) -> dict[str, Any]:
    settings = get_settings()
    database_url = database_url or settings.database_url
    data_dir = data_dir or settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        manifest = read_manifest(archive, work)
        revision = manifest.get("schema_revision")
        if revision and revision not in _code_revisions():
            raise BackupError(
                f"the backup's schema revision {revision} is newer than this version of the code: "
                "restore with the release that made the backup, then upgrade"
            )
        engine = create_engine(database_url)
        try:
            with engine.connect() as conn:
                tables = inspect(conn).get_table_names()
        finally:
            engine.dispose()
        has_files = data_dir.is_dir() and any(p.name not in SKIP for p in data_dir.iterdir())
        if (tables or has_files) and not force:
            raise BackupError(
                f"the target is not empty ({len(tables)} tables, data directory "
                f"{'has files' if has_files else 'empty'}): restore into a fresh installation, "
                "or pass --force to replace it"
            )
        pq = libpq(database_url)
        if tables:
            # --clean only drops objects present in the dump; drop the schema so nothing stale stays.
            engine = create_engine(database_url)
            try:
                with engine.begin() as conn:
                    conn.execute(text("DROP SCHEMA public CASCADE"))
                    conn.execute(text("CREATE SCHEMA public"))
            finally:
                engine.dispose()
        _run(
            [
                _tool("pg_restore"),
                "--no-owner",
                "--no-acl",
                "--single-transaction",
                "--exit-on-error",
                *pq.args,
                str(work / "database.dump"),
            ],
            pq.env,
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        if has_files:
            for child in data_dir.iterdir():
                if child.name in SKIP:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        with tarfile.open(work / "data.tar.gz") as tar:
            tar.extractall(data_dir, filter="data")
    return manifest
