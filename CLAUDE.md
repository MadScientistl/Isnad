# CLAUDE.md

Project name: **Isnad** (renamed from Amarh; env vars `ISNAD_*`, DB role `isnad_app`, GUCs `isnad.*`, DSN db/user `isnad`; existing local Postgres volumes still hold the old `amarh` db, so recreate with `docker compose down -v`). Brand kit in `brand/` (`BRAND.md`, `tokens.css`, logos), based on humain.com; `web/src/brand.css` is a copy of `brand/tokens.css`, keep in sync.

Open-source, Palantir Foundry/AIP-inspired ontology platform for one simulated use case (supply chain, geospatial events, finance ops). The full design and sources are in `RESEARCH.md`; read it before any work. This file holds the rules that apply every session.

## Status
Phase 1 done (M0-M6; gate in `bench/results/report.md`). Phase 2 done (M7-M11; gate re-run `bench/results/report_phase2.md`). Phase 3 built on branch `phase-3` (governance and actions): roles in YAML, FORCE RLS + least-privilege `isnad_app`, masking views, action engine (execute/propose/approve/rollback, criteria, rules, webhook outbox), append-only audit, permission-filtered graph/SPARQL/history, security suite, role switcher + actions panel + audit/approvals UI. Security review done and its blockers fixed; known limitations in `docs/contracts.md` ("Known limitations after Phase 3 review"): identity GUCs are trusted inside a scoped session (2 strict xfail tests), masking is view-level, `x-role` is a non-secret header (no real auth). Phase 3 merged to main with Phase 4. Phase 4 built on branch `phase-4` (runtime agent): `agent/` tool loop (9 role-scoped tools, caps, injection handling, append-only run audit), `/agent/*` routes with SSE, agent console/run history/proposal cards in the UI, proposals with `on_behalf_of`/evidence/expiry/stale detection, `reroute_shipment`, read-only `functions:`, mock model + 20-case eval suite + demo. Security review done and its findings fixed or listed in `docs/contracts.md` ("Known limitations after Phase 4 review"); no live Claude API run has happened. Phase 4 is merged to main (7f1761f). Phase 5 (scenarios and evals) was built on branch `phase-5` and merged to main via PR after user review: copy-on-write scenario overlays (scenario_id, `eff_*` views, snapshot at fork), scenario-aware actions/KPIs/graph/SPARQL, compare/merge/discard, sim what-ifs, agent scenario tools, 39-case eval suite with seeds and reports, ontology versioning (additive-only), scenario UI. Security review done and fixes applied; see docs/contracts.md. Phase 6 (ontology auto-generation, polish) is built on branch `phase-6` (not merged; waits for user review): deterministic profiler for CSV/zip/SQLite/PostgreSQL, Claude proposal agent (`agent/ontogen`, never run live: no key) with a no-model heuristic baseline, scratch-schema validation loop, human review wizard, audited import into a per-ontology workspace schema `ws_<slug>` (one API process per workspace), ontology manager UI, generalised engine/UI (no supply-chain hardcoding), opt-in token auth (dormant), README/setup/demo. See "Phase 6 commands and conventions" below. This was the last planned phase.

