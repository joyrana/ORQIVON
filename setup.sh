#!/usr/bin/env bash
# ORQIVON local setup: dependencies, configuration, database, sandbox, schema and demo accounts.
# Safe to run again; each step is skipped when it is already done. Then run ./dev.sh.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT=$PWD
VENV=$ROOT/.venv
API=$ROOT/services/api
WEB=$ROOT/apps/web
ENV_FILE=$API/.env
PG_CONTAINER=orqivon-postgres

USE_DOCKER=1
SKIP_SANDBOX=0
SKIP_DB=0

usage() {
  cat <<'EOF'
Usage: ./setup.sh [options]

Prepares this checkout for local development:
  1. checks prerequisites (Python 3.12+, Node 22+, git; on Linux also unshare)
  2. installs the Python environment (.venv) and the web app's packages
  3. creates services/api/.env from .env.example (an existing file is kept)
  4. makes sure PostgreSQL is reachable, starting it in Docker if needed, and creates the
     development and test databases
  5. prepares the code sandbox (Linux: root-owned toolchain and sandbox user, asks for sudo;
     otherwise Docker)
  6. applies database migrations and creates the development sign-in accounts

Options:
  --no-docker      never start PostgreSQL or the sandbox in Docker
  --skip-db        skip steps 4 and 6 (use when the database is managed elsewhere)
  --skip-sandbox   skip step 5
  -h, --help       show this help
EOF
}

