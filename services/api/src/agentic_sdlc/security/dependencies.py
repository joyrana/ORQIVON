"""Dependency advisories: which packages a repository pins, and which of those have known
vulnerabilities, from an OSV database the installation mirrors or from an OSV API.

Self-hosted installations usually cannot (or must not) call out with their dependency list, so
the primary source is an offline directory of OSV records (the per-ecosystem exports at
https://osv-vulnerabilities.storage.googleapis.com/<ecosystem>/all.zip, unzipped and synced on a
schedule). An OSV-compatible API is optional and sends only package names and versions.

Only exact versions are checked (lock files, `==` pins, go.mod, Maven/NuGet literals): a range
in a manifest does not say what is installed, and guessing would make the gate noisy.
"""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx

from agentic_sdlc.security.findings import Finding, Severity


@dataclass(frozen=True)
class Package:
    ecosystem: str  # OSV ecosystem name: PyPI, npm, Go, Maven, NuGet
    name: str
    version: str
    path: str  # manifest it was read from
    line: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ecosystem, self.name, self.version)


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _line_of(text: str, needle: str) -> int | None:
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else None


def parse_manifest(path: str, text: str) -> list[Package]:
    name = path.rsplit("/", 1)[-1]
    out: list[Package] = []
    try:
        if re.fullmatch(r"requirements[\w.-]*\.txt|constraints[\w.-]*\.txt", name):
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(
                    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*===?\s*([A-Za-z0-9.+!-]+)", line
                )
                if m:
                    out.append(Package("PyPI", _pep503(m.group(1)), m.group(2), path, i))
        elif name in {"poetry.lock", "uv.lock", "pdm.lock"}:
            for pkg in tomllib.loads(text).get("package", []):
                if pkg.get("name") and pkg.get("version"):
                    n = _pep503(pkg["name"])
                    out.append(
                        Package("PyPI", n, str(pkg["version"]), path, _line_of(text, f'"{pkg["name"]}"'))
                    )
        elif name == "pyproject.toml":
            deps = tomllib.loads(text).get("project", {}).get("dependencies", [])
            for d in deps:
                m = re.match(
                    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9.+!-]+)\s*$", d
                )
                if m:
                    out.append(Package("PyPI", _pep503(m.group(1)), m.group(2), path, _line_of(text, d)))
        elif name == "package-lock.json":
            data = json.loads(text)
            for key, info in (data.get("packages") or {}).items():
                if not key or not isinstance(info, dict) or not info.get("version") or info.get("link"):
                    continue
                pkg_name = info.get("name") or key.rsplit("node_modules/", 1)[-1]
                out.append(Package("npm", pkg_name, info["version"], path, _line_of(text, f'"{key}"')))
        elif name == "package.json":
            data = json.loads(text)
            for section in ("dependencies", "optionalDependencies"):
                for n, v in (data.get(section) or {}).items():
                    if isinstance(v, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", v):
                        out.append(Package("npm", n, v, path, _line_of(text, f'"{n}"')))
        elif name == "go.mod":
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]{2,}/\S+)\s+(v[\w.+-]+)", line)
                if m and not line.strip().startswith(("module", "//", "replace", "exclude")):
                    out.append(Package("Go", m.group(1), m.group(2), path, i))
        elif name == "pom.xml":
            root = ET.fromstring(text)  # noqa: S314
            ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
            q = "m:" if ns else ""
            for dep in root.iter(f"{{{ns['m']}}}dependency" if ns else "dependency"):
                g, a, v = (
                    dep.findtext(f"{q}{t}", namespaces=ns) for t in ("groupId", "artifactId", "version")
                )
                if g and a and v and "${" not in v:
                    out.append(
                        Package("Maven", f"{g}:{a}", v.strip(), path, _line_of(text, f"<artifactId>{a}<"))
                    )
        elif name.endswith((".csproj", ".fsproj", ".vbproj")) or name == "Directory.Packages.props":
            for m in re.finditer(
                r"<Package(?:Reference|Version)\s+Include=\"([^\"]+)\"\s+Version=\"([^\"$]+)\"", text
            ):
                out.append(Package("NuGet", m.group(1), m.group(2), path, _line_of(text, m.group(0))))
    except (ValueError, ET.ParseError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return []  # an unparsable manifest is the build's problem, not a security finding
    return out


MANIFEST = re.compile(
    r"(?:^|/)(?:requirements[\w.-]*\.txt|constraints[\w.-]*\.txt|poetry\.lock|uv\.lock|pdm\.lock|"
    r"pyproject\.toml|package-lock\.json|package\.json|go\.mod|pom\.xml|[^/]+\.(?:cs|fs|vb)proj|"
    r"Directory\.Packages\.props)$"
)


def is_manifest(path: str) -> bool:
    return "node_modules/" not in path and bool(MANIFEST.search(path))


# ---- version comparison -------------------------------------------------------------------------


def _generic_key(v: str) -> tuple[Any, ...]:
    v = v.strip().removeprefix("v")
    main, _, pre = v.partition("-")
    main = main.split("+", 1)[0]
    parts = [int(p) if p.isdigit() else p for p in re.split(r"[.]", main)]
    num = tuple((0, p) if isinstance(p, int) else (1, p) for p in parts)
    pre_parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.]", pre)) if pre else ()
    return (num, 0 if pre else 1, pre_parts)