## Commands
Contracts and conventions: `docs/contracts.md`. Python via `uv` (venv currently CPython 3.14).
- Database: `docker compose up -d postgres` (built from `docker/postgres/Dockerfile`: Postgres 18 + PostGIS 3.6, the official postgis image has no arm64 build; DSN `postgresql://isnad:isnad@localhost:55441/isnad`, needs `CREATE EXTENSION btree_gist`).
- Tests: `uv run pytest` (needs the database up).
- Compile ontology: `uv run python -m engine.compiler ontology/supply_chain.yaml build/`.
- Seed demo data: `ISNAD_SCHEMA=demo uv run python -m engine.api.seed`; API: `ISNAD_SCHEMA=demo uv run uvicorn engine.api.main:app --port 8000`.
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
- Seed (idempotent, non-destructive): `ISNAD_SCHEMA=demo uv run python -m engine.api.seed`.
- Headless simulation: `ISNAD_SCHEMA=demo uv run python -m sim --seed 1 --days 30` (prints action counts and the event digest; same seed gives the same digest). 30 sim days take about 15 s.
- API (use 127.0.0.1, `localhost:8000` may be another Docker service): `ISNAD_SCHEMA=demo uv run uvicorn engine.api.main:app --host 127.0.0.1 --port 8000`. The sim does not auto-run: the UI time bar starts it (speed 1/6/24 sim hours per wall second). `POST /sim/control {"reset":true}` deletes all data in the schema and reseeds, so it needs `ISNAD_ALLOW_RESET=1`.
- UI: `cd web && VITE_API=http://127.0.0.1:8000 npm run dev` (must be port 5173, the API's CORS origin). Offline UI development: `VITE_MOCK=1 npm run dev` (fake API in `web/src/mock/`). The map needs internet for OSM tiles.
- Time model: sim valid time, wall-clock recorded time. Sim epoch 2026-01-01; the sim never runs past wall-clock now, so the `cur_*` views are the latest sim state. Time slider = `as_of_valid`.
- All writes go through `engine.actions.apply_action` (one transaction, audit row with action_id). Only the seed bootstrap writes with `bt.write_*` directly. A position update resumes a delayed shipment (`ship_position` sets `in_transit`).
- Inferred edges exist for the live state only (the reasoner materialises current state); the graph view says so when the slider is in the past. Only `supplies` (transitive, inverse) produces inferred edges today.
- Tests: `uv run pytest` (about 80 s, mostly the 30-day sim tests).

## Phase 3 commands and conventions
- Schema setup: `engine.db.setup.apply_schema(conn)` (governance.sql + compiled `build/schema.sql`); seed and API startup call it. Recompile after YAML changes: `uv run python -m engine.compiler ontology/supply_chain.yaml build/ --sdk sdk/`.
- Roles live in `ontology/supply_chain.yaml` (`roles:`): planner, finance_analyst, regional_ops_lead (EMEA), admin, sim (system, never selectable). The UI sends `x-role`; a missing header is 403 unless `ISNAD_DEFAULT_ROLE` is set. Every request runs in a transaction under `engine.db.principal.scoped()` (SET LOCAL ROLE isnad_app + `isnad.*` settings).
- All writes go through `engine.actions.execute/propose/decide/rollback`. Only `engine/db`, `engine/actions` and the seed bootstrap call `bt.write_*`. Audit tables (`audit_event` is the execution table, `proposal`, `approval`, `execution_diff`, `rollback`, `webhook_outbox`, `webhook_delivery`) are append-only (triggers reject UPDATE/DELETE/TRUNCATE); tests use a fresh schema instead of truncating. Reset keeps audit.
- Webhooks: `ISNAD_WEBHOOK_SECRET` is required for delivery; URLs come from the env var named by `url_env` in the YAML; worker disabled with `ISNAD_OUTBOX_WORKER=0`. Sim control/inject are admin-only (the UI time bar gets 403 for other roles).
- Demo: `PYTHONPATH=. uv run python scripts/demo_phase3.py` (same action allowed for one role, denied for another, audited).
- Tests: `uv run pytest` (about 4 min). Security suite: `tests/security`.

## Phase 4 commands and conventions
- Agent config lives in the YAML per role (`agent:` allowlist, tools, caps, instructions); effective permissions = user RLS ∩ role actions ∩ agent allowlist. Tools take no role/user/schema/SQL; the agent only creates pending proposals (`agent:<role>`, `on_behalf_of` = launcher); approval executes as the approving human; the launcher cannot approve; proposals expire after `ISNAD_PROPOSAL_TTL_H` (24) and stale ones are 409.
- Needs `ANTHROPIC_API_KEY` (never committed); model via `ISNAD_AGENT_MODEL`. Without a key `/agent/runs` returns 503 and live tests are skipped. Limits: `ISNAD_AGENT_MAX_CONCURRENT` (1), `ISNAD_AGENT_RUNS_PER_MIN` (6), `ISNAD_AGENT_DAILY_USD` (5), `ISNAD_AGENT_MAX_GLOBAL` (4).
- Evals (mock model, deterministic): `uv run python -m evals --mode mock`. Demo: `PYTHONPATH=. uv run python scripts/demo_phase4.py` (`--live` exits 2 without a key).
- UI: `#/agent`, `#/agent/runs`; offline with `VITE_MOCK=1`.
- Tests: `uv run pytest` (agent tests in `tests/agent`, evals in `tests/evals`; live smoke in `tests/agent/live`).
- Cheap live model (Z.ai GLM via its Anthropic-compatible endpoint; only the free `glm-4.5-flash` worked with our key): `export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic ANTHROPIC_API_KEY=<z.ai key> ISNAD_AGENT_MODEL=glm-4.5-flash ISNAD_EVAL_MODEL=glm-4.5-flash`, then `uv run pytest tests/agent/live` or `uv run python -m evals --mode live` (about 10 min). Unknown model ids are priced at the maximum known rate unless listed in `agent/pricing.py`. Live run with glm-4.5-flash: 4 smoke tests pass; evals hard guardrails held (after making two checks tolerant of free-form models), soft behaviour mostly missed (small model).

## Phase 5 commands and conventions
- Scenarios: `x-scenario: <id>` header on data routes; `POST/GET /scenarios`, `/scenarios/{id}/compare|merge|simulate|series`, `DELETE /scenarios/{id}`. A scenario is an overlay in the same tables (`scenario_id`), snapshot at fork time (`base_recorded`), read via `eff_*`/`rd_eff_*` views; `cur_*` stay main-only (fast path). Merge replays human actions on main; agent-authored actions become proposals (proposer `agent:<role>`, approved by a different human). Sim-forward events are never merged.
- Limits (env): `ISNAD_SCN_MAX_PER_USER` 5, `ISNAD_SCN_MAX_AGENT_PER_USER` 5, `ISNAD_SCN_MAX_PER_RUN` 3, `ISNAD_SCN_MAX_GLOBAL` 20, `ISNAD_SCN_TTL_H` 168, `ISNAD_SCN_MAX_ROWS` 50000, `ISNAD_SCN_SIM_MAX_DAYS` 90, `ISNAD_SCN_GRAPH_MAX_OBJECTS` 20000, `ISNAD_SCN_MAX_SIM_JOBS` 2, `ISNAD_SCN_MAX_GRAPH_BUILDS` 2.
- What-if: `sim.whatif.run` (seeded, deterministic digest); injections port_closure, shipment_delay, supplier_delay, price_shock, invoice_anomaly. Demo: `PYTHONPATH=. uv run python scripts/demo_phase5.py`.
- Evals: `uv run python -m evals --mode mock --seeds 1,2,3 --md` (reports in `evals/results/`, per case tool trace, latency, cost); live mode needs a key (exit 2 without). CI: `.github/workflows/ci.yml` (not run on a real runner yet).
- Ontology versioning: `/ontology/*` admin routes; additive-only; approval records a version only, restart needed to apply.
- Tests: `uv run pytest -m "not live"` (about 10 min). `tests/sim/test_sim.py::test_thirty_days_completes_and_measure` (60 s bound) is borderline: 49 s on phase-4, 57-61 s now.

## Known limitations after Phase 5 review
See docs/contracts.md "Phase 5 security review fixes". Notably: x-role is still unauthenticated (owner/approver separation only as strong as it); sim what-ifs build the world from the full world, not the caller's view (hidden-uuid existence oracle via injection targets, L3); scenarios are snapshots, so an owner keeps fork-time views of objects they later lose until the scenario expires; scenario graph builds are synchronous and heavy on big graphs; limits and sim slots are per process; no live Claude API run of the scenario tools.

## Phase 6 commands and conventions
- One command: `./scripts/setup.sh` (compose up, uv sync, compile, npm ci, seed), then `./scripts/dev.sh [--import]` (API on 127.0.0.1:8000 + UI on 5173) or `make setup|dev|test|evals|ontogen|demo|gate`. Scripted end-to-end demo: `make demo` / `uv run python scripts/demo_phase6.py --model mock` (7 steps, ~15-25 s; `--live` needs `ANTHROPIC_API_KEY`). Docs: `README.md`, `docs/architecture.md`, `docs/demo.md`.
- Import: needs `ISNAD_ALLOW_IMPORT=1` (router and `/workspaces` are mounted only then, in every mode; a workspace process has it 0). Without `ISNAD_AUTH` the role is an unauthenticated `x-role` header, so import = anyone who reaches the port; bind to 127.0.0.1. UI role switcher is the supported mode; token auth (`ISNAD_AUTH=1`, `engine/auth`, `scripts/make_users.py`) is built and tested but dormant/experimental.
- Pipeline: `engine/ingest/` sources -> `profile` (Profile, baseline proposal) -> `proposal.py` (pydantic `Proposal`, `to_yaml` the only YAML producer, template roles, names via `names.py`) -> `validate.py` (scratch schema `gen_<hex>`, `materialize.py` shared with the importer) -> review (`PATCH items` edits create new proposals; `/approve` rejects non-accept items) -> `importer.py` (owner path, one transaction, one `import_dataset` audit row, writes `workspaces/<slug>/`). Tables `ingest_session|proposal|decision|spend` are append-only. Routes in `engine/api/ingest_routes.py`.
- Serve an imported workspace: `scripts/serve_workspace.sh <slug> 8001` (writes `workspaces/<slug>/api.json`; the UI workspace picker reads it). The importer writes no `graph_outbox` rows; the workspace process rebuilds its graph on first use.
- Generator evals: `uv run python -m evals.ontogen --mode baseline|mock|live [--validator real] [--md]` (datasets `evals/ontogen/datasets/{messy_supply,bikeshare,injection}`, references and ground truth in `evals/ontogen/references`). Mock numbers are by construction (cassettes were written from the ground truth) and prove the pipeline, not model quality. Baseline (no model): messy_supply aggregate 0.81, bikeshare 0.96, both validate. Live mode exits 2 without a key.
- Caps (env): `ISNAD_MAX_UPLOAD_BYTES` 20 MiB, `ISNAD_INGEST_MAX_TABLES|_MAX_COLUMNS|_MAX_ROWS_PROFILED|_MAX_SAMPLE|_MAX_IMPORT_ROWS|_SESSIONS_PER_USER`, `ISNAD_INGEST_THRESH_*`, `ISNAD_INGEST_GLOBAL_DAILY_USD`, `ISNAD_IMPORT_SOURCES` (named SQL sources, server side only), `ISNAD_INGEST_DIR`, `ISNAD_WORKSPACES_DIR`, `ISNAD_BUILD_DIR`.
- Generated ontologies use only declarative `rules:` plus the five generic actions; no `functions:`, `webhooks:`, `kpis:` or `sim:` can be emitted. Interfaces (RESEARCH s.8) are not in the compiler and map to `classes`.
- Test notes: run `tests/agent` and `tests/security` as separate pytest invocations if a combined invocation errors on conftest names; `tests/sim` 30-day test is marked `perf` (`ISNAD_SIM_MAX_SECONDS`).

## Known limitations after Phase 6
- Everything from the Phase 3/4/5 lists still holds (2 strict xfails: identity GUCs trusted inside a scoped session; masking view-level; per-process limits; SERVICE federation; no authentication in the supported mode).
- No live Claude API run of the generator (or of the Phase 4/5 tools against Claude). Generator quality vs the references is measured only for the heuristic baseline and for hand-authored cassettes; treat model quality as unverified.
- Imported workspaces get the fixed role template (admin plus read/write roles, no masks or row filters): PII columns are visible to every role until a human edits the YAML. A host admin is admin on every workspace.
- What-if digests are deterministic for the same fork state; a fork taken after a main-schema sim run that created random ids is not reproducible across runs.
- UI gaps: no reject button for proposals, no list of past import sessions (wizard state lives in sessionStorage), Map in generic workspaces still shows supply-chain controls, baseline link names can be unpretty after reserved-word renames.
- Review gaps accepted: named PostgreSQL sources fingerprint structure only (data can change between review and import; import re-validates), login throttling/revocation/tickets are per process, budget reservations of a crashed process count for 24 h.
