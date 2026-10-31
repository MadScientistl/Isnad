# Isnad

*Every decision, traceable.* Isnad (إسناد, the chain of transmission) is an open-source, Foundry/AIP-inspired ontology
platform. You describe a domain once in a YAML ontology; Isnad compiles it into a bitemporal Postgres/PostGIS schema, an
OWL + SHACL semantic layer, an API and a TypeScript SDK, and runs on top of it a transactional action engine (every
write is an audited action), row-level security per role, a role-scoped AI agent that can only *propose*, what-if
scenarios, and an import pipeline that proposes a new ontology from your CSV/SQLite/Postgres data and lets a human review
it before anything is loaded. The bundled use case is a simulated supply chain (ports, shipments, invoices); the
design and its sources are in `RESEARCH.md`, the frozen interfaces in `docs/contracts.md`.

Status: all six phases are built (M0-M11, governance and actions, runtime agent, scenarios and evals, ontology import).
It is a research-grade prototype: read **Known limitations** before exposing it to anyone.

## Architecture

```mermaid
flowchart TB
  subgraph authoring["Authoring"]
    YAML["YAML ontology<br/>types, links, actions, roles, kpis"]
    COMP["Compiler (engine/compiler)"]
    YAML --> COMP
    COMP --> DDL["Postgres DDL<br/>bitemporal tables, RLS, views"]
    COMP --> OWL["OWL + SHACL (Turtle)"]
    COMP --> API["API routes + Pydantic models"]
    COMP --> SDK["TypeScript SDK"]
  end

  subgraph truth["Source of truth"]
    PG[("PostgreSQL 18 + PostGIS<br/>instance data (bitemporal), scenarios,<br/>audit, proposals, outbox<br/>FORCE RLS + masking views")]
  end
  DDL --> PG

  subgraph derived["Derived semantic layer"]
    OX[("Oxigraph<br/>ontology, ids, edges,<br/>inferred edges (flagged)")]
    RS["Reasoner OWL 2 RL (reasonable)<br/>SHACL (pySHACL)"]
    PG -- "graph_outbox sync / rebuild" --> OX
    OWL --> OX
    OX --- RS
  end

  subgraph runtime["Runtime (FastAPI)"]
    ACT["Action engine<br/>criteria, rules, dry run,<br/>execute / propose / approve / rollback"]
    AUD["Append-only audit"]
    OUT["Webhook outbox (after commit)"]
    AGT["Runtime agent<br/>role-scoped tools, caps,<br/>proposals only"]
    SCN["Scenarios / what-if<br/>copy-on-write overlays, sim"]
    ACT --> AUD
    ACT --> OUT
    AGT -- "propose_action" --> ACT
    SCN --> ACT
  end
  API --> runtime
  ACT -- "one transaction" --> PG
  runtime -- "reads as the requesting role (RLS)" --> PG
  runtime --> OX

  UI["React UI<br/>role switcher, map, graph, explorer,<br/>approvals, audit, agent, scenarios, ontology"] --> runtime
  SDK --> UI

  subgraph ingest["Ontology import pipeline (admin)"]
    SRC["CSV / zip / SQLite / named Postgres source"]
    PRF["Profiler<br/>types, keys, FKs, geo"]
    PRO["Claude proposal<br/>(or heuristic baseline)"]
    VAL["Scratch-schema validation<br/>DDL, keys, coercion, links,<br/>SHACL, reasoner"]
    REV["Human review + approval"]
    VYAML["Versioned YAML + mapping"]
    IMP["Audited import<br/>one import_dataset audit row"]
    WS["Workspace<br/>schema ws_slug + own API process"]
    SRC --> PRF --> PRO --> VAL --> REV --> VYAML --> IMP --> WS
  end
  UI --> ingest
  IMP --> PG
  VYAML -. "same compiler" .-> COMP
```

ASCII fallback:

```
                      YAML ontology (single authoring source)
                                   | compiler
        +--------------------------+---------------------------+
        v                          v                           v
  Postgres DDL              OWL + SHACL (Turtle)        API routes / models / TS SDK
        |                          |                           |
 +------v------------+      +------v-----------------+         |
 | PostgreSQL+PostGIS|      | Oxigraph (derived,     |         |
 | SOURCE OF TRUTH   |----->| rebuildable)           |         |
 | bitemporal data   | sync | ontology, ids, edges,  |         |
 | scenarios, audit  |      | inferred edges flagged |         |
 | proposals, outbox |      | + OWL 2 RL, SHACL      |         |
 | FORCE RLS+masking |      +------------------------+         |
 +------^------------+                                         |
        | one transaction                                      |
 +------+---------------------------------------------------+  |
 | Runtime (FastAPI)  <-----------------------------------------+
 |  action engine (criteria, rules, dry run, execute /       |
 |    propose / approve / rollback) -> append-only audit     |
 |    -> webhook outbox (after commit)                       |
 |  runtime agent (role-scoped tools, proposals only)        |
 |  scenarios / what-if (copy-on-write overlays, simulation) |
 +------^----------------------------------------------^----+
        |                                              |
   React UI (role switcher)            Import pipeline (admin):
                                       source -> profiler -> Claude proposal
                                       -> scratch-schema validation -> human
                                       review -> versioned YAML -> audited
                                       import -> workspace (ws_<slug>)
```

More detail per component: [docs/architecture.md](docs/architecture.md).

## Prerequisites