def version_key(ecosystem: str, v: str) -> Any:
    if ecosystem == "PyPI":
        from packaging.version import InvalidVersion, Version

        try:
            return (0, Version(v))
        except InvalidVersion:
            return (1, _generic_key(v))
    return _generic_key(v)


def affected(record: dict[str, Any], pkg: Package) -> tuple[bool, list[str]]:
    """Whether `pkg` is affected by an OSV record, and the versions that fix it."""
    hit = False
    fixed: list[str] = []
    for a in record.get("affected") or []:
        p = a.get("package") or {}
        if (
            p.get("ecosystem", "").split(":")[0] != pkg.ecosystem
            or _norm(pkg.ecosystem, p.get("name", "")) != pkg.name
        ):
            continue
        if pkg.version in (a.get("versions") or []):
            hit = True
        for r in a.get("ranges") or []:
            if r.get("type") not in {"ECOSYSTEM", "SEMVER"}:
                continue
            v = version_key(pkg.ecosystem, pkg.version)
            start: Any = None
            for ev in r.get("events") or []:
                if "introduced" in ev:
                    intro = ev["introduced"]
                    start = (None,) if intro in {"0", ""} else version_key(pkg.ecosystem, intro)
                elif start is not None and ("fixed" in ev or "last_affected" in ev):
                    end_raw = ev.get("fixed", ev.get("last_affected"))
                    end = version_key(pkg.ecosystem, end_raw)
                    lower_ok = start == (None,) or _ge(v, start)
                    upper_ok = _lt(v, end) if "fixed" in ev else not _lt(end, v)
                    if lower_ok and upper_ok:
                        hit = True
                    if "fixed" in ev:
                        fixed.append(end_raw)
                    start = None
            if start is not None and (start == (None,) or _ge(v, start)):
                hit = True  # introduced with no fix yet
    return hit, fixed


def _lt(a: Any, b: Any) -> bool:
    try:
        return bool(a < b)
    except TypeError:
        return str(a) < str(b)


def _ge(a: Any, b: Any) -> bool:
    return not _lt(a, b)


def _norm(ecosystem: str, name: str) -> str:
    return _pep503(name) if ecosystem == "PyPI" else name


SEVERITY_WORDS: dict[str, Severity] = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MODERATE": "medium",
    "MEDIUM": "medium",
    "LOW": "low",
}


def record_severity(record: dict[str, Any]) -> tuple[Severity, bool]:
    """(severity, known). Unknown severities are treated as high: a gate should not wave them through."""
    for src in (
        record.get("database_specific") or {},
        *[a.get("ecosystem_specific") or {} for a in record.get("affected") or []],
    ):
        word = str(src.get("severity", "")).upper()
        if word in SEVERITY_WORDS:
            return SEVERITY_WORDS[word], True
    return "high", False


