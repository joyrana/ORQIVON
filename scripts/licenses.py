"""Third-party licence inventory and policy check for the shipped product.

Collects the runtime dependency closure of the Python package (no dev extras) and the
production npm dependencies of the web app, then:

* ``--check``   fails if any component's licence is not on the allow-list, or is unknown;
* ``--write``   regenerates THIRD_PARTY_NOTICES.md;
* ``--verify``  fails if THIRD_PARTY_NOTICES.md is out of date (used in CI).

Run from the repository root with the project virtualenv's Python, after ``npm ci``.
This is an engineering control, not legal advice: weak-copyleft components are allowed
but listed with the obligation they carry so a reviewer can confirm it is met.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
WEB = ROOT / "apps" / "web"
PY_ROOT_DIST = "agentic-sdlc"

PERMISSIVE = {
    "MIT",
    "ISC",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "0BSD",
    "Unlicense",
    "PSF-2.0",
    "Python-2.0",
    "CC0-1.0",
    "BlueOak-1.0.0",
    "Zlib",
    "CC-BY-4.0",
}
# Allowed, with the obligation shipped products must meet.
WEAK_COPYLEFT = {
    "LGPL-3.0-only": "Dynamically imported, unmodified; users may replace it. Source: upstream URL.",
    "LGPL-3.0-or-later": "Dynamically imported, unmodified; users may replace it. Source: upstream URL.",
    "MPL-2.0": "Unmodified; MPL-covered files available from upstream URL.",
    "EPL-2.0": "Unmodified; source available from upstream URL.",
}

# Normalize the many spellings found in package metadata to SPDX identifiers.
ALIASES = {
    "mit license": "MIT",
    "mit": "MIT",
    "mit*": "MIT",
    "apache software license": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "bsd license": "BSD-3-Clause",
    "bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "isc license (iscl)": "ISC",
    "isc": "ISC",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "lgpl-3.0-only": "LGPL-3.0-only",
    "lgpl-3.0-or-later": "LGPL-3.0-or-later",
    "the unlicense (unlicense)": "Unlicense",
    "unlicense": "Unlicense",
}

# Reviewed manually: package metadata omits the licence but the shipped licence file states it.
OVERRIDES = {
    ("npm", "khroma"): "MIT",  # node_modules/khroma/license: "The MIT License (MIT)"
}


@dataclass(frozen=True)
class Component:
    ecosystem: str
    name: str
    version: str
    license: str  # SPDX expression as normalized
    url: str


def _spdx(raw: str) -> str:
    raw = raw.strip()
    key = raw.lower()
    if key in ALIASES:
        return ALIASES[key]
    if ";" in raw:  # classifier lists: "Apache Software License; MIT License"
        parts = sorted({_spdx(p) for p in raw.split(";") if p.strip()})
        return " OR ".join(parts)
    return raw


Verdict = tuple[bool, str | None]  # (allowed, obligation)


def _allowed(expr: str) -> Verdict:
    """Evaluate an SPDX expression. AND binds tighter than OR; parentheses group.

    OR is allowed if any branch is (a permissive branch is preferred, so no obligation);
    AND is allowed only if every operand is, and carries all their obligations.
    """
    tokens = re.findall(r"\(|\)|[^\s()]+", expr)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def atom() -> Verdict:
        if peek() == "(":
            take()
            verdict = disjunction()
            if peek() != ")":
                raise ValueError(f"unbalanced licence expression: {expr!r}")
            take()
            return verdict
        token = take()
        if token in PERMISSIVE:
            return True, None
        if token in WEAK_COPYLEFT:
            return True, WEAK_COPYLEFT[token]
        return False, None

    def conjunction() -> Verdict:
        parts = [atom()]
        while peek() == "AND":
            take()
            parts.append(atom())
        if not all(ok for ok, _ in parts):
            return False, None
        obligations = [o for _, o in parts if o]
        return True, "; ".join(dict.fromkeys(obligations)) or None

    def disjunction() -> Verdict:
        parts = [conjunction()]
        while peek() == "OR":
            take()
            parts.append(conjunction())
        ok = [p for p in parts if p[0]]
        if not ok:
            return False, None
        return True, next((o for _, o in ok if o is None), ok[0][1])

    try:
        verdict = disjunction()
    except (IndexError, ValueError):
        return False, None
    return verdict if pos == len(tokens) else (False, None)


def _py_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return _spdx(expr)
    classifiers = [
        c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")
    ]
    classifiers = [c for c in classifiers if c not in {"OSI Approved"}]
    if classifiers:
        return _spdx("; ".join(classifiers))
    raw = (meta.get("License") or "").strip()
    if raw and len(raw) < 80:
        return _spdx(raw)
    return "UNKNOWN"


def python_components() -> list[Component]:
    seen: dict[str, Component] = {}
    queue = [PY_ROOT_DIST]
    while queue:
        name = queue.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        dist = metadata.distribution(name)
        if key != PY_ROOT_DIST:
            url = dist.metadata.get("Home-page") or ""
            for entry in dist.metadata.get_all("Project-URL") or []:
                label, _, link = entry.partition(",")
                if not url or label.strip().lower() in {"source", "homepage", "repository"}:
                    url = link.strip()
            seen[key] = Component("python", dist.metadata["Name"], dist.version, _py_license(dist), url)
        else:
            seen[key] = Component("python", PY_ROOT_DIST, dist.version, "self", "")
        for req_str in dist.requires or []:
            req = Requirement(req_str)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            try:
                metadata.distribution(req.name)
            except metadata.PackageNotFoundError:
                continue  # platform-specific dependency not installed here
            queue.append(req.name)
    return sorted((c for c in seen.values() if c.license != "self"), key=lambda c: c.name.lower())


def npm_components() -> list[Component]:
    out = subprocess.run(
        ["npm", "ls", "--omit=dev", "--all", "--json", "--long"],
        cwd=WEB,
        capture_output=True,
        text=True,
        check=False,
    )
    tree = json.loads(out.stdout or "{}")
    found: dict[tuple[str, str], Component] = {}

    def walk(deps: dict[str, dict]) -> None:
        for name, info in (deps or {}).items():
            version = info.get("version", "")
            if (name, version) in found or not version:
                continue
            pkg_json = Path(info.get("path", "")) / "package.json"
            lic = "UNKNOWN"
            url = ""
            if pkg_json.is_file():
                data = json.loads(pkg_json.read_text())
                raw = data.get("license")
                if isinstance(raw, dict):
                    raw = raw.get("type")
                if not raw and isinstance(data.get("licenses"), list):
                    raw = " OR ".join(x.get("type", "") for x in data["licenses"])
                lic = _spdx(raw) if raw else OVERRIDES.get(("npm", name), "UNKNOWN")
                repo = data.get("repository")
                url = repo.get("url", "") if isinstance(repo, dict) else (repo or data.get("homepage", ""))
            found[(name, version)] = Component("npm", name, version, lic, str(url))
            walk(info.get("dependencies", {}))

    walk(tree.get("dependencies", {}))
    return sorted(found.values(), key=lambda c: (c.name.lower(), c.version))


def render(components: list[Component]) -> str:
    lines = [
        "# Third-Party Notices",
        "",
        "Agentic SDLC Workspace is proprietary software (see [LICENSE](LICENSE)). It ships with the",
        "open-source components below, each licensed to you under its own terms. This file is",
        "generated by `python scripts/licenses.py --write`; CI fails if it is out of date or if a",
        "component's licence is not on the allow-list in that script.",
        "",
        "Components with an obligation column are weak-copyleft. They are used unmodified, and",
        "their source is available at the linked upstream location.",
        "",
    ]
    for eco, title in (("python", "Python runtime dependencies"), ("npm", "Web application (bundled)")):
        rows = [c for c in components if c.ecosystem == eco]
        lines += [
            f"## {title} ({len(rows)})",
            "",
            "| Component | Version | Licence | Obligation |",
            "|---|---|---|---|",
        ]
        for c in rows:
            _, obligation = _allowed(c.license)
            name = f"[{c.name}]({_clean_url(c.url)})" if _clean_url(c.url) else c.name
            lines.append(f"| {name} | {c.version} | {c.license} | {obligation or ''} |")
        lines.append("")
    return "\n".join(lines)


def _clean_url(url: str) -> str:
    url = url.removeprefix("git+").removesuffix(".git")
    if url.startswith("git://"):
        url = "https://" + url[len("git://") :]
    if url.startswith("github:"):
        url = "https://github.com/" + url[len("github:") :]
    return url if url.startswith("http") else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    components = python_components() + npm_components()
    denied = [c for c in components if not _allowed(c.license)[0]]
    if denied:
        for c in denied:
            print(f"DENIED {c.ecosystem} {c.name}@{c.version}: {c.license}", file=sys.stderr)
        return 1
    text = render(components)
    if args.write:
        NOTICES.write_text(text)
        print(f"wrote {NOTICES.relative_to(ROOT)} ({len(components)} components)")
    elif args.verify:
        if not NOTICES.is_file() or NOTICES.read_text() != text:
            print(
                "THIRD_PARTY_NOTICES.md is out of date: run python scripts/licenses.py --write",
                file=sys.stderr,
            )
            return 1
        print(f"notices up to date ({len(components)} components)")
    else:
        print(f"all {len(components)} components allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
