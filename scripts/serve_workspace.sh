#!/usr/bin/env bash
# Serve an imported workspace: scripts/serve_workspace.sh <slug> [port]   (default port 8001)
# One API process per workspace (own ontology + Postgres schema ws_<slug>). Writes workspaces/<slug>/api.json so the
# host's GET /workspaces reports the API base and the UI picker can switch to it. Ctrl-C stops it and removes api.json.
# The workspace process never imports, resets or seeds. Auth: export the same ISNAD_AUTH/ISNAD_AUTH_SECRET/ISNAD_USERS_FILE as the host.
set -euo pipefail
slug="${1:?usage: serve_workspace.sh <slug> [port]}"
port="${2:-8001}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
ws="${ISNAD_WORKSPACES_DIR:-workspaces}/$slug"
[[ "$slug" =~ ^[a-z][a-z0-9_]{0,60}$ ]] || { echo "invalid slug: $slug" >&2; exit 2; }
[[ -f "$ws/ontology.yaml" ]] || { echo "no imported workspace at $ws (expected $ws/ontology.yaml)" >&2; exit 2; }
build="$ws/build"
mkdir -p "$build"
printf '{"api_base":"http://127.0.0.1:%s"}\n' "$port" > "$ws/api.json"
trap 'rm -f "$ws/api.json"' EXIT INT TERM
echo "workspace $slug -> http://127.0.0.1:$port (schema ws_$slug)"
ISNAD_ONTOLOGY="$ws/ontology.yaml" ISNAD_SCHEMA="ws_$slug" ISNAD_BUILD_DIR="$build" \
ISNAD_ALLOW_IMPORT=0 ISNAD_ALLOW_RESET=0 ISNAD_OUTBOX_WORKER="${ISNAD_OUTBOX_WORKER:-1}" \
  uv run uvicorn engine.api.main:app --host 127.0.0.1 --port "$port"
