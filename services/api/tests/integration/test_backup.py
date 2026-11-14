"""Backup and restore round-trip a real installation: database rows and stored files."""

from __future__ import annotations

import json
import shutil
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.ops.backup import BackupError, create_backup, libpq, restore_backup, verify_backup
from tests.conftest import H

pytestmark = pytest.mark.skipif(not shutil.which("pg_dump"), reason="PostgreSQL client tools missing")


def _counts(url: str) -> dict[str, int]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                t: int(conn.scalar(text(f'SELECT count(*) FROM "{t}"')) or 0)  # noqa: S608 - table names
                for t in sorted(inspect(conn).get_table_names())
            }
    finally:
        engine.dispose()


@pytest.fixture
def fresh_db() -> Iterator[str]:
    src = make_url(get_settings().database_url)
    name = "asw_restore_test"
    admin = create_engine(src.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield src.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_round_trip_into_a_fresh_installation(
    client: TestClient, project: dict, tmp_path: Path, fresh_db: str
) -> None:
    # Some real content: a project with a document stored in the data directory.
    data = tmp_path / "data"
    (data / "objects" / "ab").mkdir(parents=True)
    (data / "objects" / "ab" / "abcdef").write_bytes(b"uploaded document bytes")
    (data / "repositories" / "r1.git").mkdir(parents=True)
    (data / "repositories" / "r1.git" / "HEAD").write_text("ref: refs/heads/main\n")
    (data / ".ready-probe").write_text("x")
    assert client.get(f"/api/v1/projects/{project['id']}", headers=H("owner")).status_code == 200

    archive = create_backup(tmp_path / "out", data_dir=data)
    assert archive.stat().st_mode & 0o077 == 0  # backups hold everything: owner-only
    manifest = verify_backup(archive)
    assert manifest["files"] == 2 and manifest["schema_revision"]

    target = tmp_path / "restored"
    restore_backup(archive, database_url=fresh_db, data_dir=target)
    assert _counts(fresh_db) == _counts(get_settings().database_url)
    assert (target / "objects" / "ab" / "abcdef").read_bytes() == b"uploaded document bytes"
    assert (target / "repositories" / "r1.git" / "HEAD").is_file()
    assert not (target / ".ready-probe").exists()

    # A second restore over a live installation needs --force, and then replaces it entirely.
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(archive, database_url=fresh_db, data_dir=target)
    (target / "stale.txt").write_text("left over")
    engine = create_engine(fresh_db)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE stale_table (id int)"))
    engine.dispose()
    restore_backup(archive, database_url=fresh_db, data_dir=target, force=True)
    assert not (target / "stale.txt").exists()
    assert "stale_table" not in _counts(fresh_db)


def test_damaged_or_foreign_backups_are_refused(tmp_path: Path, fresh_db: str) -> None:
    archive = create_backup(tmp_path / "out", data_dir=tmp_path / "empty")
    # Tampered: same names, different database dump.
    work = tmp_path / "x"
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    (work / "database.dump").write_bytes(b"not a dump")
    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as tar:
        for n in ("manifest.json", "database.dump", "data.tar.gz"):
            tar.add(work / n, arcname=n)
    with pytest.raises(BackupError, match="checksum"):
        restore_backup(bad, database_url=fresh_db, data_dir=tmp_path / "t")
    assert _counts(fresh_db) == {}  # nothing was touched

    # From a newer release: the schema revision is unknown to this code.
    manifest = json.loads((work / "manifest.json").read_text())
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    manifest["schema_revision"] = "9999_future"
    (work / "manifest.json").write_text(json.dumps(manifest))
    newer = tmp_path / "newer.tar"
    with tarfile.open(newer, "w") as tar:
        for n in ("manifest.json", "database.dump", "data.tar.gz"):
            tar.add(work / n, arcname=n)
    with pytest.raises(BackupError, match="newer than this version"):
        restore_backup(newer, database_url=fresh_db, data_dir=tmp_path / "t")

    other = tmp_path / "other.tar"
    with tarfile.open(other, "w") as tar:
        tar.add(work / "manifest.json", arcname="manifest.json")
    with pytest.raises(BackupError, match="not a backup archive"):
        verify_backup(other)


def test_password_is_not_on_the_command_line() -> None:
    pq = libpq("postgresql+psycopg://svc:s3cret@db.internal:6543/asw?sslmode=require")
    assert "s3cret" not in " ".join(pq.args)
    assert pq.env["PGPASSWORD"] == "s3cret" and pq.env["PGSSLMODE"] == "require"
    assert pq.args == ["--dbname", "asw", "--host", "db.internal", "--port", "6543", "--username", "svc"]
