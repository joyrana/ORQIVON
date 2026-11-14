"""Repository security scanning: secrets, code patterns, dependency advisories, SARIF tools.

Credential-shaped strings are assembled at runtime so this file does not itself contain
anything a secret scanner (ours or the git host's) would flag.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.security import code_rules, dependencies, sarif, secrets
from agentic_sdlc.security.findings import Finding, SecurityPolicy
from agentic_sdlc.security.scan import added_lines, scan_change, validate_tools

AWS_ID = "AKIA" + "Z7Q2" * 4
GH_TOKEN = "ghp" + "_" + "x9K2mPq7" * 5
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
STRIPE = "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
GENERIC = "Zx81kQ!vB2" + "mN9#pL4r"


def rules(found: list[Finding]) -> list[str]:
    return sorted(f.rule_id for f in found)


# ---- secrets ------------------------------------------------------------------------------------


def test_provider_tokens_are_found_and_redacted() -> None:
    text = "\n".join(
        [
            f'AWS_ACCESS_KEY_ID = "{AWS_ID}"',
            f"token: {GH_TOKEN}",
            PEM,
            f"stripe = '{STRIPE}'",
            f'db = "postgresql://svc:{GENERIC}@db.internal/app"',
            f'client_secret = "{GENERIC}"',
        ]
    )
    found = secrets.scan_text("app/config.py", text, added_lines={1, 2, 3, 4, 5, 6})
    assert rules(found) == [
        "aws-access-key-id",
        "connection-string-password",
        "generic-credential",
        "github-token",
        "private-key",
        "stripe-live-key",
    ]
    for f in found:
        assert f.introduced
        if f.rule_id != "private-key":
            assert f.excerpt and AWS_ID not in f.excerpt and GH_TOKEN not in f.excerpt
            assert GENERIC not in f.excerpt and STRIPE not in f.excerpt
    assert {f.severity for f in found if f.rule_id == "aws-access-key-id"} == {"critical"}


@pytest.mark.parametrize(
    "line",
    [
        'password = "changeme123"',
        'api_key = os.environ["API_KEY"]',
        'token = "${GITHUB_TOKEN}"',
        "password: '<your password here>'",
        'secret = "aaaaaaaaaaaa"',
        'password = "{{ vault_password }}"',
        "DATABASE_URL=postgresql://sdlc:${DB_PASSWORD}@db/app",
        'token_url = "https://login.example.com/oauth2/token"',
        "private_key = load_private_key_from(path)",
        f'client_secret = "{GENERIC}"  # asw:allow-secret documented test vector',
        'password_field = "password_confirmation"',
    ],
)
def test_placeholders_and_references_are_not_secrets(line: str) -> None:
    assert secrets.scan_text("app/settings.py", line) == []


def test_secrets_in_test_fixtures_are_low_severity_but_keys_stay_critical() -> None:
    text = f'client_secret = "{GENERIC}"\nkey = "{AWS_ID}"\n'
    found = {f.rule_id: f.severity for f in secrets.scan_text("tests/fixtures/conf.py", text)}
    assert found == {"generic-credential": "low", "aws-access-key-id": "critical"}


def test_lock_files_and_binaries_are_skipped() -> None:
    assert secrets.scan_text("package-lock.json", f'"integrity": "{GENERIC}" "{AWS_ID}"') == []
    assert secrets.scan_text("img.bin", "\x00" + AWS_ID) == []


# ---- code patterns ------------------------------------------------------------------------------


def test_code_rules_flag_dangerous_calls_not_safe_ones() -> None:
    bad = "\n".join(
        [
            "result = eval(user_input)",
            "subprocess.run(cmd, shell=True)",
            "data = yaml.load(stream)",
            'cur.execute(f"SELECT * FROM t WHERE id = {tid}")',
            "requests.get(url, verify=False)",
            "obj = pickle.loads(blob)",
        ]
    )
    found = code_rules.scan_text("svc/handler.py", bad, {1, 2, 3, 4, 5, 6})
    assert rules(found) == [
        "py-eval",
        "py-pickle",
        "py-shell-true",
        "py-sql-format",
        "py-tls-verify-off",
        "py-yaml-load",
    ]
    good = "\n".join(
        [
            "# eval(user_input) is not used here",
            "data = yaml.safe_load(stream)",
            "data = yaml.load(stream, Loader=yaml.SafeLoader)",
            'cur.execute("SELECT * FROM t WHERE id = %s", (tid,))',
            "subprocess.run(['ls', path], check=True)",
            "value = literal_eval(text)",
            "self.evaluate(x)",
            "digest = hashlib.md5(data, usedforsecurity=False)",
        ]
    )
    assert code_rules.scan_text("svc/handler.py", good) == []


def test_code_rules_cover_other_languages() -> None:
    cases = {
        "web/app.ts": ("el.innerHTML = html;", "js-inner-html"),
        "web/run.js": ("exec(`git log ${ref}`)", "js-child-process"),
        "cmd/main.go": ("cfg := &tls.Config{InsecureSkipVerify: true}", "go-tls-off"),
        "db/repo.go": (
            'rows, err := db.Query(fmt.Sprintf("SELECT * FROM t WHERE id=%s", id))',
            "go-sql-format",
        ),
        "src/A.java": ('stmt.executeQuery("SELECT * FROM t WHERE id=" + id);', "java-sql-concat"),
        "src/B.cs": ("var f = new BinaryFormatter();", "cs-binaryformatter"),
    }
    for path, (line, rule) in cases.items():
        assert rules(code_rules.scan_text(path, line)) == [rule], path
    assert code_rules.scan_text("README.md", "eval(x)") == []


# ---- dependencies -------------------------------------------------------------------------------

ADVISORY = {
    "id": "GHSA-test-0001",
    "summary": "Remote code execution in jinja-like templating",
    "aliases": ["CVE-2099-0001"],
    "database_specific": {"severity": "HIGH"},
    "affected": [
        {
            "package": {"ecosystem": "PyPI", "name": "Jinja_Like"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.11.3"}]}],
        }
    ],
}


def _pkg(eco: str, name: str, v: str) -> dependencies.Package:
    return dependencies.Package(eco, name, v, "requirements.txt", 1)


def test_osv_ranges_versions_and_last_affected() -> None:
    assert dependencies.affected(ADVISORY, _pkg("PyPI", "jinja-like", "2.10.1")) == (True, ["2.11.3"])
    assert dependencies.affected(ADVISORY, _pkg("PyPI", "jinja-like", "2.11.3"))[0] is False
    assert dependencies.affected(ADVISORY, _pkg("npm", "jinja-like", "1.0.0"))[0] is False
    rec = {
        "id": "X",
        "affected": [
            {
                "package": {"ecosystem": "npm", "name": "left-pad"},
                "ranges": [
                    {"type": "SEMVER", "events": [{"introduced": "1.2.0"}, {"last_affected": "1.3.0"}]},
                    {"type": "SEMVER", "events": [{"introduced": "2.0.0-beta.1"}]},
                ],
                "versions": ["0.9.9"],
            }
        ],
    }
    for version, hit in [
        ("1.1.9", False),
        ("1.2.0", True),
        ("1.3.0", True),
        ("1.3.1", False),
        ("0.9.9", True),
        ("2.0.0-alpha", False),
        ("2.0.0", True),
        ("3.1.4", True),
    ]:
        assert dependencies.affected(rec, _pkg("npm", "left-pad", version))[0] is hit, version
    go = {
        "id": "GO-1",
        "affected": [
            {
                "package": {"ecosystem": "Go", "name": "golang.org/x/net"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "0.17.0"}]}],
            }
        ],
    }
    assert dependencies.affected(go, _pkg("Go", "golang.org/x/net", "v0.10.0"))[0]
    assert not dependencies.affected(go, _pkg("Go", "golang.org/x/net", "v0.17.0"))[0]
    assert dependencies.record_severity({"id": "Y"}) == ("high", False)


def test_manifests_are_parsed_to_exact_versions() -> None:
    parse = dependencies.parse_manifest
    assert [p.key for p in parse("requirements.txt", "Jinja_Like==2.10.1\nrequests>=2\n# x==1\n")] == [
        ("PyPI", "jinja-like", "2.10.1")
    ]
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "app"},
            "node_modules/lodash": {"version": "4.17.20"},
            "node_modules/a/node_modules/minimist": {"version": "1.2.0"},
        },
    }
    assert {p.key for p in parse("web/package-lock.json", json.dumps(lock))} == {
        ("npm", "lodash", "4.17.20"),
        ("npm", "minimist", "1.2.0"),
    }
    gomod = "module x\n\nrequire (\n\tgolang.org/x/net v0.10.0\n\tgithub.com/a/b v1.2.3 // indirect\n)\n"
    assert {p.key for p in parse("go.mod", gomod)} == {
        ("Go", "golang.org/x/net", "v0.10.0"),
        ("Go", "github.com/a/b", "v1.2.3"),
    }
    pom = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies><dependency>'
        "<groupId>org.apache.logging.log4j</groupId><artifactId>log4j-core</artifactId>"
        "<version>2.14.1</version></dependency><dependency><groupId>g</groupId>"
        "<artifactId>a</artifactId><version>${v}</version></dependency></dependencies></project>"
    )
    assert [p.key for p in parse("pom.xml", pom)] == [
        ("Maven", "org.apache.logging.log4j:log4j-core", "2.14.1")
    ]
    csproj = (
        '<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" Version="12.0.1" />'
        "</ItemGroup></Project>"
    )
    assert [p.key for p in parse("App.csproj", csproj)] == [("NuGet", "Newtonsoft.Json", "12.0.1")]
    assert parse("package.json", "{not json") == []


def test_offline_database_and_osv_api(tmp_path: Path) -> None:
    db = tmp_path / "osv" / "PyPI"
    db.mkdir(parents=True)
    (db / "GHSA-test-0001.json").write_text(json.dumps(ADVISORY))
    (db / "withdrawn.json").write_text(json.dumps({**ADVISORY, "id": "W", "withdrawn": "2024-01-01"}))
    pkgs = [_pkg("PyPI", "jinja-like", "2.10.1"), _pkg("PyPI", "requests", "2.31.0")]
    found = dependencies.findings_for(
        pkgs, base_keys=set(), source=dependencies.OfflineDatabase(tmp_path / "osv")
    )
    assert [(f.rule_id, f.severity, f.introduced) for f in found] == [("GHSA-test-0001", "high", True)]
    assert "2.11.3" in found[0].message and "CVE-2099-0001" in found[0].message
    # Already pinned at the base: reported, not introduced.
    assert not dependencies.findings_for(pkgs, {pkgs[0].key}, dependencies.OfflineDatabase(tmp_path / "osv"))[
        0
    ].introduced

    from tests.osv_server import serve_osv

    with serve_osv([ADVISORY]) as osv:
        api = dependencies.OsvApi(osv.url)
        found = dependencies.findings_for(pkgs, set(), api)
        assert [f.rule_id for f in found] == ["GHSA-test-0001"]
        # Only names and versions leave the installation.
        assert osv.queries == [
            {"package": {"ecosystem": "PyPI", "name": "jinja-like"}, "version": "2.10.1"},
            {"package": {"ecosystem": "PyPI", "name": "requests"}, "version": "2.31.0"},
        ]


# ---- SARIF and policy ---------------------------------------------------------------------------


def _sarif(results: list[dict]) -> dict:
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "semgrep",
                        "rules": [
                            {
                                "id": "r.sqli",
                                "shortDescription": {"text": "SQL injection"},
                                "properties": {"security-severity": "9.1"},
                            },
                            {"id": "r.style", "defaultConfiguration": {"level": "note"}},
                        ],
                    }
                },
                "results": results,
            }
        ],
    }


def test_sarif_results_map_to_findings() -> None:
    doc = _sarif(
        [
            {
                "ruleId": "r.sqli",
                "level": "error",
                "message": {"text": "user input reaches query"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "app/db.py"},
                            "region": {"startLine": 7},
                        }
                    }
                ],
            },
            {
                "ruleId": "r.style",
                "message": {"text": "nit"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "./app/x.py"},
                            "region": {"startLine": 2},
                        }
                    }
                ],
            },
        ]
    )
    found = sarif.parse(doc, "semgrep", {"app/db.py": {7}})
    assert [(f.rule_id, f.severity, f.path, f.introduced) for f in found] == [
        ("r.sqli", "critical", "app/db.py", True),
        ("r.style", "low", "app/x.py", False),
    ]
    with pytest.raises(ValueError):
        sarif.parse({"nope": 1}, "x", {})


def test_policy_blocks_only_introduced_findings_at_or_above_threshold() -> None:
    def f(sev: str, introduced: bool, scanner: str = "code") -> Finding:
        return Finding(scanner, "r", sev, "t", "a.py", 1, "m", introduced=introduced)  # type: ignore[arg-type]

    p = SecurityPolicy()
    assert p.blocks(f("high", True)) and p.blocks(f("critical", True))
    assert not p.blocks(f("medium", True)) and not p.blocks(f("critical", False))
    assert p.blocks(f("low", True, "secrets"))  # any new secret
    lax = SecurityPolicy.from_dict({"block_severity": "critical", "block_secrets": False})
    assert not lax.blocks(f("high", True)) and not lax.blocks(f("low", True, "secrets"))
    with pytest.raises(ValueError):
        SecurityPolicy.from_dict({"block_severity": "urgent"})
    with pytest.raises(ValueError):
        validate_tools([{"name": "x", "command": "semgrep --sarif", "sarif": "o.sarif"}])
    with pytest.raises(ValueError):
        validate_tools([{"name": "x", "command": ["t"], "sarif": "../escape.sarif"}])


# ---- a whole change -----------------------------------------------------------------------------


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*g, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*g, "commit", "-qm", msg], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def change(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "origin"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    base = _commit(
        repo,
        {
            "app/legacy.py": f'LEGACY_KEY = "{AWS_ID[:-1]}X"\n',
            "app/main.py": "def handler(x):\n    return x\n",
            "requirements.txt": "requests==2.31.0\n",
        },
        "base",
    )
    head = _commit(
        repo,
        {
            "app/main.py": (f'import yaml\n\ndef handler(x):\n    return eval(x)\n\nGITHUB = "{GH_TOKEN}"\n'),
            "requirements.txt": "requests==2.31.0\njinja-like==2.10.1\n",
        },
        "change",
    )
    return repo, base, head


def test_scan_change_judges_what_the_change_introduced(
    change: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = change
    assert added_lines(repo, base, head) == {"app/main.py": {1, 2, 4, 5, 6}, "requirements.txt": {2}}
    report = scan_change(repo, base, head, policy=SecurityPolicy())
    by_rule = {f["rule_id"]: f for f in report.findings}
    assert by_rule["aws-access-key-id"]["introduced"] is False  # existing secret: reported only
    assert by_rule["github-token"]["introduced"] is True
    assert by_rule["py-eval"]["introduced"] is True
    assert report.scanners["dependencies"]["status"] == "not_configured"
    assert report.status == "blocked"
    assert sorted(by_rule[r]["fingerprint"] for r in ("github-token", "py-eval")) == sorted(report.blocking)
    assert GH_TOKEN not in json.dumps(report.as_dict())

    # Accepting a finding (by fingerprint) unblocks it; the rest still blocks.
    accepted = {by_rule["py-eval"]["fingerprint"]}
    assert scan_change(repo, base, head, policy=SecurityPolicy(), accepted=accepted).blocking == [
        by_rule["github-token"]["fingerprint"]
    ]

    # With advisories configured, the newly pinned vulnerable package is introduced and blocks.
    osv = tmp_path / "osv"
    osv.mkdir()
    (osv / "a.json").write_text(json.dumps(ADVISORY))
    monkeypatch.setattr(get_settings(), "security_advisory_dir", osv)
    report = scan_change(repo, base, head, policy=SecurityPolicy(), accepted=accepted)
    dep = next(f for f in report.findings if f["scanner"] == "dependencies")
    assert dep["introduced"] and dep["fingerprint"] in report.blocking and dep["line"] == 2

    # Required but unavailable: an error, never a silent pass.
    monkeypatch.setattr(get_settings(), "security_advisory_dir", None)
    strict = SecurityPolicy.from_dict({"require_dependency_scan": True})
    assert scan_change(repo, base, head, policy=strict).status == "error"


@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("unshare"), reason="needs root and unshare")
def test_sarif_tool_runs_in_the_sandbox(change: tuple[Path, str, str]) -> None:
    repo, base, head = change
    tool = (
        "import json, pathlib, socket\n"
        "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2); net = 'yes'\n"
        "except OSError:\n    net = 'no'\n"
        "doc = {'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 't', 'rules': []}}, 'results': [\n"
        "  {'ruleId': 'net-' + net, 'level': 'error', 'message': {'text': 'm'},\n"
        "   'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'app/main.py'},\n"
        "                                      'region': {'startLine': 4}}}]}]}]}\n"
        "pathlib.Path('.asw').mkdir(exist_ok=True)\n"
        "pathlib.Path('.asw/t.sarif').write_text(json.dumps(doc))\n"
    )
    py = get_settings().sandbox_python if Path(get_settings().sandbox_python).exists() else sys.executable
    tools = validate_tools(
        [
            {"name": "probe", "command": [py, "-c", tool], "sarif": ".asw/t.sarif", "timeout_s": 60},
            {"name": "broken", "command": [py, "-c", "pass"], "sarif": ".asw/none.sarif", "timeout_s": 60},
        ]
    )
    report = scan_change(repo, base, head, policy=SecurityPolicy(), tools=tools)
    probe = [f for f in report.findings if f["scanner"] == "sarif:probe"]
    assert [f["rule_id"] for f in probe] == ["net-no"]  # no network inside the sandbox
    assert probe[0]["introduced"] and probe[0]["fingerprint"] in report.blocking
    assert report.scanners["sarif:broken"]["status"] == "error"
    assert report.status == "error" and "produced no SARIF report" in report.problems[0]


def test_advisory_mirror_sync_is_atomic_and_refuses_bad_exports(tmp_path: Path) -> None:
    import io
    import zipfile

    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    from agentic_sdlc.cli import main
    from agentic_sdlc.security.advisories import sync
    from tests.model_server import _free_port

    def zipped(records: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, body in records.items():
                zf.writestr(name, body)
        return buf.getvalue()

    exports = {
        "PyPI": zipped({"GHSA-test-0001.json": json.dumps(ADVISORY), "../../evil.json": "{}", "x.txt": "no"}),
        "npm": zipped({"README": "nothing here"}),
    }

    async def export(request: Request) -> Response:
        eco = request.path_params["eco"]
        return (
            Response(exports[eco], media_type="application/zip")
            if eco in exports
            else Response(status_code=404)
        )

    import threading
    import time

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/{eco}/all.zip", export)]),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    source = f"http://127.0.0.1:{port}"
    try:
        dest = tmp_path / "osv"
        assert sync(dest, ("PyPI",), source) == {"PyPI": 2}
        # Archive paths are flattened to a safe file name inside the mirror: nothing escapes it.
        assert sorted(p.name for p in (dest / "PyPI").iterdir()) == ["GHSA-test-0001.json", "evil.json"]
        assert not (tmp_path / "evil.json").exists() and not (dest / "evil.json").exists()
        # A broken export keeps the previous mirror.
        (dest / "npm").mkdir()
        (dest / "npm" / "keep.json").write_text("{}")
        with pytest.raises(ValueError, match="no advisories"):
            sync(dest, ("npm",), source)
        assert (dest / "npm" / "keep.json").exists()
        assert (
            main(["advisories", "sync", "--dest", str(dest), "--ecosystem", "PyPI", "--source", source]) == 0
        )
        assert main(["advisories", "sync", "--dest", str(dest), "--ecosystem", "Go", "--source", source]) == 1
        # The mirrored records feed the scanner directly.
        found = dependencies.findings_for(
            [_pkg("PyPI", "jinja-like", "2.10.1")], set(), dependencies.OfflineDatabase(dest)
        )
        assert [f.rule_id for f in found] == ["GHSA-test-0001"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
