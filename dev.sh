#!/usr/bin/env bash
# ORQIVON local run: API, worker and web app together, with labelled output. Ctrl-C stops all.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT=$PWD
VENV=$ROOT/.venv
API=$ROOT/services/api
WEB=$ROOT/apps/web
ENV_FILE=$API/.env
LOGS=$ROOT/var/logs
PG_CONTAINER=orqivon-postgres

DEBUG=0
RUN_WEB=1
RUN_WORKER=1
API_PORT=${API_PORT:-8000}
WEB_PORT=${WEB_PORT:-5173}
API_DEBUG_PORT=${API_DEBUG_PORT:-5678}
WORKER_DEBUG_PORT=${WORKER_DEBUG_PORT:-5679}
WAIT_FOR_DEBUGGER=0

usage() {
  cat <<'EOF'
Usage: ./dev.sh [options]

Runs ORQIVON locally: the API (http://localhost:8000), the background worker and the web app
(http://localhost:5173). Output is labelled per process and also written to var/logs/.
Press Ctrl-C to stop everything. Run ./setup.sh first.

Options:
  --debug           debug mode: DEBUG-level logs, and a Python debugger (debugpy) listening for
                    the API on port 5678 and the worker on port 5679. Attach from VS Code with the
                    "Attach to API" / "Attach to worker" configurations, or any DAP client.
                    The API does not auto-reload in debug mode (reloading drops the debugger).
  --wait            with --debug: start the API and worker only once a debugger has attached
  --sql             log every database query (SQL_ECHO=true); combine with --debug or not
  --no-web          do not start the web app
  --no-worker       do not start the worker (runs will wait in the queue)
  --api-port N      API port (default 8000; also API_PORT)
  --web-port N      web app port (default 5173; also WEB_PORT)
  -h, --help        show this help
EOF
}

while (($#)); do
  case $1 in
    --debug) DEBUG=1 ;;
    --wait) WAIT_FOR_DEBUGGER=1 ;;
    --sql) export SQL_ECHO=true ;;
    --no-web) RUN_WEB=0 ;;
    --no-worker) RUN_WORKER=0 ;;
    --api-port) API_PORT=${2:?--api-port needs a value}; shift ;;
    --web-port) WEB_PORT=${2:?--web-port needs a value}; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
if ((WAIT_FOR_DEBUGGER && !DEBUG)); then echo "--wait only applies with --debug" >&2; exit 2; fi

if [[ -t 1 ]]; then
  B=$'\033[1m' D=$'\033[2m' G=$'\033[32m' Y=$'\033[33m' R=$'\033[31m' N=$'\033[0m'
  C_API=$'\033[36m' C_WORKER=$'\033[35m' C_WEB=$'\033[34m'
else
  B='' D='' G='' Y='' R='' N='' C_API='' C_WORKER='' C_WEB=''
