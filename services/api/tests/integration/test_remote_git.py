"""Remote repositories over real git smart HTTP, and pull-request / CI adapters per provider."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.integrations import git_hosting as gh
from agentic_sdlc.integrations.git_hosting import (
    GitConnection,
    GitHostingError,
    HostingClient,
    parse_clone_url,
)
from tests.git_server import FakeHost, serve_host

FILES = {"README.md": "# tasks\n", "tasks/core.py": "def add(a, b):\n    return a + b\n"}


@pytest.fixture
def host(tmp_path: Path) -> Iterator[FakeHost]:
    with serve_host(FakeHost(root=tmp_path / "remote")) as h:
        yield h


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Configure connections for a test; restores settings and the connection cache afterwards."""
    s = get_settings()
    saved = (s.git_connections, list(s.git_insecure_http_hosts))
    s.git_insecure_http_hosts = ["127.0.0.1"]

    def configure(*conns: dict[str, Any]) -> dict[str, GitConnection]:
        s.git_connections = json.dumps(list(conns))
        gh.load_connections.cache_clear()
        gh._token_cache.clear()
        return gh.load_connections()

    yield configure
    s.git_connections, s.git_insecure_http_hosts = saved[0], saved[1]
    gh.load_connections.cache_clear()
    gh._token_cache.clear()


def github(host: FakeHost, **kw: Any) -> dict[str, Any]:
    return {
        "name": "gh",
        "provider": "github",
        "api_url": f"{host.url}/github",
        "git_host": "127.0.0.1",
        "auth": "token",
        "token_env": "TEST_GH_TOKEN",
        **kw,
    }


# --- URL and connection rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://github.com/acme/tasks", "https"),
        ("https://user:pw@github.com/acme/tasks", "credentials"),
        ("https://gitlab.com/acme/tasks", "does not match"),
        ("https://github.com/acme", "does not name"),
        ("https://github.com/acme/tasks/extra", "look like"),
        ("https://github.com/acme/../tasks", "does not name"),
        ("https://github.com/acme/tasks?x=1", "query"),
    ],
)
def test_clone_urls_are_validated_against_the_connection(url: str, error: str) -> None:
    conn = GitConnection(name="gh", provider="github", auth="none")
    with pytest.raises(ValidationFailed) as exc:
        parse_clone_url(url, conn)
    assert error in (exc.value.message + json.dumps(exc.value.details))


def test_clone_urls_are_canonicalised() -> None:
    conn = GitConnection(name="gh", provider="github", auth="none")
    assert parse_clone_url("https://GitHub.com/acme/tasks.git", conn).url == "https://github.com/acme/tasks"
    ado = GitConnection(name="ado", provider="azure_devops", auth="none")
    ref = parse_clone_url("https://dev.azure.com/org/Proj/_git/tasks", ado)
    assert ref.path == "org/Proj/_git/tasks"
    with pytest.raises(ValidationFailed):
        parse_clone_url("https://dev.azure.com/org/Proj/tasks", ado)


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "x", "provider": "github", "auth": "token"},  # no token_env
        {"name": "x", "provider": "gitlab", "auth": "github_app", "app_id": 1, "private_key_env": "K"},
        {"name": "x", "provider": "github", "auth": "github_app"},  # no app id / key
        {"name": "x", "provider": "github", "auth": "none", "api_url": "http://api.example.com"},
        {"name": "Bad", "provider": "github", "auth": "none"},
        {"name": "x", "provider": "github", "auth": "none", "secret": "y"},
    ],
)
def test_invalid_connections_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GitConnection.model_validate(bad)


def test_generic_connections_cannot_open_pull_requests() -> None:
    c = GitConnection(name="g", provider="generic", auth="none")
    assert c.operations == ["read", "push"] and not c.allows("pull_request")


def test_credentials_are_scoped_to_the_repository_url() -> None:
    conn = GitConnection(name="gl", provider="gitlab", auth="token", token_env="X")
    ref = parse_clone_url("https://gitlab.com/group/sub/tasks", conn)
    env = gh.git_auth_env(ref, gh.Credential("oauth2", "tok"))
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.com/group/sub/tasks.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert gh.git_auth_env(ref, None) == {}


# --- sync and publish over real git smart HTTP -----------------------------------------------------------


def _repo_row(host: FakeHost, path: str = "acme/tasks", branch: str = "main") -> Any:
    from agentic_sdlc.orm import ProjectRepository

    return ProjectRepository(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="tasks",
        kind="git",
        location=f"{host.url}/{path}",
        default_branch=branch,
        is_sample=False,
        connection="gh",
        sync_status={},
        test_config={},
    )


