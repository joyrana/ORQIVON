# Connecting Git Hosting

Remote repositories live on GitHub (cloud or Enterprise Server), GitLab (cloud or self-managed),
Bitbucket Cloud or Azure DevOps. A generic connection covers any https Git server, but supports only
push, not pull requests. The platform:

1. keeps a **mirror** of each repository, refreshed at the start of every run;
2. lets agents work only on disposable clones of that mirror, never on the remote;
3. after the independent regression run passes, **pushes** the verified commit to `asw/run-<id>`
   and **opens a pull request** that carries the criteria verification table and test evidence;
4. **waits for the host's CI** on exactly that commit (bounded) before release readiness;
5. **never merges**. Merging stays with your reviewers and branch protection.

## Declare connections

Connections are deployment configuration, not data: they are declared in a file that
`GIT_CONNECTIONS_FILE` points to, and referenced by name when a repository is registered. See
`infrastructure/config/git-connections.example.yaml`.

| Provider | `provider` | Recommended auth | Scopes or permissions |
|---|---|---|---|
| GitHub / GHES | `github` | `github_app` (`app_id`, `private_key_file`) | Contents: write, Pull requests: write, Checks: read, Commit statuses: read, Metadata: read |
| GitLab | `gitlab` | `token` (project or group access token) | `read_repository`, `write_repository`, `api` |
| Bitbucket Cloud | `bitbucket` | `token` (repository access token) | Repositories: write, Pull requests: write |
| Azure DevOps | `azure_devops` | `token` (PAT) | Code: read & write, Code status: read |
| Any https server | `generic` | `token` | read and push |

With `auth: github_app`, the platform signs a short JWT with the App's private key and mints an
**installation token for one repository with only those permissions**. The token lasts an hour
and is refreshed before it expires. Prefer this to personal tokens.

`operations` narrows what a connection may do (`read`, `push`, `pull_request`, `checks`). For
example, `["read"]` lets agents analyse a repository without being able to publish anything.

## Where credentials live

- Only the **worker** needs them: it syncs mirrors, pushes and opens pull requests. Leave the API
  process without them; the Settings page then shows `credential_available: false`, which is
  expected.
- They are read from environment variables or files you mount, and are never stored in the
  database. A repository stores only its connection's *name*.
- git receives the credential as a per-URL `http.extraHeader` through `GIT_CONFIG_*` environment
  variables. It never appears on a command line, in a config file, in logs, in events, in prompts,
  or inside the sandbox.
- A connection's credential is sent only to its own host. A clone URL on another host is refused
  when the repository is registered, and API calls outside the connection's base URL are refused.
  Redirects are not followed.

## Register a repository

**Settings → Repositories → Connect a remote repository**, or:

```http
POST /api/v1/projects/{project}/repositories
{"name": "tasks", "kind": "git", "location": "https://github.com/acme/tasks",
 "connection": "github", "default_branch": "main"}
```

The worker syncs it in the background. **Sync now** (`POST .../repositories/{id}/sync`) refreshes
the mirror on demand, and `sync_status` shows the result. Fetches do not prune, so unpublished
platform branches survive.

## Pull requests and CI

| Setting | Default | Meaning |
|---|---|---|
| `EXTERNAL_CI_POLL_S` | 60 | How often a run checks CI on its pull request |
| `EXTERNAL_CI_TIMEOUT_S` | 7200 | Longest a run waits before escalating |
| project `limits.require_external_ci` | `true` | `false` turns red or missing CI into a warning instead of a blocker |

While waiting, a run shows **waiting_external**. If a host reports no checks within five minutes, the
release is not blocked, but the approver sees a warning ("the host reported no checks"). Red CI
escalates the run and names the failing checks.

Publishing is idempotent. A branch that already holds the commit is not pushed again, and an
existing pull request for the branch is reused. A diverged remote branch is never overwritten.
