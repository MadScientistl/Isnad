#!/usr/bin/env bash
# Run the API (127.0.0.1:8000) and the UI (Vite, port 5173) together. Ctrl-C stops both. Logs: var/log/{api,web}.log
#   scripts/dev.sh              API on schema 'demo' (ISNAD_SCHEMA), role switcher, simulation controllable from the UI
#   scripts/dev.sh --import     also ISNAD_ALLOW_IMPORT=1: ontology import wizard and manager (admin role, auth off)
# Environment you may set: ISNAD_SCHEMA (demo), API_PORT (8000), UI_PORT (5173), ANTHROPIC_API_KEY (real agent), ISNAD_AUTH=1 (+ secret/users)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
allow_import=0
for a in "$@"; do
  case "$a" in
    --import) allow_import=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
api_port="${API_PORT:-8000}"
ui_port="${UI_PORT:-5173}"
export ISNAD_SCHEMA="${ISNAD_SCHEMA:-demo}"
export ISNAD_ALLOW_IMPORT="$allow_import"
mkdir -p var/log

[ -d web/node_modules ] || { echo "web/node_modules missing: run scripts/setup.sh first" >&2; exit 1; }
[ -f build/schema.sql ] || { echo "build/ missing: run scripts/setup.sh first" >&2; exit 1; }
if lsof -nP -iTCP:"$api_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $api_port is already in use (another API or a Docker service). Free it or set API_PORT." >&2; exit 1
fi
if lsof -nP -iTCP:"$ui_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $ui_port is already in use (another Vite server?). Stop it or set UI_PORT (the API allows CORS only from 5173 by default)." >&2; exit 1
fi

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo; echo "stopping..."
  for p in "${pids[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
  # uvicorn/vite spawn children: give them a moment, then clean up stragglers of our process group
  sleep 1
  for p in "${pids[@]:-}"; do [ -n "$p" ] && pkill -P "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "API  http://127.0.0.1:$api_port   (schema $ISNAD_SCHEMA, import=$allow_import)  log var/log/api.log"
uv run uvicorn engine.api.main:app --host 127.0.0.1 --port "$api_port" >var/log/api.log 2>&1 &
pids+=("$!")
echo "UI   http://localhost:$ui_port                                  log var/log/web.log"
(cd web && VITE_API="http://127.0.0.1:$api_port" exec npm run dev -- --port "$ui_port" --strictPort) >var/log/web.log 2>&1 &
pids+=("$!")

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$api_port/health" >/dev/null 2>&1 && { echo "API is up. Open http://localhost:$ui_port (Ctrl-C to stop)"; break; }
  kill -0 "${pids[0]}" 2>/dev/null || { echo "API exited; see var/log/api.log" >&2; tail -20 var/log/api.log >&2; exit 1; }
  sleep 1
done
# bash 3.2 (macOS) has no `wait -n`: poll until one of the two processes exits
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do sleep 1; done
echo "a process exited; see var/log/"
