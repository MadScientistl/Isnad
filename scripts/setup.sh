#!/usr/bin/env bash
# One-command setup for a fresh clone. Idempotent: safe to run again.
#   scripts/setup.sh            full setup (database, Python, compile, web, seed)
#   ISNAD_SCHEMA=myschema scripts/setup.sh   seed another schema (default: demo)
#   SKIP_WEB=1 scripts/setup.sh              skip the Node install (API/engine only)
#   SKIP_DB=1 scripts/setup.sh               do not touch Docker; use the database at ISNAD_DSN (default
#                                            postgresql://isnad:isnad@localhost:55441/isnad), e.g. another checkout's
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
schema="${ISNAD_SCHEMA:-demo}"
dsn_port=55441

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found. $2"; }

say "Checking prerequisites"
if [ "${SKIP_DB:-0}" != "1" ]; then
  need docker "Install Docker Desktop or Docker Engine (https://docs.docker.com/get-docker/)."
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running. Start Docker and re-run."
  docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is not available."
fi
need uv "Install uv: https://docs.astral.sh/uv/getting-started/installation/"
if [ "${SKIP_WEB:-0}" != "1" ]; then
  need node "Install Node 20 or newer (https://nodejs.org/) or run with SKIP_WEB=1."
  need npm "npm ships with Node."
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  [ "$node_major" -ge 20 ] || die "Node $node_major found, Node 20+ is required."
fi
echo "docker, uv$([ "${SKIP_WEB:-0}" != "1" ] && echo ", node $(node -v)") ok"

if [ "${SKIP_DB:-0}" != "1" ]; then
say "Starting PostgreSQL 18 + PostGIS (docker compose, port $dsn_port)"
if docker compose ps --status running --services 2>/dev/null | grep -qx postgres; then
  echo "postgres service already running"
elif command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$dsn_port" -sTCP:LISTEN >/dev/null 2>&1; then
  die "port $dsn_port is already in use by another process. Stop it, or change the port mapping in docker-compose.yml and set ISNAD_DSN."
fi
docker compose up -d postgres

say "Waiting for the database to accept connections"
cid="$(docker compose ps -q postgres)"
[ -n "$cid" ] || die "the postgres container did not start (docker compose logs postgres)."
ready=0
for _ in $(seq 1 60); do
  if docker exec "$cid" pg_isready -U isnad -d isnad -q >/dev/null 2>&1 \
     && docker exec "$cid" psql -U isnad -d isnad -Atqc 'select 1' >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "$ready" = 1 ] || die "PostgreSQL did not become ready in 60 s (docker compose logs postgres)."
# The extensions (btree_gist, postgis) are created by the compiled schema DDL; nothing to do here.
echo "database ready"
fi

say "Installing Python dependencies (uv sync)"
uv sync

if [ "${SKIP_DB:-0}" = "1" ]; then
  say "Checking the database connection"
  uv run python -c "from engine.db import connect; connect().execute('select 1'); print('database reachable')" \
    || die "cannot connect to the database (ISNAD_DSN)."
fi

say "Compiling the ontology (DDL, OWL, SHACL, models, SDK)"
uv run python -m engine.compiler ontology/supply_chain.yaml build/ --sdk sdk/

if [ "${SKIP_WEB:-0}" != "1" ]; then
  say "Installing web dependencies"
  (cd web && { npm ci --no-audit --no-fund || npm install --no-audit --no-fund; })
fi

say "Seeding the demo data into schema '$schema' (idempotent)"
ISNAD_SCHEMA="$schema" uv run python -m engine.api.seed

cat <<MSG

Setup complete.

Next steps
  Run the app (API + UI):      scripts/dev.sh          (or: make dev)
      API  http://127.0.0.1:8000     UI  http://localhost:5173
      scripts/dev.sh --import  also enables ontology import (Ontology page, admin role)
  Run the scripted demo:       make demo               (about 30 s, headless, no API key needed)
  Run the tests:               make test-fast          (needs the database up)
  Optional: export ANTHROPIC_API_KEY=... to use a real Claude model for the agent and the generator.
MSG