for arg in "$@"; do
  case $arg in
    --no-docker) USE_DOCKER=0 ;;
    --skip-db) SKIP_DB=1 ;;
    --skip-sandbox) SKIP_SANDBOX=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then B=$'\033[1m' G=$'\033[32m' Y=$'\033[33m' R=$'\033[31m' N=$'\033[0m'; else B='' G='' Y='' R='' N=''; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok() { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die() { printf '\n%sSetup stopped:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
trap 'die "a command failed on line $LINENO (see the output above)"' ERR
mkdir -p "$ROOT/var/logs"
SETUP_LOG=$ROOT/var/logs/setup.log
: >"$SETUP_LOG"
quietly() { # run a noisy command; show its output only if it fails
  if ! "$@" >>"$SETUP_LOG" 2>&1; then
    tail -n 40 "$SETUP_LOG" >&2
    die "'$*' failed (full output in var/logs/setup.log)"
  fi
}

OS=$(uname -s)

# ---------------------------------------------------------------- 1. prerequisites
step "Checking prerequisites"
PYTHON=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null &&
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PYTHON=$(command -v "$candidate"); break
  fi
done
[[ -n $PYTHON ]] || die "Python 3.12 or newer is required (https://www.python.org/downloads/)."
ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())')"

command -v node >/dev/null || die "Node.js 22 or newer is required (https://nodejs.org/)."
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
((NODE_MAJOR >= 22)) || die "Node.js 22 or newer is required; found $(node --version)."
command -v npm >/dev/null || die "npm is required (it ships with Node.js)."
ok "Node $(node --version)"
command -v git >/dev/null || die "git is required."
ok "git $(git --version | awk '{print $3}')"
if [[ $OS == Linux ]]; then
  command -v unshare >/dev/null || die "unshare is required on Linux (package util-linux)."
  ok "unshare (util-linux)"
fi
DOCKER_OK=0
if ((USE_DOCKER)) && command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  DOCKER_OK=1; ok "Docker is available"
fi

# ---------------------------------------------------------------- 2. dependencies
step "Installing dependencies"
if [[ ! -x $VENV/bin/python ]]; then
  "$PYTHON" -m venv "$VENV"
fi
STAMP=$VENV/.orqivon-deps
WANT=$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$API/pyproject.toml")
if [[ ! -f $STAMP || $(cat "$STAMP") != "$WANT" ]]; then
  echo "  Installing Python packages (a minute or two the first time)…"
  quietly "$VENV/bin/python" -m pip install --upgrade pip
  quietly "$VENV/bin/python" -m pip install -e "${API}[dev]"
  echo "$WANT" >"$STAMP"
  ok "Python packages installed"
else
  ok "Python packages up to date"
fi
if [[ ! -d $WEB/node_modules || $WEB/package-lock.json -nt $WEB/node_modules/.package-lock.json ]]; then
  echo "  Installing web packages…"
  quietly npm --prefix "$WEB" ci --no-audit --no-fund
  ok "Web packages installed"
else
  ok "Web packages up to date"
fi

# ---------------------------------------------------------------- 3. configuration
step "Configuration"
if [[ ! -f $ENV_FILE ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  ok "Created services/api/.env from .env.example"
else
  ok "Keeping existing services/api/.env"
fi

env_get() { # value of KEY in .env (last assignment wins), empty if unset
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1
}
env_set() { # set KEY=VALUE in .env, replacing an existing assignment
  local key=$1 value=$2 tmp
  tmp=$(mktemp)
  grep -v "^[[:space:]]*$key=" "$ENV_FILE" >"$tmp" || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  cat "$tmp" >"$ENV_FILE" && rm -f "$tmp"
}

DATABASE_URL=$(env_get DATABASE_URL)
DATABASE_URL=${DATABASE_URL:-postgresql+psycopg://sdlc:sdlc@localhost:5432/sdlc}
# host port user password database (from the SQLAlchemy URL)
IFS='|' read -r DB_HOST DB_PORT DB_USER DB_PASS DB_NAME < <("$VENV/bin/python" - "$DATABASE_URL" <<'EOF'
import sys
from sqlalchemy.engine import make_url
u = make_url(sys.argv[1])
print("|".join(str(v) for v in (u.host or "localhost", u.port or 5432, u.username or "", u.password or "", u.database or "")))
EOF
)
TEST_DB=${DB_NAME}_test

db_ping() { # exit 0 when DATABASE_URL accepts connections
  "$VENV/bin/python" - "$DATABASE_URL" <<'EOF' >/dev/null 2>&1
import sys
import psycopg
from sqlalchemy.engine import make_url
u = make_url(sys.argv[1])
psycopg.connect(host=u.host, port=u.port or 5432, user=u.username, password=u.password, dbname=u.database, connect_timeout=3).close()
EOF
}

# ---------------------------------------------------------------- 4. database
if ((SKIP_DB)); then
  step "Database (skipped)"
else
  step "Database"
  # ok | created | nodb | auth | down: tells "server not running" apart from "wrong login" and
  # "database missing", and creates a missing database when the role may (CREATEDB).
  DB_STATE=$("$VENV/bin/python" - "$DATABASE_URL" <<'EOF' 2>/dev/null || echo down
import sys
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
u = make_url(sys.argv[1])
kw = dict(host=u.host, port=u.port or 5432, user=u.username, password=u.password, connect_timeout=3)
try:
    psycopg.connect(dbname=u.database, **kw).close()
    print("ok")
except psycopg.OperationalError as e:
    msg = str(e)
    if "does not exist" in msg and "database" in msg:
        try:
            with psycopg.connect(dbname="postgres", autocommit=True, **kw) as c:
                c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(u.database)))
            print("created")
        except psycopg.Error:
            print("nodb")
    elif "authentication failed" in msg or "role" in msg:
        print("auth")
    else:
        print("down")
EOF
)
  case $DB_STATE in
    ok) ok "PostgreSQL reachable at $DB_HOST:$DB_PORT (database $DB_NAME)" ;;
    created) ok "Created database $DB_NAME on $DB_HOST:$DB_PORT" ;;
    nodb) die "PostgreSQL is running at $DB_HOST:$DB_PORT but database '$DB_NAME' does not exist, and user '$DB_USER' may not create it.
  Create it with:  sudo -u postgres createdb -O $DB_USER $DB_NAME" ;;
    auth) die "PostgreSQL at $DB_HOST:$DB_PORT refused the login for user '$DB_USER'.
  Create the role:  sudo -u postgres psql -c \"CREATE ROLE $DB_USER LOGIN CREATEDB PASSWORD '$DB_PASS'\"
  or correct DATABASE_URL in services/api/.env." ;;
  esac
  if [[ $DB_STATE == down ]] && [[ $DB_HOST =~ ^(localhost|127\.0\.0\.1)$ ]] && ((DOCKER_OK)); then
    if docker container inspect "$PG_CONTAINER" >/dev/null 2>&1; then
      docker start "$PG_CONTAINER" >/dev/null
      ok "Started existing container $PG_CONTAINER"
    else
      docker run -d --name "$PG_CONTAINER" --restart unless-stopped \
        -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD="$DB_PASS" -e POSTGRES_DB="$DB_NAME" \
        -p "127.0.0.1:$DB_PORT:5432" -v orqivon-pgdata:/var/lib/postgresql/data postgres:16 >/dev/null
      ok "Started PostgreSQL 16 in Docker (container $PG_CONTAINER, data in volume orqivon-pgdata)"
    fi
    for _ in $(seq 1 60); do db_ping && break; sleep 1; done
    db_ping || die "PostgreSQL in Docker did not become ready (docker logs $PG_CONTAINER)."
    ok "PostgreSQL ready"
  elif [[ $DB_STATE == down ]]; then
    die "PostgreSQL is not running at $DB_HOST:$DB_PORT.
  Start PostgreSQL 16 and create the role and database, for example:
    sudo -u postgres psql -c \"CREATE ROLE $DB_USER LOGIN CREATEDB PASSWORD '$DB_PASS'\"
    sudo -u postgres createdb -O $DB_USER $DB_NAME
  or install Docker and run ./setup.sh again, or point DATABASE_URL in services/api/.env at your server."
  fi
  # The test suite (make test-api) uses <database>_test on the same server.
  if "$VENV/bin/python" - "$DATABASE_URL" "$TEST_DB" <<'EOF'
