# CLAUDE.md

Open-source, Palantir Foundry/AIP-inspired ontology platform for one simulated use case (supply chain, geospatial events, finance ops). The full design and sources are in `RESEARCH.md`; read it before any work. This file holds the rules that apply every session.

## Status
Phase 1 done (M0-M6; gate in `bench/results/report.md`). Phase 2 done (M7-M11; gate re-run `bench/results/report_phase2.md`). Phase 3 built on branch `phase-3` (governance and actions): roles in YAML, FORCE RLS + least-privilege `amarh_app`, masking views, action engine (execute/propose/approve/rollback, criteria, rules, webhook outbox), append-only audit, permission-filtered graph/SPARQL/history, security suite, role switcher + actions panel + audit/approvals UI. Security review done and its blockers fixed; known limitations in `docs/contracts.md` ("Known limitations after Phase 3 review"): identity GUCs are trusted inside a scoped session (2 strict xfail tests), masking is view-level, `x-role` is a non-secret header (no real auth). Phase 3 pending user review (not merged to main). Phase 4 (agent) is next; one phase per session (RESEARCH.md section 10).

## Commands
Contracts and conventions: `docs/contracts.md`. Python via `uv` (venv currently CPython 3.14).
- Database: `docker compose up -d postgres` (built from `docker/postgres/Dockerfile`: Postgres 18 + PostGIS 3.6, the official postgis image has no arm64 build; DSN `postgresql://amarh:amarh@localhost:55441/amarh`, needs `CREATE EXTENSION btree_gist`).
- Tests: `uv run pytest` (needs the database up).
- Compile ontology: `uv run python -m engine.compiler ontology/supply_chain.yaml build/`.
- Seed demo data: `AMARH_SCHEMA=demo uv run python -m engine.api.seed`; API: `AMARH_SCHEMA=demo uv run uvicorn engine.api.main:app --port 8000`.
- UI: `cd web && npm install && npm run dev` (port 5173). If `localhost:8000` hits another Docker service, run `VITE_API=http://127.0.0.1:8000 npm run dev`. `sdk/client.ts` is a copy of the generated `build/client.ts`: recompile, then copy.
- Gate benchmark: `uv run python -m bench.gate --seed 1 --scale 1.0`.

## Working rules
1. Plan first: for each phase write a concrete plan (modules, data model, milestones) and wait for approval before writing code.
2. Follow RESEARCH.md. If a decision looks wrong, say so and ask before deviating.
3. Claims marked unverified in RESEARCH.md (Oxigraph SHACL/RDF-star, PostgreSQL 18 temporal features, `reasonable` maturity) must be verified by docs or tests before relying on them. Report findings.
4. The Phase 1 feasibility gate is a real stop: report results honestly, including failures, and ask before building on a failed gate.
5. Keep this file updated with stack, commands and conventions as they become real.

## Architecture (short)
- Postgres + PostGIS is the only source of truth: instance data (bitemporal), audit, proposals, permissions, action outbox.
- Oxigraph holds the semantic layer (OWL ontology, instance ids and edges, inferred edges). It is derived and must be rebuildable from the YAML ontology plus Postgres.
- One YAML ontology compiles to Postgres DDL, OWL + SHACL, API routes/models and a TypeScript SDK.
- Writes happen only through action types, in one Postgres transaction; side effects go through the outbox after commit.
- Reasoning: OWL 2 RL (`reasonable`, fallback `owlrl`), SHACL via pySHACL. Business rules are functions, not OWL. Inferred edges are always flagged and carry provenance.
- Stack: Python FastAPI, SimPy, React + Vite + TypeScript, MapLibre/deck.gl, Cytoscape, ECharts, shadcn/ui, Claude via the API.

## Roles, permissions and agents
- v1 has no real login: a UI role switcher selects the active role. The backend must still enforce permissions on every request from that role, so real auth can be added later without engine changes.
- Roles are defined in the ontology config (for example: supply-chain planner, finance analyst, regional operations lead, admin). Each role has definition-level permissions (which types and actions it can see or use) and instance-level permissions (row-level rules, property masking).
- This section is about the platform's own runtime agent (the product), not the development subagents. Each runtime agent instance is scoped to the acting user's role and carries that role's permissions and an action allowlist. The agent's effective permissions are the intersection of the acting user's permissions, the role's permissions and the agent's allowlist, never the union.
- Different roles get different agent behavior: tools, visible objects, proposable actions and approval requirements are all role-scoped. A finance agent cannot see or propose what its role cannot.
- Reads run as the requesting user. `propose_action` writes only a proposal. Approval by a named human triggers execution as the approver, re-checked at execution time.
- Permissions are enforced in the API and database (row-level security with FORCE), never only in prompts.
- Multi-hop traversals and SPARQL results are filtered per node and per edge. An inferred edge is visible only if the user can see every fact it was derived from. Maintain a test suite that tries to reach hidden objects through multi-hop and inferred paths.
- Permissions apply to history: losing access to an object removes access to its past versions.
- Every agent run stores the role, user, tool trace and outcome in the audit log.

