#!/usr/bin/env bash
# Run a command (as root) with only the capabilities the Helm chart grants the worker, read from
# the chart itself, so tests prove the sandbox works under exactly that set. CI uses this.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
chart_caps=$(grep -o 'add: \[[^]]*\]' "$here/infrastructure/helm/agentic-sdlc/templates/worker.yaml" \
  | grep -o '"[A-Z_]*"' | tr -d '"' | tr 'A-Z' 'a-z' | sed 's/^/cap_/')
[ -n "$chart_caps" ] || { echo "no capabilities found in the worker template" >&2; exit 2; }
drop=$(capsh --print | sed -n 's/^Bounding set =//p' | tr ',' '\n' | grep -vxF -f <(echo "$chart_caps") | paste -sd, -)
exec capsh --drop="$drop" -- -c 'exec "$@"' -- "$@"
