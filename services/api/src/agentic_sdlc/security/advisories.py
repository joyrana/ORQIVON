"""Mirror OSV advisories for offline dependency scanning.

Downloads each ecosystem's export (`<source>/<ecosystem>/all.zip`), keeps only the JSON records,
and swaps the ecosystem's directory in atomically, so a scan never sees a half-written mirror.
Run it on a schedule from somewhere that may reach the source (the Helm chart has a CronJob).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import httpx

DEFAULT_SOURCE = "https://osv-vulnerabilities.storage.googleapis.com"
DEFAULT_ECOSYSTEMS = ("PyPI", "npm", "Go", "Maven", "NuGet")
MAX_ZIP_BYTES = 2_000_000_000
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}\.json$")


def sync(
    dest: Path, ecosystems: tuple[str, ...] = DEFAULT_ECOSYSTEMS, source: str = DEFAULT_SOURCE
) -> dict[str, int]:
    dest.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        for eco in ecosystems:
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,40}", eco):
                raise ValueError(f"invalid ecosystem name {eco!r}")
            with client.stream("GET", f"{source.rstrip('/')}/{eco}/all.zip") as r:
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_bytes():
                    buf.write(chunk)
                    if buf.tell() > MAX_ZIP_BYTES:
                        raise ValueError(f"{eco}: export larger than {MAX_ZIP_BYTES} bytes")
            staging = dest / f".staging-{eco}-{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            n = 0
            with zipfile.ZipFile(buf) as zf:
                for info in zf.infolist():
                    name = Path(info.filename).name
                    if info.is_dir() or not SAFE_NAME.match(name):
                        continue
                    data = zf.read(info)
                    json.loads(data)  # refuse a corrupt record rather than mirror it
                    (staging / name).write_bytes(data)
                    n += 1
            if n == 0:
                shutil.rmtree(staging)
                raise ValueError(f"{eco}: the export contained no advisories; keeping the previous mirror")
            target = dest / eco
            old = dest / f".old-{eco}-{os.getpid()}"
            if target.exists():
                target.rename(old)
            staging.rename(target)
            shutil.rmtree(old, ignore_errors=True)
            counts[eco] = n
    return counts