def test_sync_and_publish_with_credentials(
    host: FakeHost, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_sdlc.execution.repo_reader import head_commit
    from agentic_sdlc.projects import repositories as repos

    monkeypatch.setenv("TEST_GH_TOKEN", host.token)
    connections(github(host))
    remote_head = host.create_repo("acme/tasks", FILES)
    repo = _repo_row(host)
    status = repos.sync(repo)
    assert status["state"] == "ok" and status["head"] == remote_head
    origin = repos.local_origin(repo)
    assert head_commit(origin, "main") == remote_head

    # An agent-style commit on a platform branch, pushed back to the mirror, then published.
    work = origin.parent / "ws"
    git = ["git", "-c", "user.name=a", "-c", "user.email=a@a", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "tasks" / "core.py").write_text("def add(a, b):\n    return a + b  # reviewed\n")
    subprocess.run([*git, "commit", "-qam", "change"], cwd=work, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/agent/x"], cwd=work, check=True)
    repos.publish_branch(repo, "asw/run-1", sha)
    assert host.head("acme/tasks", "asw/run-1") == sha
    repos.publish_branch(repo, "asw/run-1", sha)  # idempotent

    with pytest.raises(GitHostingError, match="asw/ namespace"):
        repos.publish_branch(repo, "main", sha)
    # Never forced: a diverged remote branch is not overwritten.
    with pytest.raises(GitHostingError, match="failed"):
        repos.publish_branch(repo, "asw/run-1", remote_head)
    assert host.head("acme/tasks", "asw/run-1") == sha
    # A later sync keeps unpublished platform branches.
    assert repos.sync(repo)["state"] == "ok"
    assert head_commit(origin, "agent/x") == sha


def test_bad_credentials_fail_without_leaking_the_token(
    host: FakeHost, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_sdlc.projects import repositories as repos

    monkeypatch.setenv("TEST_GH_TOKEN", "wrong-token-value-123")
    connections(github(host))
    host.create_repo("acme/tasks", FILES)
    repo = _repo_row(host)
    status = repos.sync(repo)
    assert status["state"] == "error"
    assert "wrong-token-value-123" not in json.dumps(status)
    with pytest.raises(ValidationFailed, match="not been synchronised"):
        repos.local_origin(repo)  # an empty mirror from a failed first sync is not usable
    monkeypatch.delenv("TEST_GH_TOKEN")
    assert "not set" in repos.sync(repo)["error"]


def test_missing_default_branch_is_reported(
    host: FakeHost, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_sdlc.projects import repositories as repos

    monkeypatch.setenv("TEST_GH_TOKEN", host.token)
    connections(github(host))
    host.create_repo("acme/tasks", FILES)
    status = repos.sync(_repo_row(host, branch="develop"))
    assert status["state"] == "error" and "no branch 'develop'" in status["error"]


# --- pull requests and CI per provider --------------------------------------------------------------------


PROVIDERS: dict[str, tuple[dict[str, Any], str]] = {
    "github": ({"provider": "github", "api_url": "{url}/github"}, "acme/tasks"),
    "gitlab": ({"provider": "gitlab", "api_url": "{url}/gitlab"}, "group/sub/tasks"),
    "bitbucket": ({"provider": "bitbucket", "api_url": "{url}/bitbucket"}, "acme/tasks"),
    "azure_devops": ({"provider": "azure_devops", "api_url": "{url}/azure"}, "org/Proj/_git/tasks"),
}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_pull_requests_are_idempotent_and_ci_is_normalised(
    provider: str, host: FakeHost, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, path = PROVIDERS[provider]
    monkeypatch.setenv("TEST_TOKEN", host.token)
    conns = connections(
        {
            "name": "c",
            "git_host": "127.0.0.1",
            "auth": "token",
            "token_env": "TEST_TOKEN",
            **{k: v.format(url=host.url) for k, v in cfg.items()},
        }
    )
    conn = conns["c"]
    ref = parse_clone_url(f"{host.url}/{path}", conn)
    client = HostingClient(conn, ref)
    pr = client.open_pull_request(branch="asw/run-1", base="main", title="T", body="evidence")
    assert pr.created and pr.state == "open" and pr.head_branch == "asw/run-1" and pr.url
    again = client.open_pull_request(branch="asw/run-1", base="main", title="T", body="evidence")
    assert not again.created and again.number == pr.number
    assert len(host.pulls) == 1 and host.pulls[0]["base"] == "main" and host.pulls[0]["body"] == "evidence"

    sha = "a" * 40
    assert client.ci_status(sha).state == "none"
    host.checks[sha] = [
        {"name": "build", "status": "in_progress"},
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    assert client.ci_status(sha).state == "pending"
    host.checks[sha][0] = {"name": "build", "status": "completed", "conclusion": "success"}
    status = client.ci_status(sha)
    assert status.state == "success" and {c["name"].split("/")[-1] for c in status.checks} == {
        "build",
        "lint",
    }
    host.checks[sha][1] = {"name": "lint", "status": "completed", "conclusion": "failure"}
    assert client.ci_status(sha).state == "failure"

    # Every API request carried the credential, and nothing went anywhere but the connection's API.
    api_prefix = cfg["api_url"].format(url="").rstrip("/")
    assert all(r["path"].startswith(api_prefix + "/") for r in host.requests)


def test_credentials_are_never_sent_outside_the_connection_api(host: FakeHost) -> None:
    conn = GitConnection(name="gh", provider="github", auth="none", api_url="https://api.github.com")
    client = HostingClient(conn, parse_clone_url("https://github.com/acme/tasks", conn))
    with pytest.raises(GitHostingError, match="outside"):
        client._req("GET", f"{host.url}/github/repos/acme/tasks/pulls", "probe")


def test_github_app_mints_a_narrow_installation_token(
    host: FakeHost, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    host.app_public_key = key.public_key()
    monkeypatch.setenv("TEST_APP_KEY", pem)
    conns = connections(
        {
            "name": "app",
            "provider": "github",
            "api_url": f"{host.url}/github",
            "git_host": "127.0.0.1",
            "auth": "github_app",
            "app_id": 99,
            "private_key_env": "TEST_APP_KEY",
            "operations": ["read", "push", "pull_request"],
        }
    )
    conn = conns["app"]
    ref = parse_clone_url(f"{host.url}/acme/tasks", conn)
    host.create_repo("acme/tasks", FILES)
    cred = gh.credential(conn, ref)
    assert cred is not None and cred.secret.startswith("ghs_app_") and cred.expires_at is not None
    minted = host.app_tokens[0]
    assert minted["installation"] == "4242"
    assert minted["body"]["repositories"] == ["tasks"]
    assert minted["body"]["permissions"] == {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    assert gh.credential(conn, ref) is cred  # cached until close to expiry
    # The minted token authenticates git and the API.
    pr = HostingClient(conn, ref).open_pull_request(branch="asw/x", base="main", title="t", body="b")
    assert pr.created
    assert HostingClient(conn, ref).ci_status("b" * 40).state == "none"  # checks not permitted


# --- API --------------------------------------------------------------------------------------------------


def test_registering_a_remote_repository_queues_a_sync_done_by_the_worker(
    client: Any, project: dict, host: FakeHost, connections: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_sdlc.workflows.worker import drain
    from tests.conftest import H

    connections(github(host))
    host.create_repo("acme/tasks", FILES)
    pid = project["id"]
    bad = client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"name": "t", "kind": "git", "location": f"{host.url}/acme/tasks", "connection": "nope"},
        headers=H("owner"),
    )
    assert bad.status_code == 422
    r = client.post(
        f"/api/v1/projects/{pid}/repositories",
        json={"name": "tasks", "kind": "git", "location": f"{host.url}/acme/tasks.git", "connection": "gh"},
        headers=H("owner"),
    )
    assert r.status_code == 201, r.text
    repo = r.json()
    assert repo["sync_status"]["state"] == "pending" and repo["location"] == f"{host.url}/acme/tasks"
    # The API process has no token; the worker does.
    monkeypatch.setenv("TEST_GH_TOKEN", host.token)
    drain()
    listed = client.get(f"/api/v1/projects/{pid}/repositories", headers=H("auditor")).json()
    synced = next(x for x in listed if x["id"] == repo["id"])
    assert synced["sync_status"]["state"] == "ok" and len(synced["sync_status"]["head"]) == 40
    profile = client.get(f"/api/v1/projects/{pid}/repositories/{repo['id']}/test-profile", headers=H("owner"))
    assert profile.json()["profile"]["name"] == "pytest"
    conns = client.get("/api/v1/git-connections", headers=H("auditor")).json()
    assert conns == [
        {
            "name": "gh",
            "provider": "github",
            "host": "127.0.0.1",
            "auth": "token",
            "operations": ["read", "push", "pull_request", "checks"],
            "credential_available": True,
        }
    ]
    assert (
        client.post(
            f"/api/v1/projects/{pid}/repositories/{repo['id']}/sync", headers=H("auditor")
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/projects/{pid}/repositories/{repo['id']}/sync", headers=H("owner")).status_code
        == 202
    )