- Docker with Compose v2 (runs PostgreSQL 18 + PostGIS; the image is built locally, it works on arm64 and amd64)
- [uv](https://docs.astral.sh/uv/) (Python 3.12+; the project has been run on CPython 3.14)
- Node 20+ and npm (for the UI)
- Optional: `ANTHROPIC_API_KEY` for a real Claude model in the agent and in the ontology generator. Everything else, including
  the demo and the test suite, runs without a key (scripted mock model).

## Quick start

```bash
git clone <this repository> isnad && cd isnad
make setup          # = scripts/setup.sh: docker compose up, wait for Postgres, uv sync, compile, npm ci, seed
make dev            # API http://127.0.0.1:8000 and UI http://localhost:5173 (Ctrl-C stops both)
```

Open <http://localhost:5173> and pick a role in the role switcher (top right). To use the ontology import wizard, start with
`make dev-import` (= `scripts/dev.sh --import`) and switch to the `admin` role; an **Ontology** entry appears in the menu.
`setup.sh` is idempotent. `SKIP_WEB=1` skips Node; `SKIP_DB=1` uses an existing database at `ISNAD_DSN`;
`ISNAD_SCHEMA=name` seeds another schema. If port 8000 or 5173 is taken (another Docker service often is), set
`API_PORT` / `UI_PORT`; the API only accepts browser calls from `http://localhost:5173`, so the default UI port is
the one to keep. `scripts/dev.sh` binds the API to `127.0.0.1` only.

Without `make`, the same steps by hand:

```bash
docker compose up -d postgres
uv sync
uv run python -m engine.compiler ontology/supply_chain.yaml build/ --sdk sdk/
(cd web && npm ci)
ISNAD_SCHEMA=demo uv run python -m engine.api.seed
ISNAD_SCHEMA=demo uv run uvicorn engine.api.main:app --host 127.0.0.1 --port 8000     # terminal 1
(cd web && VITE_API=http://127.0.0.1:8000 npm run dev)                                  # terminal 2
```

The simulation does not auto-run: use the time bar in the UI (admin role) to start it, or run it headless:
`ISNAD_SCHEMA=demo uv run python -m sim --seed 1 --days 30`. Offline UI development without a backend: `cd web && VITE_MOCK=1 npm run dev`.

### See it work without the UI

```bash
make demo           # scripted end-to-end demo, ~20-30 s, deterministic, no key, uses throw-away schemas
```

It seeds the supply chain, simulates 7 days with an injected port closure, lets the planner's (scripted) agent create a
proposal, approves it as admin and shows the audit row, forks a what-if scenario and compares KPIs, reads the history of the
closed port, then imports the bike-share dataset and reads the new workspace back. See [docs/demo.md](docs/demo.md) for the
output, the options (`--model mock|live`, `--live`) and a click path for a live presentation.

## Feature tour

| Area | What you get | Where |
|---|---|---|
| Ontology | One YAML: object/link types, classes, actions, roles, KPIs, sim block | `ontology/supply_chain.yaml`, `engine/compiler/` |
| Bitemporal data | Valid time x recorded time, as-of reads, history, diffs; corrections never overwrite | `engine/db/` |
| Geospatial | PostGIS points/lines, bbox queries, map page | `/geo/{type}`, `web/src/pages/MapPage.tsx` |
| Graph + reasoning | Oxigraph derived from Postgres; OWL 2 RL inference (flagged, with provenance); SHACL; per-node/edge permission filtering | `engine/sync/`, `engine/reason/` |
| Actions | Typed params, criteria, declarative rules, dry run, approval gate, rollback, webhook outbox; the only write path | `engine/actions/` |
| Governance | Roles in YAML, FORCE RLS, masking views, append-only audit, approvals inbox | `engine/db/governance.sql`, UI Approvals/Audit |
| Runtime agent | Role-scoped tools, caps, injection handling, proposals with evidence and expiry, run history, SSE console | `agent/`, UI Agent |
| Simulation | Seeded SimPy supply chain writing through the action engine; injections (port closure, delays, price shock) | `sim/` |
| Scenarios | Copy-on-write what-if forks, compare, merge, discard, what-if simulation | `engine/scenarios/`, UI Scenarios |
| Evals | 39-case agent suite (mock and live), ontology-generator evals with baseline and cassettes | `evals/` |
| Ontology versioning | Additive-only versions, diffs with sensitive-change detection, approval | `engine/versioning/`, UI Ontology |
| Ontology import | Profiler, Claude proposal, scratch-schema validation, review, audited import, workspaces | `engine/ingest/`, `agent/ontogen/` |

## Walkthrough: import a dataset

Two datasets are bundled in `evals/ontogen/datasets/`: `bikeshare` (7 related CSV tables: stations with coordinates,
bikes, riders, trips, maintenance, zones, clusters) and `messy_supply` (supply-chain CSVs with deliberately dirty values:
date formats, duplicates, odd enum spellings). A third, `injection`, contains hostile text for testing the generator's
defences; do not expect a useful ontology from it.

1. `make dev-import`, open <http://localhost:5173>, switch to **admin**, open **Ontology**.
2. Zip the dataset: `cd evals/ontogen/datasets/bikeshare && zip -q /tmp/bikeshare.zip *.csv`, and upload it (a single CSV,
   TSV, a zip of CSVs or a SQLite file also work; a named Postgres source is configured server side with `ISNAD_IMPORT_SOURCES`).
3. The profile shows tables, row counts, inferred types, key candidates, foreign-key candidates with confidence and geo columns.
4. Create a proposal. **Baseline** is a heuristic with no model and works without a key. **Model** needs
   `ANTHROPIC_API_KEY`; without one the API answers 503 `agent_unavailable` and baseline stays available.
5. Read the validation report. It loads the data into a throw-away schema `gen_<hex>` and checks DDL, keys, nulls, type
   coercion, enums, link resolution (at least 95 percent), SHACL and the reasoner. Items can be accepted, rejected or edited; every edit is re-validated.
6. Approve (a comment is required when permissions are widened) and run the import. One transaction creates the schema
   `ws_<slug>`, loads objects and links with deterministic ids, drops and counts unresolvable links, writes one
   `import_dataset` audit row and `workspaces/<slug>/{ontology.yaml,mapping.json,import_report.json}`.
7. Serve the workspace and browse it in the UI: `scripts/serve_workspace.sh <slug> [port]` starts an API for it and
   registers it so the workspace picker in the UI can switch to it.

The same flow scripted and headless is step (f) of `make demo`. The generator evals run offline:
`make ontogen` (baseline scores and cassette replays; the cassettes were written from the ground truth, so their scores
test the pipeline, not a model).

## Testing and evals

```bash
make test-fast      # uv run pytest -m "not live and not perf" -x -q      (database must be up)
make test           # uv run pytest -m "not live"                         (about 10 minutes)
make evals          # agent evals, mock model, seeds 1-3, report in evals/results/
make ontogen        # ontology-generator evals (baseline + mock cassettes)
make demo           # scripted demo; tests/demo/ runs it as a smoke test
make gate           # Phase 1 feasibility benchmark (slow; results in bench/results/)
uv run pytest tests/security      # attempts to reach hidden objects through multi-hop, inferred and SPARQL paths
ANTHROPIC_API_KEY=... uv run pytest -m live tests/agent/live   # live smoke tests, skipped without a key
```

Tests that depend on the machine are marked `perf` (for example the 30-day simulation time bound). Two security tests are
`xfail(strict=True)` on purpose: they document the identity-GUC limitation below.

## Configuration

All variables are optional. `.env.example` lists them; nothing in the repository reads a `.env` file, so export them in your shell.

**Database**

| Variable | Default | Meaning |
|---|---|---|
| `ISNAD_DSN` | `postgresql://isnad:isnad@localhost:55441/isnad` | Connection string |
| `ISNAD_SCHEMA` | none (use `demo`) | Schema the API serves; seed and API must agree |
| `ISNAD_TEST_DSN` | `ISNAD_DSN` | Tests only |

**API**

| Variable | Default | Meaning |
|---|---|---|
| `ISNAD_ONTOLOGY` | `ontology/supply_chain.yaml` | Ontology the API loads |
| `ISNAD_BUILD_DIR` | `build/` | Compiled shapes/ttl (workspaces use `workspaces/<slug>/build`) |
| `ISNAD_DEFAULT_ROLE` | unset | Role used when `x-role` is missing (otherwise 403). Dev convenience, widens access |
| `ISNAD_ALLOW_RESET` | `0` | `1` lets `POST /sim/control {"reset":true}` delete all data in the schema |
| `ISNAD_ALLOW_IMPORT` | `0` | `1` enables `/ingest/*` and `/ontology/*` (admin role). See Known limitations |
| `ISNAD_PIPELINE` | `1` | `0` disables the Oxigraph sync pipeline |
| `ISNAD_SIM_AUTOSTART` | `0` | Start the simulation at boot |
| `ISNAD_SIM_MAX_SECONDS` | `60` | Tests only: wall-time bound of the 30-day sim test (`perf`) |
| `ISNAD_MAX_BODY_BYTES` / `ISNAD_MAX_PROPS_BYTES` | 2 MiB / 65536 | Request body and per-write properties limits |

**Agent**

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Required for a real model; without it `/agent/runs` returns 503 |
| `ISNAD_AGENT_MODEL` | `claude-sonnet-5-5` | Model id (unknown ids are priced at the maximum known rate) |
| `ISNAD_AGENT_MAX_CONCURRENT` / `_RUNS_PER_MIN` / `_MAX_GLOBAL` | 1 / 6 / 4 | Per-user concurrency, per-user rate, process-wide cap |
| `ISNAD_AGENT_DAILY_USD` | 5 | Daily cost cap (shared with the ontology generator) |
| `ISNAD_PROPOSAL_TTL_H` | 24 | Proposal expiry |
| `ISNAD_EVAL_MODEL` | unset | Model for `python -m evals --mode live` |
| `ANTHROPIC_BASE_URL` | SDK default | Point the SDK at an Anthropic-compatible endpoint |

**Scenarios**

| Variable | Default |
|---|---|
| `ISNAD_SCN_MAX_PER_USER` / `_MAX_AGENT_PER_USER` / `_MAX_PER_RUN` / `_MAX_GLOBAL` | 5 / 5 / 3 / 20 |
| `ISNAD_SCN_TTL_H` | 168 (never above 720) |
| `ISNAD_SCN_MAX_ROWS` | 50000 row versions per scenario |
| `ISNAD_SCN_SIM_MAX_DAYS` / `_MAX_SIM_JOBS` | 90 / 2 |
| `ISNAD_SCN_GRAPH_MAX_OBJECTS` / `_MAX_GRAPH_BUILDS` / `_GRAPH_TIMEOUT_S` / `_GRAPH_CACHE` | 20000 / 2 / 120 / 3 |

**Ontology import (ingest)**

| Variable | Default | Meaning |
|---|---|---|
| `ISNAD_INGEST_DIR` | `var/ingest` | Stored uploads (kept by sha256) |
| `ISNAD_WORKSPACES_DIR` | `workspaces` | Imported workspaces |
| `ISNAD_MAX_UPLOAD_BYTES` | 20 MiB | Upload limit |
| `ISNAD_IMPORT_SOURCES` | unset | Named server-side sources: `name=postgresql://...` or `name=sqlite:/abs/path` (several entries separated by `;` or newlines) |
| `ISNAD_INGEST_MAX_TABLES` / `_MAX_COLUMNS` / `_MAX_ROWS_PROFILED` / `_MAX_SAMPLE` | 50 / 60 / 200000 / 5000 | Profiler caps |
| `ISNAD_INGEST_MAX_ZIP_MEMBERS` / `_MAX_ZIP_RATIO` | 100 / 50 | Zip safety |
| `ISNAD_INGEST_SESSIONS_PER_USER` | 10 | Import sessions per user per day |
| `ISNAD_INGEST_MAX_IMPORT_ROWS` / `_IMPORT_TIMEOUT_S` | 1000000 / 900 | Import bounds |
| `ISNAD_INGEST_THRESH_*` | 0.02 coercion and enum, 0.95 link resolution, 0.0 required nulls, 0 key violations, 50 inferred ratio | Validation thresholds |
| `ISNAD_INGEST_VALIDATE_BUDGET_S` / `_STATEMENT_TIMEOUT_MS` / `_SCRATCH_MAX_AGE_S` / `_SHACL_MAX` | 120 / 60000 / 3600 / 2000 | Validation budgets |
| `ISNAD_WORKSPACE_<SLUG>_URL` | unset | API base of a served workspace (or `workspaces/<slug>/api.json`) |
| `ISNAD_ONTOLOGY_SELF_APPROVE` | `0` | Dev only: proposer may approve own ontology change |

**Webhooks**

| Variable | Default | Meaning |
|---|---|---|
| `ISNAD_WEBHOOK_SECRET` | unset | Required for delivery (HMAC signing) |
| `ISNAD_WEBHOOK_OPS_URL`, `ISNAD_WEBHOOK_FINANCE_URL` | unset | Targets named by `url_env` in `supply_chain.yaml` |
| `ISNAD_OUTBOX_WORKER` / `ISNAD_OUTBOX_INTERVAL_S` | `1` / 2 | Delivery worker on/off and poll interval |

Optional, off by default, experimental: signed-token login (`ISNAD_AUTH=1` with `ISNAD_AUTH_SECRET`, `ISNAD_USERS_FILE`; see
`engine/auth/` and the "Auth" section of `docs/contracts.md`). It is not part of the quick start, the demo or the tests you
need to run, and has had less review than the rest.

## Security model

- **Roles** are declared in the ontology YAML (`planner`, `finance_analyst`, `regional_ops_lead` (EMEA rows), `admin`; `sim` is a
  system role that can never be selected). Each role has definition-level permissions (types, links, actions) and
  instance-level permissions (row filters, property masks). Deny by default.
- **Database enforcement.** Every request runs under `SET LOCAL ROLE isnad_app` with the identity in `isnad.*` settings. `isnad_app`
  is least-privilege; tables have FORCE row-level security; masked columns are served through `rd_*` views. Hidden objects
  return 404, indistinguishable from missing ones; losing access removes access to history.
- **Graph and SPARQL.** Traversals are filtered per node and per edge; an inferred edge is visible only if the viewer can see every
  supporting fact; SPARQL is SELECT/ASK only with a timeout and row cap, rows naming hidden objects are dropped, aggregates are
  refused for non-admins, SERVICE/FROM/UPDATE are rejected. `tests/security` tries to reach hidden objects through multi-hop and inferred paths.
- **Writes** only through actions. Gated actions become proposals; approval by a named human executes as the approver and is
  re-checked at execution time. Audit tables are append-only (triggers) and cover executions, proposals, approvals, diffs,
  rollbacks, webhooks and agent runs.
- **The agent** cannot choose its role, user, schema or SQL; its effective permissions are the intersection of the launcher's RLS, the
  role's actions and the agent allowlist; it only creates pending proposals; the launcher cannot approve its own run's proposal;
  proposals expire and are rejected as stale when the underlying objects changed.
- **Untrusted input in the generator.** Everything from a data source (table and column names, values) is untrusted: identifiers are
  re-derived through `engine.ingest.names` (`safe_prop/safe_type/safe_link`), strings are truncated and stripped of control characters,
  data is never placed in the system prompt, tool results use an untrusted envelope with an injection scanner (telemetry only),
  model output enters only through strict `Proposal.model_validate`, YAML is produced only by the deterministic `to_yaml`, generated
  ontologies can contain declarative rules and the generic actions only (no functions, webhooks or custom handlers), roles are
  expanded from a fixed template, validation runs in a throw-away schema, uploads are hash-pinned (tamper check at import) and
  zip bombs, path traversal and type mismatches are rejected. The defence is structural; the regex scanner is not the defence.

## Known limitations

Read these first.

1. **There is no authentication in the default mode.** The role is the `x-role` header sent by the UI role switcher.
   *Anyone who can reach the API port can act as any role, including `admin`.* Everything that says "a human approves" is
   enforced at the engine level, but who the human is cannot be proven. Consequences: the launcher/approver separation for agent
   proposals and the separation of duties in the import flow hold against honest use only; and **`ISNAD_ALLOW_IMPORT=1` means anyone who
   can reach the port can upload files and import data and ontologies.** Keep the API on `127.0.0.1` (`scripts/dev.sh` does) and do
   not publish the port. Optional token login exists (`ISNAD_AUTH=1`) but is experimental and off by default.
2. **Identity settings are trusted inside a scoped session** (`isnad.role/user/region` GUCs set with `SET LOCAL`). If arbitrary SQL execution were ever exposed to a
   request, it could forge the identity. No endpoint executes caller-supplied SQL today. Two `xfail(strict=True)` tests in
   `tests/security/test_review_fixes.py` document this; a signed identity context is deferred.
3. **Masking is view-level.** Direct `SELECT` on `obj_*` as `isnad_app` still returns masked columns; engine code must read through `rd_*`
   (the bitemporal layer raises when the view is missing under a scoped role).
4. A create with an id that already exists returns a generic 409, so the existence of a hidden id is distinguishable from a free id.
5. **SERVICE federation cannot be disabled in pyoxigraph 0.5**; it is blocked by query validation only. Add egress filtering in deployment.
   Runaway SPARQL cannot be cancelled in Oxigraph (the worker thread continues after the 5 s timeout; concurrency is capped at 4).
6. **Limits are per process.** The agent run registry, concurrency and global limits, scenario build semaphores, simulation slots, lazy scenario
   expiry and ingest jobs live in one process (run one uvicorn worker). Per-minute and daily agent budgets are DB-backed; there is no global daily cap.
7. Agent: admins can read every run and transcript (by design); evidence is bound to refs the run retrieved, not to the truth of the claim; stale detection
   covers the keys in the dry-run diff (volatile properties such as `position` and `progress` are ignored); the injection scanner is regex telemetry only;
   `rationale`, `evidence.claim` and `dry_run` are written from the launcher's view and not filtered for the approver; no pagination or retention for agent run events;
   idempotent re-propose may report an already decided proposal as pending.
8. Scenarios: they are snapshots of main at fork time, so an owner who later loses access to an object keeps the fork-time view until the scenario expires (default 7 days).
   Sim what-ifs build their world from the full effective state, not the caller's view (an existence oracle for injection targets).
   Scenario graph builds are synchronous and heavy on big graphs. Merge replays at merge time, and `audit_event.tool_trace` of merge replays is an object, not a list.
9. Ontology versioning is additive-only; approving a version records it and a restart is needed to apply it.
10. Import (new in Phase 6): the import is a bulk load, so the graph store is not built at import time (the workspace process rebuilds it on first use);
    one workspace is one schema, one YAML and one API process (no multi-ontology process); sources are CSV/TSV/zip, SQLite and named Postgres only;
    generated ontologies get no masks or row filters (read/write/admin template roles only); the ingest tables are append-only, so rejected and abandoned sessions stay;
    stored uploads are kept in `var/ingest` until removed by hand; the validator's scratch schemas are swept by age, not instantly, if the process dies.
    Imported data is a snapshot: re-importing creates a new workspace, there is no incremental sync.
11. **What has and has not been verified.** The test suite, the mock evals (39 agent cases; generator cassettes) and `make demo` run in CI-less local
    runs only; `.github/workflows/ci.yml` has not run on a real runner. **No run against the real Claude API has been made by the project authors** (no Anthropic key was available): `AnthropicModel` is
    tested against a stub. In Phase 4 the agent smoke tests and evals were run once against a small free non-Claude model (GLM through an
    Anthropic-compatible endpoint): hard guardrails held, soft behaviour mostly missed. The scenario tools and the ontology generator have never been run live.
    Cassette and scripted-agent scores are ~1.0 by construction and prove the platform around a model, not model quality; the heuristic baseline
    (types F1 0.91 and 1.00 on messy_supply and bikeshare) is the number a model must beat.
    Hosted deployment, multi-user load, and the image on non-arm64 hosts have not been exercised.
12. The simulated data is synthetic and the use case is one supply-chain ontology; geospatial map tiles need internet (OpenStreetMap).

## Project layout

```
ontology/            supply_chain.yaml (the demo ontology)
engine/              compiler, db (bitemporal, RLS, principal), actions, api, scenarios, sync, reason, versioning, ingest, auth
agent/               runtime agent (tools, policy, audit, models) and ontogen/ (ontology generator)
sim/                 SimPy supply-chain world, runner, what-if
web/                 React + Vite UI (pages/, import/, mock/ for offline development)
sdk/                 generated TypeScript client (copy of build/client.ts)
evals/               agent evals (cases/), ontogen/ (datasets, cassettes, references, reports)
bench/               Phase 1 feasibility gate
tests/               pytest suites by area (security/, ingest/, agent/, demo/ ...)
scripts/             setup.sh, dev.sh, serve_workspace.sh, demo_phase3..6.py, make_users.py
docs/                architecture.md, contracts.md, demo.md
brand/               brand kit (BRAND.md, tokens.css, logos)
build/               compiled output (generated, not committed)
workspaces/          imported workspaces (generated, ignored except demo_* fixtures)
```

`CLAUDE.md` holds the working rules and the per-phase command notes; `RESEARCH.md` the research and plan.

## License

No license has been chosen yet; until one is added to the repository, all rights are reserved by the authors.