# ---- advisory sources ---------------------------------------------------------------------------


class AdvisorySource(Protocol):
    name: str

    def lookup(self, packages: list[Package]) -> dict[tuple[str, str, str], list[dict[str, Any]]]: ...


class OfflineDatabase:
    """A directory of OSV JSON records (any layout; read recursively, indexed once per change)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.name = f"osv-dir:{root}"

    def lookup(self, packages: list[Package]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        index = _load_index(str(self.root), _dir_stamp(self.root))
        out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for pkg in packages:
            hits = [r for r in index.get((pkg.ecosystem, pkg.name), []) if affected(r, pkg)[0]]
            if hits:
                out[pkg.key] = hits
        return out


def _dir_stamp(root: Path) -> tuple[int, float]:
    files = list(root.rglob("*.json"))
    return len(files), max((f.stat().st_mtime for f in files), default=0.0)


@lru_cache(maxsize=4)
def _load_index(root: str, stamp: tuple[int, float]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in Path(root).rglob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("withdrawn"):
            continue
        for a in rec.get("affected") or []:
            p = a.get("package") or {}
            eco = str(p.get("ecosystem", "")).split(":")[0]
            index.setdefault((eco, _norm(eco, str(p.get("name", "")))), []).append(rec)
    return index


class OsvApi:
    """An OSV-compatible API (api.osv.dev or a self-hosted mirror). Sends names and versions only."""

    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.name = f"osv-api:{self.base_url}"

    def lookup(self, packages: list[Package]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        uniq = list({p.key: p for p in packages}.values())
        with httpx.Client(timeout=self.timeout_s, follow_redirects=False) as client:
            for i in range(0, len(uniq), 500):
                chunk = uniq[i : i + 500]
                r = client.post(
                    f"{self.base_url}/v1/querybatch",
                    json={
                        "queries": [
                            {"package": {"ecosystem": p.ecosystem, "name": p.name}, "version": p.version}
                            for p in chunk
                        ]
                    },
                )
                r.raise_for_status()
                for pkg, res in zip(chunk, r.json().get("results", []), strict=False):
                    ids = [v["id"] for v in res.get("vulns") or []]
                    records = []
                    for vid in ids:
                        d = client.get(f"{self.base_url}/v1/vulns/{vid}")
                        d.raise_for_status()
                        records.append(d.json())
                    if records:
                        out[pkg.key] = records
        return out


def findings_for(
    packages: list[Package], base_keys: set[tuple[str, str, str]], source: AdvisorySource
) -> list[Finding]:
    out: list[Finding] = []
    for key, records in source.lookup(packages).items():
        pkg = next(p for p in packages if p.key == key)
        for rec in records:
            sev, known = record_severity(rec)
            _, fixed = affected(rec, pkg)
            aliases = [a for a in rec.get("aliases") or [] if a.startswith("CVE-")]
            fix = (
                f" Upgrade to {', '.join(sorted(set(fixed)))} or later."
                if fixed
                else " No fixed version is published."
            )
            out.append(
                Finding(
                    scanner="dependencies",
                    rule_id=str(rec.get("id")),
                    severity=sev,
                    title=f"{pkg.name} {pkg.version}: {str(rec.get('summary') or rec.get('id'))[:160]}",
                    path=pkg.path,
                    line=pkg.line,
                    message=(
                        f"{pkg.ecosystem} package {pkg.name} {pkg.version} is affected by {rec.get('id')}"
                        f"{' (' + ', '.join(aliases) + ')' if aliases else ''}."
                        f"{'' if known else ' Severity not published; treated as high.'}{fix}"
                    ),
                    excerpt=f"{pkg.name}=={pkg.version}",
                    introduced=pkg.key not in base_keys,
                    extra={
                        "match_key": f"{pkg.ecosystem}:{pkg.name}:{pkg.version}:{rec.get('id')}",
                        "ecosystem": pkg.ecosystem,
                        "package": pkg.name,
                        "version": pkg.version,
                        "fixed": sorted(set(fixed)),
                        "aliases": aliases,
                    },
                )
            )
    return out