fi
die() { printf '%s%s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# ---------------------------------------------------------------- pre-flight
[[ -x $VENV/bin/agentic-sdlc && -d $WEB/node_modules && -f $ENV_FILE ]] ||
  die "This checkout is not set up yet. Run ./setup.sh first."

env_get() { sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1; }
DATABASE_URL=${DATABASE_URL:-$(env_get DATABASE_URL)}
SANDBOX=${SANDBOX_BACKEND:-$(env_get SANDBOX_BACKEND)}
SANDBOX=${SANDBOX:-namespace}

db_ping() {
  (cd "$API" && "$VENV/bin/python" - <<'EOF' >/dev/null 2>&1)
import psycopg
from sqlalchemy.engine import make_url
from agentic_sdlc.core.settings import get_settings
u = make_url(get_settings().database_url)
psycopg.connect(host=u.host, port=u.port or 5432, user=u.username, password=u.password, dbname=u.database, connect_timeout=3).close()
EOF
}
if ! db_ping; then
  if command -v docker >/dev/null && docker container inspect "$PG_CONTAINER" >/dev/null 2>&1; then
    echo "Starting PostgreSQL ($PG_CONTAINER)…"
    docker start "$PG_CONTAINER" >/dev/null
    for _ in $(seq 1 30); do db_ping && break; sleep 1; done
  fi
  db_ping || die "PostgreSQL is not reachable (DATABASE_URL in services/api/.env). Start it, or run ./setup.sh."
fi

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
for p in "$API_PORT" $( ((RUN_WEB)) && echo "$WEB_PORT") $( ((DEBUG)) && echo "$API_DEBUG_PORT $WORKER_DEBUG_PORT"); do
  port_busy "$p" && die "Port $p is already in use (another ./dev.sh?). Stop it or choose another port."
done

# Keep the schema current after a git pull; a no-op when nothing changed.
(cd "$API" && "$VENV/bin/alembic" upgrade head >/dev/null 2>&1) ||
  die "Database migration failed: cd services/api && ../../.venv/bin/alembic upgrade head"

# The worker needs root for the namespace sandbox on Linux; ask for sudo once, up front.
WORKER_SUDO=()
if ((RUN_WORKER)) && [[ $SANDBOX == namespace && $(uname -s) == Linux && $EUID -ne 0 ]]; then
  echo "The worker runs generated tests in a Linux namespace sandbox, which needs root."
  if sudo -v; then
    WORKER_SUDO=(sudo -E env "PATH=$PATH")
  else
    printf '%sNo sudo: the worker runs without root, so runs that execute tests will fail.%s\n' "$Y" "$N"
  fi
fi

# ---------------------------------------------------------------- processes
mkdir -p "$LOGS"
export PYTHONUNBUFFERED=1 LOG_FORMAT=${LOG_FORMAT:-text}
if ((DEBUG)); then export LOG_LEVEL=DEBUG; else export LOG_LEVEL=${LOG_LEVEL:-INFO}; fi

# ${a[@]+"${a[@]}"} expands an empty array safely under `set -u` on bash 3.2 (macOS).
WAITFLAG=()
((WAIT_FOR_DEBUGGER)) && WAITFLAG=(--wait-for-client)
if ((DEBUG)); then
  # -Xfrozen_modules=off: otherwise breakpoints in some standard-library code may be missed.
  API_CMD=("$VENV/bin/python" -Xfrozen_modules=off -m debugpy --listen "127.0.0.1:$API_DEBUG_PORT" ${WAITFLAG[@]+"${WAITFLAG[@]}"}
    "$VENV/bin/agentic-sdlc" api --port "$API_PORT")
  WORKER_CMD=(${WORKER_SUDO[@]+"${WORKER_SUDO[@]}"} "$VENV/bin/python" -Xfrozen_modules=off -m debugpy
    --listen "127.0.0.1:$WORKER_DEBUG_PORT" ${WAITFLAG[@]+"${WAITFLAG[@]}"} "$VENV/bin/agentic-sdlc" worker)
else
  API_CMD=("$VENV/bin/agentic-sdlc" api --port "$API_PORT" --reload)
  WORKER_CMD=(${WORKER_SUDO[@]+"${WORKER_SUDO[@]}"} "$VENV/bin/agentic-sdlc" worker)
fi
WEB_CMD=(npm run dev --silent -- --port "$WEB_PORT" --strictPort)

set -m # each background job gets its own process group, so stopping one stops its children
PGIDS=()
NAMES=()
SUDO_PGIDS=()

label() { # prefix each line with the process name; also append it to var/logs/<name>.log
  # A shell loop rather than awk: some awks (mawk, Debian/Ubuntu's default) buffer their input,
  # which would hold log lines back until kilobytes have accumulated.
  local name=$1 color=$2 line prefix
  prefix="${color}$(printf '%-6s' "$name")${N}${D}│${N} "
  exec 3>"$LOGS/$name.log"
  while IFS= read -r line || [[ -n $line ]]; do
    printf '%s%s\n' "$prefix" "$line"
    printf '%s\n' "$line" >&3
  done
}

start() { # start NAME COLOR DIR CMD...
  local name=$1 color=$2 dir=$3; shift 3
  # No terminal input: a background process that reads the terminal (vite does) is suspended.
  (cd "$dir" && exec "$@" </dev/null) 2>&1 | label "$name" "$color" &
  local pgid
  pgid=$(ps -o pgid= -p $! | tr -d ' ')
  disown # stopped by process group below; keeps bash from printing job status lines
  PGIDS+=("$pgid"); NAMES+=("$name")
  if [[ $name == worker && -n ${WORKER_SUDO[*]-} ]]; then SUDO_PGIDS+=("$pgid"); fi
  return 0
}

STOPPING=0
stop_all() {
  ((STOPPING)) && return; STOPPING=1
  printf '\n%sStopping…%s\n' "$D" "$N"
  local pg alive
  for pg in ${PGIDS[@]+"${PGIDS[@]}"}; do kill -TERM -- "-$pg" 2>/dev/null || true; done
  for pg in ${SUDO_PGIDS[@]+"${SUDO_PGIDS[@]}"}; do sudo -n kill -TERM -- "-$pg" 2>/dev/null || true; done
  for _ in $(seq 1 50); do
    alive=0
    for pg in ${PGIDS[@]+"${PGIDS[@]}"}; do kill -0 -- "-$pg" 2>/dev/null && alive=1; done
    ((alive)) || break
    sleep 0.1
  done
  for pg in ${PGIDS[@]+"${PGIDS[@]}"}; do kill -KILL -- "-$pg" 2>/dev/null || true; done
  for pg in ${SUDO_PGIDS[@]+"${SUDO_PGIDS[@]}"}; do sudo -n kill -KILL -- "-$pg" 2>/dev/null || true; done
}
trap 'stop_all; exit 130' INT TERM
trap 'stop_all' EXIT

start api "$C_API" "$API" "${API_CMD[@]}"
((RUN_WORKER)) && start worker "$C_WORKER" "$API" "${WORKER_CMD[@]}"
export API_URL="http://127.0.0.1:$API_PORT" # where the web dev server proxies /api
((RUN_WEB)) && start web "$C_WEB" "$WEB" "${WEB_CMD[@]}"

# ---------------------------------------------------------------- banner
for _ in $(seq 1 120); do
  curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health" >/dev/null 2>&1 && break
  ((WAIT_FOR_DEBUGGER)) && break
  sleep 0.5
done
{
  printf '\n%sORQIVON is running%s%s\n' "$B$G" "$( ((DEBUG)) && echo ' in debug mode')" "$N"
  ((RUN_WEB)) && printf '  Web app      %shttp://localhost:%s%s\n' "$B" "$WEB_PORT" "$N"
  printf '  API          http://localhost:%s   (docs: /api/docs)\n' "$API_PORT"
  printf '  Worker       %s\n' "$( ((RUN_WORKER)) && echo "running, sandbox: $SANDBOX" || echo 'not started (--no-worker)')"
  if ((DEBUG)); then
    printf '  Debugger     API on 127.0.0.1:%s, worker on 127.0.0.1:%s (debugpy)%s\n' "$API_DEBUG_PORT" "$WORKER_DEBUG_PORT" \
      "$( ((WAIT_FOR_DEBUGGER)) && echo ' — waiting for you to attach')"
    printf '  Logs         DEBUG level\n'
  fi
  printf '  Sign in      with a development token, e.g. dev_owner, dev_engineer, dev_reviewer, dev_approver\n'
  printf '  Log files    var/logs/{api,worker,web}.log        Stop: Ctrl-C\n\n'
} >&2

# Stop everything as soon as any process exits on its own.
while true; do
  for i in "${!PGIDS[@]}"; do
    if ! kill -0 -- "-${PGIDS[$i]}" 2>/dev/null; then
      printf '%s%s stopped; stopping the others. See var/logs/%s.log%s\n' "$R" "${NAMES[$i]}" "${NAMES[$i]}" "$N" >&2
      exit 1
    fi
  done
  sleep 1
done