import sys
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
u = make_url(sys.argv[1])
with psycopg.connect(host=u.host, port=u.port or 5432, user=u.username, password=u.password, dbname=u.database, autocommit=True) as c:
    if c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (sys.argv[2],)).fetchone() is None:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(sys.argv[2])))
EOF
  then
    ok "Test database $TEST_DB ready"
  else
    warn "Could not create the test database $TEST_DB (the role needs CREATEDB); make test-api will need it."
  fi
fi

# ---------------------------------------------------------------- 5. sandbox
SANDBOX=$(env_get SANDBOX_BACKEND)
SANDBOX=${SANDBOX:-namespace}
if ((SKIP_SANDBOX)); then
  step "Code sandbox (skipped)"
else
  step "Code sandbox"
  sandbox_ready() { [[ -x /opt/asw-sandbox/bin/python ]] && id sandbox >/dev/null 2>&1; }
  if [[ $OS == Linux && $SANDBOX == namespace ]]; then
    if sandbox_ready; then
      ok "Namespace sandbox toolchain present (/opt/asw-sandbox, user sandbox)"
    elif [[ $EUID -eq 0 ]]; then
      quietly make --no-print-directory sandbox-toolchain
      ok "Installed the namespace sandbox toolchain"
    elif command -v sudo >/dev/null; then
      echo "  The sandbox that runs generated tests needs a root-owned toolchain and a 'sandbox' user."
      echo "  sudo will ask for your password once:"
      if sudo make --no-print-directory sandbox-toolchain >/dev/null; then
        ok "Installed the namespace sandbox toolchain"
      elif ((DOCKER_OK)); then
        env_set SANDBOX_BACKEND docker; SANDBOX=docker
        warn "Could not install it; using the Docker sandbox instead (SANDBOX_BACKEND=docker)."
      else
        warn "Could not install it. Runs that execute tests will fail until: sudo make sandbox-toolchain"
      fi
    elif ((DOCKER_OK)); then
      env_set SANDBOX_BACKEND docker; SANDBOX=docker
      ok "No sudo here; using the Docker sandbox (SANDBOX_BACKEND=docker)"
    else
      warn "No sudo or Docker: runs that execute tests will fail until the sandbox is set up."
    fi
  elif [[ $SANDBOX == namespace ]]; then # macOS and others: no Linux namespaces
    if ((DOCKER_OK)); then
      env_set SANDBOX_BACKEND docker; SANDBOX=docker
      ok "Using the Docker sandbox on $OS (SANDBOX_BACKEND=docker)"
    else
      warn "The namespace sandbox needs Linux. Install Docker and run ./setup.sh again so tests can run."
    fi
  else
    ok "Sandbox backend: $SANDBOX"
  fi
  if [[ $SANDBOX == docker ]] && ((DOCKER_OK)); then
    image=$(env_get SANDBOX_DOCKER_IMAGE); image=${image:-python:3.12-slim}
    docker image inspect "$image" >/dev/null 2>&1 || docker pull --quiet "$image" >/dev/null
    ok "Sandbox image $image available"
  fi
fi

# ---------------------------------------------------------------- 6. schema and accounts
if ((SKIP_DB == 0)); then
  step "Database schema and development accounts"
  (cd "$API" && quietly "$VENV/bin/alembic" upgrade head)
  ok "Migrations applied"
  (cd "$API" && quietly "$VENV/bin/agentic-sdlc" seed)
  ok "Development sign-in accounts ready (tokens are listed when you run ./dev.sh)"
fi

trap - ERR
printf '\n%sSetup complete.%s Start everything with:  %s./dev.sh%s   (or ./dev.sh --debug)\n' "$G" "$N" "$B" "$N"
