"""A real git smart-HTTP server and a fake hosting API, for tests.

Git traffic is served by `git http-backend` (the same CGI program real servers use) behind a check
of the `Authorization` header, so fetch/push with credentials, rejection of bad credentials and
refusal of non-fast-forward pushes are exercised for real. Pull-request and CI endpoints mimic the
subset of the GitHub, GitLab, Bitbucket Cloud and Azure DevOps REST APIs the platform calls, and
record every request so tests can assert exactly what was sent.
"""

from __future__ import annotations

import base64
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.model_server import _free_port


@dataclass
class FakeHost:
    root: Path  # directory holding bare repositories, e.g. root/acme/tasks
    token: str = "s3cret-token"
    user: str = "x-access-token"
    requests: list[dict[str, Any]] = field(default_factory=list)
    pulls: list[dict[str, Any]] = field(default_factory=list)
    # sha -> list of {"name", "status", "conclusion"} (GitHub check-run shape) used by every provider
    checks: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    app_tokens: list[dict[str, Any]] = field(default_factory=list)
    app_public_key: Any = None  # verify GitHub App JWTs when set
    url: str = ""

    # -- helpers
    def bare(self, path: str) -> Path:
        # No ".git" suffix: the platform canonicalises clone URLs without it.
        return self.root / path

    def create_repo(self, path: str, files: dict[str, str], branch: str = "main") -> str:
        work = self.root.parent / f"{self.root.name}-work" / path
        work.mkdir(parents=True)
        for rel, content in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(content)
        g = ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=work, check=True)
        subprocess.run([*g, "add", "-A"], cwd=work, check=True)
        subprocess.run([*g, "commit", "-qm", "initial"], cwd=work, check=True)
        bare = self.bare(path)
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
        subprocess.run(["git", "config", "http.receivepack", "true"], cwd=bare, check=True)
        return self.head(path, branch)

    def head(self, path: str, branch: str = "main") -> str:
        out = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"], cwd=self.bare(path), capture_output=True, text=True
        )
        return out.stdout.strip()

    def _authorized(self, request: Request, *, api: bool = False) -> bool:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            _user, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            return secret in self.valid_tokens()
        if auth.startswith("Bearer "):
            return auth[7:] in self.valid_tokens()
        return api and request.headers.get("private-token") in self.valid_tokens()

    def valid_tokens(self) -> set[str]:
        return {self.token, *(t["token"] for t in self.app_tokens)}

    # -- git smart HTTP via git http-backend
    async def git(self, request: Request) -> Response:
        if not self._authorized(request):
            return Response(
                "auth required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="git"'}
            )
        body = await request.body()
        env = {
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": "/" + request.path_params["path"],
            "REQUEST_METHOD": request.method,
            "QUERY_STRING": request.url.query,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "tester",
            "REMOTE_ADDR": "127.0.0.1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.root),
            "PATH": "/usr/bin:/bin",
        }
        if request.headers.get("content-encoding") == "gzip":
            env["HTTP_CONTENT_ENCODING"] = "gzip"
        proc = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True, timeout=60)
        head, _, payload = proc.stdout.partition(b"\r\n\r\n")
        status, headers = 200, {}
        for line in head.decode(errors="replace").split("\r\n"):
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            elif k:
                headers[k.strip()] = v.strip()
        return Response(payload, status_code=status, headers=headers)

    # -- hosting APIs
    def _record(self, request: Request, body: Any) -> None:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params),
                "json": body,
                "headers": dict(request.headers),
            }
        )

    async def api(self, request: Request) -> Response:
        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.json()
        self._record(request, body)
        path = request.url.path
        if path.startswith("/github/app/") or path.endswith("/installation"):
            return self._github_app(request, body)
        if not self._authorized(request, api=True):
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        if path.startswith("/github/"):
            return self._github(request, path.removeprefix("/github"), body)
        if path.startswith("/gitlab/"):
            return self._gitlab(request, path.removeprefix("/gitlab"), body)
        if path.startswith("/bitbucket/"):
            return self._bitbucket(request, path.removeprefix("/bitbucket"), body)
        if path.startswith("/azure/"):
            return self._azure(request, path.removeprefix("/azure"), body)
        return JSONResponse({"message": "not found"}, status_code=404)

    def _github_app(self, request: Request, body: Any) -> Response:
        import jwt

        auth = request.headers.get("authorization", "")
        try:
            claims = jwt.decode(auth.removeprefix("Bearer "), self.app_public_key, algorithms=["RS256"])
        except Exception:
            return JSONResponse({"message": "invalid JWT"}, status_code=401)
        if request.url.path.endswith("/installation"):
            return JSONResponse({"id": 4242, "app_id": int(claims["iss"])})
        token = f"ghs_app_{len(self.app_tokens)}_{int(time.time())}"
        self.app_tokens.append(
            {"token": token, "body": body, "installation": request.url.path.split("/")[-2]}
        )
        return JSONResponse({"token": token, "expires_at": "2099-01-01T00:00:00Z"}, status_code=201)

    def _ci(self, sha: str) -> list[dict[str, str]]:
        return self.checks.get(sha, [])

    def _github(self, request: Request, path: str, body: Any) -> Response:
        parts = path.strip("/").split("/")  # repos/<owner>/<repo>/...
        owner, repo = parts[1], parts[2]
        rest = parts[3:]
        if rest == ["pulls"] and request.method == "GET":
            head = request.query_params.get("head", "")
            found = [p for p in self.pulls if p["provider"] == "github" and f"{owner}:{p['head']}" == head]
            return JSONResponse([self._gh_pr(p, owner, repo) for p in found])
        if rest == ["pulls"] and request.method == "POST":
            p = self._new_pull(
                "github", f"{owner}/{repo}", body["head"], body["base"], body["title"], body["body"]
            )
            return JSONResponse(self._gh_pr(p, owner, repo), status_code=201)
        if len(rest) == 3 and rest[0] == "commits" and rest[2] == "check-runs":
            return JSONResponse({"check_runs": self._ci(rest[1])})
        if len(rest) == 3 and rest[0] == "commits" and rest[2] == "status":
            return JSONResponse({"state": "pending", "statuses": []})
        return JSONResponse({"message": "not found"}, status_code=404)

    def _gh_pr(self, p: dict[str, Any], owner: str, repo: str) -> dict[str, Any]:
        return {
            "number": p["number"],
            "html_url": f"{self.url}/{owner}/{repo}/pull/{p['number']}",
            "state": "open",
            "merged_at": None,
            "head": {"ref": p["head"]},
        }

    def _gitlab(self, request: Request, path: str, body: Any) -> Response:
        # /projects/<urlencoded>/merge_requests | /projects/<enc>/repository/commits/<sha>/statuses
        from urllib.parse import unquote

        raw = request.scope["raw_path"].decode().removeprefix("/gitlab/projects/")
        enc, _, rest = raw.partition("/")
        project = unquote(enc)
        if rest == "merge_requests" and request.method == "GET":
            found = [
                p
                for p in self.pulls
                if p["provider"] == "gitlab" and p["head"] == request.query_params.get("source_branch")
            ]
            return JSONResponse([self._gl_mr(p, project) for p in found])
        if rest == "merge_requests" and request.method == "POST":
            p = self._new_pull(
                "gitlab",
                project,
                body["source_branch"],
                body["target_branch"],
                body["title"],
                body["description"],
            )
            return JSONResponse(self._gl_mr(p, project), status_code=201)
        if rest.startswith("repository/commits/") and rest.endswith("/statuses"):
            sha = rest.split("/")[2]
            return JSONResponse([{"name": c["name"], "status": _gl_status(c)} for c in self._ci(sha)])
        return JSONResponse({"message": "404"}, status_code=404)

    def _gl_mr(self, p: dict[str, Any], project: str) -> dict[str, Any]:
        return {
            "iid": p["number"],
            "web_url": f"{self.url}/{project}/-/merge_requests/{p['number']}",
            "state": "opened",
            "source_branch": p["head"],
        }

    def _bitbucket(self, request: Request, path: str, body: Any) -> Response:
        parts = path.strip("/").split("/")  # repositories/<ws>/<slug>/...
        repo = f"{parts[1]}/{parts[2]}"
        rest = parts[3:]
        if rest == ["pullrequests"] and request.method == "GET":
            q = request.query_params.get("q", "")
            found = [p for p in self.pulls if p["provider"] == "bitbucket" and f'"{p["head"]}"' in q]
            return JSONResponse({"values": [self._bb_pr(p, repo) for p in found]})
        if rest == ["pullrequests"] and request.method == "POST":
            p = self._new_pull(
                "bitbucket",
                repo,
                body["source"]["branch"]["name"],
                body["destination"]["branch"]["name"],
                body["title"],
                body["description"],
            )
            return JSONResponse(self._bb_pr(p, repo), status_code=201)
        if len(rest) == 3 and rest[0] == "commit" and rest[2] == "statuses":
            return JSONResponse(
                {
                    "values": [
                        {"key": c["name"], "name": c["name"], "state": _bb_state(c)}
                        for c in self._ci(rest[1])
                    ]
                }
            )
        return JSONResponse({"error": {"message": "not found"}}, status_code=404)

    def _bb_pr(self, p: dict[str, Any], repo: str) -> dict[str, Any]:
        return {
            "id": p["number"],
            "state": "OPEN",
            "source": {"branch": {"name": p["head"]}},
            "links": {"html": {"href": f"{self.url}/{repo}/pull-requests/{p['number']}"}},
        }

    def _azure(self, request: Request, path: str, body: Any) -> Response:
        # /<org>/<project>/_apis/git/repositories/<repo>/pullrequests | .../commits/<sha>/statuses
        parts = path.strip("/").split("/")
        repo = f"{parts[0]}/{parts[1]}/_git/{parts[5]}"
        rest = parts[6:]
        if request.query_params.get("api-version") != "7.1":
            return JSONResponse({"message": "api-version required"}, status_code=400)
        if rest == ["pullrequests"] and request.method == "GET":
            ref = request.query_params.get("searchCriteria.sourceRefName")
            found = [
                p for p in self.pulls if p["provider"] == "azure_devops" and f"refs/heads/{p['head']}" == ref
            ]
            return JSONResponse({"value": [self._ado_pr(p) for p in found]})
        if rest == ["pullrequests"] and request.method == "POST":
            p = self._new_pull(
                "azure_devops",
                repo,
                body["sourceRefName"].removeprefix("refs/heads/"),
                body["targetRefName"].removeprefix("refs/heads/"),
                body["title"],
                body["description"],
            )
            return JSONResponse(self._ado_pr(p), status_code=201)
        if len(rest) == 3 and rest[0] == "commits" and rest[2] == "statuses":
            return JSONResponse(
                {
                    "value": [
                        {"context": {"genre": "ci", "name": c["name"]}, "state": _ado_state(c)}
                        for c in self._ci(rest[1])
                    ]
                }
            )
        return JSONResponse({"message": "not found"}, status_code=404)

    def _ado_pr(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"pullRequestId": p["number"], "status": "active", "sourceRefName": f"refs/heads/{p['head']}"}

    def _new_pull(
        self, provider: str, repo: str, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        p = {
            "provider": provider,
            "repo": repo,
            "number": len(self.pulls) + 1,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        }
        self.pulls.append(p)
        return p

    def app(self) -> Starlette:
        api = ["GET", "POST"]
        return Starlette(
            routes=[
                *(
                    Route(f"/{p}/{{path:path}}", self.api, methods=api)
                    for p in ("github", "gitlab", "bitbucket", "azure")
                ),
                Route("/{path:path}", self.git, methods=api),  # git smart HTTP for every other path
            ]
        )


def _gl_status(c: dict[str, str]) -> str:
    if c["status"] != "completed":
        return "running"
    return {"success": "success", "failure": "failed"}.get(c.get("conclusion", ""), "failed")


def _bb_state(c: dict[str, str]) -> str:
    if c["status"] != "completed":
        return "INPROGRESS"
    return "SUCCESSFUL" if c.get("conclusion") == "success" else "FAILED"


def _ado_state(c: dict[str, str]) -> str:
    if c["status"] != "completed":
        return "pending"
    return "succeeded" if c.get("conclusion") == "success" else "failed"


@contextmanager
def serve_host(host: FakeHost) -> Iterator[FakeHost]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(host.app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake git host did not start")
        time.sleep(0.02)
    host.url = f"http://127.0.0.1:{port}"
    try:
        yield host
    finally:
        server.should_exit = True
        thread.join(timeout=10)