## Development workflow: Claude subagents with roles
Spin up Claude subagents for implementation, each with a specific role, a narrow file scope and only the tools and permissions that role needs. Give each subagent a self-contained brief (goal, files it owns, definition of done), since it does not see this conversation.

| Subagent role | Owns (write access) | Tools / permissions |
|---|---|---|
| Ontology & compiler | `ontology/`, compiler in `engine/` | Read, write own files, run tests |
| Data & temporal model | Postgres schema, migrations, bitemporal logic | Read, write own files, run DB and tests |
| Action engine & audit | action engine, policy, audit, outbox | Read, write own files, run tests |
| Reasoning & sync | Oxigraph sync, reasoner, SHACL, feasibility benchmark | Read, write own files, run benchmarks |
| Agent & evals | `agent/`, `evals/` | Read, write own files, call the Claude API |
| Frontend | `web/`, `sdk/` | Read, write own files, run the dev server and browser checks |
| Security reviewer | none (read-only) | Read and search only; reviews permission, RLS and multi-hop changes |
| Verifier | none (read-only) | Runs tests and checks results against the phase's definition of done |

Rules:
- One writer per set of files. Never let two subagents edit the same files in parallel; sequence them or split by directory.
- Run independent subagents in parallel only when none needs another's output.
- Read-only roles never edit; they report findings back.
- The main session integrates the results, resolves conflicts and checks the work before reporting to the user.

## Conventions
- Seeded randomness everywhere in simulation and evals so runs are reproducible.
- Do not commit secrets. The Claude API key comes from the environment.
- Add commands to this file (install, run, test, lint) as soon as they exist.

## Phase 2 commands and conventions
- Compile and refresh the SDK: `uv run python -m engine.compiler ontology/supply_chain.yaml build/ --sdk sdk/` (also copies `build/client.ts` to `sdk/client.ts`).
- Seed (idempotent, non-destructive): `AMARH_SCHEMA=demo uv run python -m engine.api.seed`.
- Headless simulation: `AMARH_SCHEMA=demo uv run python -m sim --seed 1 --days 30` (prints action counts and the event digest; same seed gives the same digest). 30 sim days take about 15 s.
- API (use 127.0.0.1, `localhost:8000` may be another Docker service): `AMARH_SCHEMA=demo uv run uvicorn engine.api.main:app --host 127.0.0.1 --port 8000`. The sim does not auto-run: the UI time bar starts it (speed 1/6/24 sim hours per wall second). `POST /sim/control {"reset":true}` deletes all data in the schema and reseeds, so it needs `AMARH_ALLOW_RESET=1`.
- UI: `cd web && VITE_API=http://127.0.0.1:8000 npm run dev` (must be port 5173, the API's CORS origin). Offline UI development: `VITE_MOCK=1 npm run dev` (fake API in `web/src/mock/`). The map needs internet for OSM tiles.
- Time model: sim valid time, wall-clock recorded time. Sim epoch 2026-01-01; the sim never runs past wall-clock now, so the `cur_*` views are the latest sim state. Time slider = `as_of_valid`.
- All writes go through `engine.actions.apply_action` (one transaction, audit row with action_id). Only the seed bootstrap writes with `bt.write_*` directly. A position update resumes a delayed shipment (`ship_position` sets `in_transit`).
- Inferred edges exist for the live state only (the reasoner materialises current state); the graph view says so when the slider is in the past. Only `supplies` (transitive, inverse) produces inferred edges today.
- Tests: `uv run pytest` (about 80 s, mostly the 30-day sim tests).

## Phase 3 commands and conventions
- Schema setup: `engine.db.setup.apply_schema(conn)` (governance.sql + compiled `build/schema.sql`); seed and API startup call it. Recompile after YAML changes: `uv run python -m engine.compiler ontology/supply_chain.yaml build/ --sdk sdk/`.
- Roles live in `ontology/supply_chain.yaml` (`roles:`): planner, finance_analyst, regional_ops_lead (EMEA), admin, sim (system, never selectable). The UI sends `x-role`; a missing header is 403 unless `AMARH_DEFAULT_ROLE` is set. Every request runs in a transaction under `engine.db.principal.scoped()` (SET LOCAL ROLE amarh_app + `amarh.*` settings).
- All writes go through `engine.actions.execute/propose/decide/rollback`. Only `engine/db`, `engine/actions` and the seed bootstrap call `bt.write_*`. Audit tables (`audit_event` is the execution table, `proposal`, `approval`, `execution_diff`, `rollback`, `webhook_outbox`, `webhook_delivery`) are append-only (triggers reject UPDATE/DELETE/TRUNCATE); tests use a fresh schema instead of truncating. Reset keeps audit.
- Webhooks: `AMARH_WEBHOOK_SECRET` is required for delivery; URLs come from the env var named by `url_env` in the YAML; worker disabled with `AMARH_OUTBOX_WORKER=0`. Sim control/inject are admin-only (the UI time bar gets 403 for other roles).
- Demo: `PYTHONPATH=. uv run python scripts/demo_phase3.py` (same action allowed for one role, denied for another, audited).
- Tests: `uv run pytest` (about 4 min). Security suite: `tests/security`.
