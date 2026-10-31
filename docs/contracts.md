# Phase 1 contracts (owned by the main session)

Python 3.12 via `uv` (`uv run pytest`). Postgres 18 (no PostGIS in Phase 1): `docker compose up -d postgres`,
DSN `postgresql://isnad:isnad@localhost:55441/isnad`. Extension `btree_gist` required.

## Ontology model
Input: `ontology/supply_chain.yaml`. Property types: `string`, `int`, `float`, `bool`, `timestamp`, `{enum: [...]}`; a `!` suffix means required.
Link keys: `from`, `to`, `cardinality` (many-to-many | many-to-one | one-to-many | one-to-one), `required`, `transitive`, `inverse`.
`classes` is a hierarchy (`extends`); an object type with the same name as a class is a subclass of it.

## Table contract (compiler emits, data engine uses)
Common columns on every table: `valid_during tstzrange NOT NULL`, `recorded_during tstzrange NOT NULL`,
`changed_by text NOT NULL`, `action_id uuid NULL`, `scenario_id int NOT NULL DEFAULT 0`.
- `obj_<snake_type>`: `id uuid NOT NULL`, one column per property (`enum` -> text + CHECK, `timestamp` -> timestamptz), plus common columns.
  Constraint: `EXCLUDE USING gist (scenario_id WITH =, id WITH =, valid_during WITH &&, recorded_during WITH &&)`. NO `WITHOUT OVERLAPS` PK (it rejects bitemporal corrections). Index on `(id)`.
- `link_<link_name>`: `src uuid NOT NULL`, `dst uuid NOT NULL`, common columns. EXCLUDE on `(scenario_id, src, dst, valid_during, recorded_during)`. Indexes on `(src)` and `(dst)`. No FKs; the engine checks endpoints.
- View `cur_obj_<t>` / `cur_link_<l>`: rows where `upper_inf(recorded_during)` and `valid_during @> now()` and scenario_id = 0.
- Open-ended ranges use `infinity` upper bounds.
- `graph_outbox(seq bigserial pk, kind text, type text, id uuid, src uuid, dst uuid, op text, at timestamptz default now())`; engine writes one row per committed change in the same transaction. `ontology_registry(hash text pk, yaml text, applied_at timestamptz)`.
- Compiler API: `engine.compiler.compile_ontology(path) -> CompiledOntology` with `.model` (parsed dataclasses), `.ddl() -> str`, `.owl_turtle() -> str`, `.shacl_turtle() -> str`, `.pydantic_models()`, `.ts_sdk() -> str`. Generated files are also written by `python -m engine.compiler ontology/supply_chain.yaml build/`.
- Instance IRIs: `<namespace>obj/<uuid>`; classes `<namespace><TypeName>`; link properties `<namespace><link_name>`; the object type name is also the `rdf:type`.
- Data engine API (`engine/db/bitemporal.py`): `write_object(conn, type, id, props, valid_from, changed_by, ...)`, `delete_object(...)`, `write_link(...)`, `end_link(...)`, `get_object(conn, type, id, as_of_valid=None, as_of_recorded=None)`, `history(conn, type, id)`. The ONLY code that splits/stitches ranges lives there. Each call writes `graph_outbox` rows in the same transaction.

## Addenda after M1/M2 (verified: `uv run pytest` = 34 passed)
- Open-ended range = unbounded upper (`upper_inf`) OR literal `infinity`; always test with `upper_inf(r) OR upper(r)='infinity'`. Use `engine.db.connect()` (registers an infinity loader).
- Outbox `kind` in {obj, link}; `type` = type or link name as passed; `op` in {upsert, delete}. Table names `obj_<snake(type)>`, `link_<snake(link)>`.
- Engine reads: get_object, list_objects, history, link_history, neighbours(link, id, direction), list_links. Bulk: bulk_load_objects, bulk_load_links.
- Compiled outputs in `build/` (schema.sql, ontology.ttl, shapes.ttl, models.py, client.ts). API routes the TS client assumes: `/objects/{type}`, `/objects/{type}/{id}`, `/links/{link}?id=`, header `x-role`.

# Phase 2 contracts (owned by the main session; frozen before implementation)

## Decisions
- Time model: the simulation has its own clock `sim_now`; every sim write uses `valid_from = sim_now`. Recorded time stays wall-clock (`clock_timestamp()`), so the bitemporal invariant is untouched. The sim epoch is in the past (default `2026-01-01T00:00:00Z`) and the sim never advances beyond wall-clock now. Because sim writes are monotonic in sim time and `sim_now <= now()`, the existing `cur_*` views equal "latest sim state"; sync and the reasoner keep using them unchanged. The time slider is `as_of_valid` (sim time); `as_of_recorded` is only for "what did we believe then".
- PostGIS: custom image built from `docker/postgres/Dockerfile` (`postgres:18` + `postgresql-18-postgis-3`; the official `postgis/postgis` image has no arm64 build), `CREATE EXTENSION IF NOT EXISTS postgis` in generated schema.sql. Same volume path as before.
- Geo property types in YAML: `point` -> `geometry(Point,4326)`, `linestring` -> `geometry(LineString,4326)`. Python/TS/API representation is GeoJSON (`{"type":"Point","coordinates":[lon,lat]}`), written via `ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)`, read via `ST_AsGeoJSON`. OWL: datatype property with range `geo:wktLiteral`. Geometry is not mirrored into Oxigraph (ids/types/edges only). GiST index on every geometry column. No-op detection compares normalised WKB (`ST_AsEWKB`).
- `lat`/`lon` stay on Port/Warehouse until M8 is finished; `location: point` is added alongside and written by the seed.
- Map tiles: free OSM raster (no key). Needs internet; documented in CLAUDE.md.

## Action + audit layer (minimal, extended in Phase 3)
- YAML key `actions:` maps name -> `{params: {<prop>: <type>}, writes: [...]}`. Phase 2 actions are declared and implemented in Python (`engine/actions/`), the YAML list is the registry of names and parameter types.
- `engine.actions.apply_action(conn, name, params, actor, *, valid_at, source="api", run_id=None) -> ActionResult(action_id, changed)`. One Postgres transaction: validate, write via `bt.write_*` with a fresh `action_id uuid`, insert `audit_event`, commit. Callers (REST mutations, simulation) never call `bt.write_*` directly.
- `audit_event(seq bigserial pk, action_id uuid, name text, actor text, source text, run_id text, params jsonb, sim_time timestamptz, recorded_at timestamptz default clock_timestamp(), before jsonb, after jsonb)`. Append only (no UPDATE/DELETE grants in Phase 3; Phase 2 never updates or deletes).
- Actions: `create_object`, `update_object`, `delete_object`, `create_link`, `end_link` (generic, used by REST) plus sim actions `ship_depart`, `ship_position`, `ship_arrive`, `delay_shipment`, `close_port`, `reopen_port`, `issue_invoice`, `flag_invoice`, `pay_invoice`.

## Ontology v2 (supply_chain.yaml additions)
Object types: `Invoice` (ref!, amount float!, currency string, status enum[open,approved,paid,flagged], due timestamp, anomaly string), `PurchaseOrder` (ref!, amount float, status string), `Event` (kind!, severity enum[low,medium,high], started timestamp, ended timestamp, note string). Shipment gains `position: point`, `progress: float`, `cargo_value: float`, `delay_hours: float`. Port/Warehouse gain `location: point`. Links: `ships_from` (Shipment->Warehouse|Port, many-to-one), `ships_to` (Shipment->Warehouse, many-to-one), `invoices_for` (Invoice->Shipment, many-to-one), `billed_by` (Invoice->Supplier, many-to-one), `affects` (Event->Port, many-to-many). A shipment's `status` enum: planned|in_transit|delayed|arrived.

## Simulation (`sim/`)
- `sim.world.World(seed, epoch, ...)`, `sim.runner.SimRunner`. Master seed derives one `random.Random` per process family (`sha256(f"{seed}:{family}")`). Same seed + same injections => identical sequence of `(sim_time, action name, params)`; test hashes this sequence.
- Speeds (sim hours per wall second): `paused`, `1`, `6`, `24`, plus `step` (+1 sim hour per call). Tick = 1 sim hour; one tick's writes are applied in order via `apply_action` (each action is its own transaction). Position updates every 6 sim hours per in-transit shipment (keep ~30 active shipments). The runner must stay within its target rate or report the achieved rate in `/sim/state` (`achieved_hours_per_s`); sync runs after each tick batch, the reasoner at most every `min_reason_interval_s` wall seconds and only if edges changed. Headless `run_until(sim_time)` is unthrottled; target: 30 sim days in under 60 s wall.
- Interfaces: `sim.runner.SimRunner(dsn=None, schema=None, seed=1, epoch=datetime(2026,1,1,tzinfo=UTC), pipeline=None, run_id=None)` with `.state() -> dict`, `.set_speed(speed)`, `.step()`, `.run_until(t)`, `.inject(kind, target=None, at=None, duration_h=24.0) -> dict`, `.reset()` (re-seed state is the API's job), `.start()/.stop()` (background thread). `.event_digest() -> str` (sha256 over the ordered (sim_time, action, params) stream). `engine.sync.pipeline.SyncPipeline(dsn=None, schema=None, store_path=None, min_reason_interval_s=5.0)` with `.sync() -> int`, `.maybe_reason(force=False) -> bool`, `.rebuild()`, `.store`, `.inferred_edges(ids) -> list[tuple[src,link,dst]]` (instance-to-instance inferred edges touching ids).
- Seed (`engine.api.seed`): epoch 2026-01-01; about 12 ports, 6 warehouses, 6 carriers, 30 suppliers (chains + tree), all with `location` points and lat/lon; no shipments at start (the sim creates them via apply_action create_object + create_link + ship_depart).- Injection: `POST /sim/inject {kind: port_closure|shipment_delay|invoice_anomaly, target: uuid|null, at: sim time|null, duration_h}`.

## API additions
`GET /sim/state` -> `{sim_now, epoch, speed, running, seed, tick}`; `POST /sim/control {speed|step|reset}`; `POST /sim/inject`; `GET /events?from&to&limit` (audit feed, newest first); `GET /geo/{type}?bbox=w,s,e,n&as_of=` GeoJSON FeatureCollection; `GET /stats/summary?as_of=`; `GET /stats/series?metric&from&to&step_hours`; `GET /graph?center=&depth=&as_of=&include_inferred=` -> `{nodes:[{id,type,label,props}], edges:[{src,dst,link,inferred}]}`. All accept `x-role`. Inferred edges are returned only when `as_of` is absent or "live" (the reasoner materialises current state); otherwise `inferred_available:false`.

## Response shapes (frozen for the frontend; Phase 2)
- `GET /sim/state` -> `{sim_now: iso, epoch: iso, speed: "paused"|"1"|"6"|"24", running: bool, seed: int, tick: int, achieved_hours_per_s: float|null, last_error: str|null}`. `POST /sim/control` body `{speed?: ..., step?: true, reset?: true}` -> same shape. `POST /sim/inject` body `{kind: "port_closure"|"shipment_delay"|"invoice_anomaly", target?: uuid, at?: iso, duration_h?: number}` -> `{injected: {kind, target, at}}`.
- `GET /events?from=&to=&limit=100` -> `[{seq, action_id, name, actor, source, sim_time, recorded_at, params, summary}]` newest first; `summary` is a short human string made by the API.
- `GET /geo/{type}?bbox=w,s,e,n&as_of=iso` (type in Port, Warehouse, Shipment) -> GeoJSON FeatureCollection; each feature `{type:"Feature", id: uuid, geometry: Point, properties: {id, type, ...object props except geometry}}`. Objects with null geometry are omitted. Shipment features also carry `origin: [lon,lat]|null`, `destination: [lon,lat]|null` in properties (from ships_from/ships_to, as of `as_of`).
- `GET /stats/summary?as_of=iso` -> `{as_of, shipments: {planned, in_transit, delayed, arrived}, ports: {open, closed}, invoices: {open, approved, paid, flagged}, value_at_risk: float, delay_hours_total: float, open_events: int}`; `value_at_risk` = sum of cargo_value of non-arrived shipments that ship_via a closed port or are delayed.
- `GET /stats/series?metric=&from=&to=&step_hours=24` (metric in shipments_in_transit, shipments_delayed, ports_closed, invoices_flagged, value_at_risk) -> `{metric, points: [{t: iso, v: number}]}`, computed from bitemporal history at each step.
- `GET /graph?center=uuid&depth=1..3&as_of=iso|live&include_inferred=true` -> `{center, as_of, inferred_available: bool, nodes: [{id, type, label, status|null}], edges: [{src, dst, link, inferred: bool}]}`; max 300 nodes; truncated flag `truncated: bool`. Inferred edges only when as_of is absent or live.
- All list endpoints continue to accept `as_of_valid`/`as_of_recorded`; the existing REST mutation routes (POST/PUT/DELETE objects and links) now route through `apply_action` with `source="api"`, actor = role.

## Addenda after Phase 2 integration (verified: `uv run pytest` = 132 passed)
- `ship_position` sets status `in_transit` when the shipment was `delayed` (a position update means it is moving again).
- `POST /sim/control`: a non-paused speed starts the runner thread, `paused` stops it. `reset` is refused (403) unless `ISNAD_ALLOW_RESET=1`; when allowed it truncates obj_*, link_*, graph_outbox and audit_event in the API's schema, reseeds, rebuilds the sync store and resets the runner.
- The API creates its `SyncPipeline` lazily, syncs and reasons once on first use, and `/graph` (live) syncs again so rows written by another process (headless sim) appear.
- Benchmark on the PostGIS schema: `bench/results/report_phase2.md` (materialize 18.35 s, 4-hop p95 0.211 s, SHACL 0.30 s, all PASS).

# Phase 3 contracts (owned by the main session; frozen before implementation)

Decisions taken (user accepted defaults): D1 rollback = compensating action; D2 webhook failure never reverts data or graph (retry, dead-letter, manual retry); D3 proposer != approver; D4 current visibility of an object id decides access to all its versions (row filters also apply to the version); D5 SPARQL aggregates rejected for non-admin; D6 inferred edge visible if >= 1 derivation has every supporting fact visible; D7 seed = privileged loader audited as ONE `seed` execution, reset keeps audit; D8 `region` property on Port, Warehouse (enum APAC|EMEA|AMER, assigned by longitude in seed); D9 no hash chain.

## Roles (YAML, top level `roles:`; deny by default)
```yaml
roles:
  planner:
    description: ...
    types:                      # absent type = no access
      Shipment: {read: true, write: true}
      Invoice:  {read: false}
      Supplier: {read: true, mask: [country]}      # mask: properties returned as null
      Port:     {read: true, where: "region = $region"}   # row filter; $region = role.region
    links: [ships_via, ...] | all                  # readable link types (both endpoints must also be visible)
    actions: [delay_shipment, ...]                 # may execute (or propose)
    approves: [action, ...]                        # may approve proposals of these actions
    region: EMEA                                   # optional, value of $region
```
Roles: `planner`, `finance_analyst`, `regional_ops_lead` (region EMEA, row filters on Port/Warehouse by `region`, Shipment via `exists(ships_to, Warehouse: region = $region)`), `admin` (everything, approves everything), `sim` (system role: only the sim action set; never selectable from the UI). The old role name `analyst` is removed.
Row-filter DSL (compiler parses, no eval, identifiers validated): `prop (=|!=|<|<=|>|>=) (literal|$region)`, `prop in (..)`, `is null`, `and|or|not`, parentheses, and `exists(<link>, <Type>: <expr>)` (outbound link, current version of target). Unknown names fail compile.

## Postgres enforcement
- DB roles (global, created idempotently): `isnad_app` NOLOGIN, no BYPASSRLS, not owner. Owner/loader/migrations use the connection user (superuser in dev); owner-owned `SECURITY DEFINER` helper functions (`SET search_path FROM CURRENT` at creation) answer "is object id currently visible" so policies avoid self-recursion.
- Per-request identity (set inside the request transaction): `SET LOCAL ROLE isnad_app; SELECT set_config('isnad.role', <role>, true), set_config('isnad.user', <user>, true), set_config('isnad.region', <region|''>, true)`; also `statement_timeout=5s`, `lock_timeout=2s`. Unset or unknown `isnad.role` => zero rows, writes denied (fail closed).
- Every `obj_*`, `link_*`, governance table: `ENABLE` + `FORCE ROW LEVEL SECURITY`. Object policy: type readable for role AND the object id's CURRENT version satisfies the role's `where` (D4), applied to all versions. Write policies require `write: true` + filter. Link policy: link type readable AND both endpoint ids visible. `cur_*` views are `security_invoker`.
- Masking: generated views `rd_obj_<t>` (all versions) and `rd_cur_obj_<t>` (current), `security_invoker`, masked columns are `CASE WHEN <role masks it> THEN NULL ELSE col END` keyed on `current_setting('isnad.role')`. `isnad_app` has SELECT on `rd_*` views and link tables; for base `obj_*` tables it has INSERT/UPDATE (closing ranges) only via the action engine path and SELECT only on non-masked types' columns it needs (engine reads use `rd_*`). Reads in `engine.db.bitemporal` take `masked: bool = True` (default) and read `rd_*`; the owner/loader passes `masked=False`.
- Security tests assume `isnad_app` cannot UPDATE/DELETE/TRUNCATE audit tables and cannot write obj_/link_ tables outside the engine's grants.

## Governance tables (file `engine/db/governance.sql`, per schema, idempotent, all append-only: BEFORE UPDATE/DELETE trigger raises; no UPDATE/DELETE/TRUNCATE grants; FORCE RLS; SELECT policy = admin all, other roles own rows (actor/proposer/approver = isnad.user) plus rows whose affected objects they can see for diff rows)
- `audit_event` (= the EXECUTION table; name kept for compatibility with sim and `/events`). Existing columns kept (`seq, action_id, name, actor, source, run_id, params, sim_time, recorded_at, before, after`); added: `role text, on_behalf_of text, proposal_id uuid, status text default 'executed', tool_trace jsonb, parent_execution_id uuid` (set for rollback).
- `proposal(id uuid pk, seq, action text, params jsonb, proposer, proposer_role, rationale text, dry_run jsonb, created_at, run_id, tool_trace)`; state is derived from approval rows (`approval(id, proposal_id, decision 'approved'|'rejected', approver, approver_role, comment, decided_at, execution_id uuid null)`); a proposal is pending iff it has no approval row.
- `execution_diff(seq, action_id, kind 'object'|'link', type, key text, op 'create'|'modify'|'delete', before jsonb, after jsonb)`: one row per changed object/link (key = object id or `src>dst`). `audit_event.before/after` stay as the summary.
- `rollback(id, original_action_id, rollback_action_id, actor, reason, at)`.
- `webhook_outbox(id uuid pk, action_id, hook text, url text, payload jsonb, idempotency_key text unique, created_at)` plus `webhook_delivery(seq, outbox_id, attempt int, status 'pending'|'delivered'|'retry'|'dead', http_status int, error text, next_attempt_at, at)`: state = latest delivery row for an outbox id; "pending" row is written in the same transaction as the action. Worker uses `FOR UPDATE SKIP LOCKED` over the latest-row view `webhook_state`.
- `inferred_support(link text, src uuid, dst uuid, derivation_no int, support uuid[] (all node ids involved), data_version bigint)`.
- Python: `engine/db/principal.py`: `Principal(role, user, region)`, `scoped(conn, principal)` context manager issuing the SET LOCALs above (conn must be in a transaction), `OWNER` sentinel for loader code.

## Actions (YAML)
```yaml
actions:
  flag_invoice:
    description: ...
    params: {invoice: "uuid!", anomaly: "string!"}     # existing syntax; `uuid(Type)!` = uuid that must reference a visible object of Type
    roles: [finance_analyst, admin, sim]               # may execute directly
    approval: {required: false}                        # or {required: true, approvers: [admin]}; required => execute returns a proposal
    bind: {inv: {type: Invoice, id: $invoice}}         # objects loaded (under the actor's RLS) for criteria
    criteria:
      - {expr: "inv.status != 'paid'", message: "Invoice is already paid"}
    rules:                                             # applied in order, one transaction, via bitemporal engine
      - modify: {type: Invoice, id: $invoice, set: {status: flagged, anomaly: $anomaly}}
      - create: {as: event, type: Event, set: {kind: x, started: $now}}   # $new.event = generated uuid
      - delete: {type: T, id: $x}
      - link:   {link: l, src: $a, dst: $b}
      - unlink: {link: l, src: $a, dst: $b}
    function: close_port                               # alternative to rules: registered Python function (escape hatch)
    webhooks: [notify_finance]
    rollback: compensate | irreversible                # default compensate
webhooks:
  notify_finance: {url_env: ISNAD_WEBHOOK_FINANCE_URL, method: POST, max_attempts: 5}   # missing env var => delivery marked dead with error "no url"
```
Expression language (no eval): `$param`, `$now`, `$actor`, `alias.prop`, literals, `== != < <= > >= in`, `and or not`, `is null`, `is not null`, `+ - * /`. Permission checks run in this order: role may execute action -> params valid -> binds visible under RLS (invisible = "not found") -> criteria -> rules, whose writes are further constrained by write RLS. A denial is audited as an `audit_event` row with `status='denied'` ONLY for permission/criteria failures on API/agent calls (not sim), no data writes.
- Engine API (`engine/actions/`): `execute(conn, name, params, principal, *, valid_at, source='api', run_id=None, proposal_id=None, tool_trace=None, ontology=None) -> ActionResult` (replaces `apply_action`; `apply_action` kept as thin alias with `principal=Principal(actor-as-role...)` removed once callers migrate); `propose(conn, name, params, principal, rationale, ...) -> proposal_id`; `decide(conn, proposal_id, principal, approve: bool, comment)` (approve executes AS the approver, re-checking everything, D3: proposer != approver; admin can't bypass); `rollback(conn, action_id, principal, reason)` (D1: only if every affected object/link still equals the recorded `after`; else `RollbackConflict` listing keys); `dry_run(...)` returns the diff without committing. Errors: `PermissionDenied`, `CriteriaFailed(failures)`, `ValueError` (validation), `RollbackConflict`. `engine/actions/outbox.py`: `deliver_due(conn_factory, now=None) -> int`, `run_worker(stop_event, interval_s)`; header `Idempotency-Key`, body HMAC `X-Isnad-Signature` with `ISNAD_WEBHOOK_SECRET`; backoff 2^n s * jitter up to `max_attempts`, then `dead`; manual retry writes a new `pending` delivery row.
- Sim: calls `execute(..., Principal('sim','system:sim',''), source='sim')`; the sim event digest is unchanged for the same seed.
- Seed: runs as OWNER, writes through `bt.*`, then inserts ONE `audit_event` (`name='seed'`, `source='seed'`, params = counts). `/sim/control reset` deletes obj_/link_/graph_outbox/inferred_support rows only, keeps all audit tables, inserts a `reset` audit event.

## API (all routes get `Principal` from `x-role`; unknown/`sim` => 403; every handler runs inside `scoped()`)
`GET /me` -> `{role, user, region, types:{T:{read,write,masked:[..]}}, actions:[..]}`; `GET /roles` -> selectable roles (no `sim`); `GET /actions?type=&id=` -> `[{name, description, params, requires_approval, allowed: bool, reason: str|null}]`; `POST /actions/{name}/execute {params, valid_at?}` -> 200 `{action_id, status:'executed', changed, webhooks:[..]}` | 202 `{proposal_id, status:'pending_approval'}` | 403 `{code:'permission_denied', detail}` | 422 `{code:'criteria_failed', failures:[{message}]}` or validation; `POST /actions/{name}/propose {params, rationale}` -> 201 `{proposal_id}`; `GET /proposals?state=pending|all`; `POST /proposals/{id}/approve|reject {comment}`; `GET /executions?limit&name&actor&status`, `GET /executions/{action_id}` -> `{execution, diff:[..], webhooks:[{outbox, deliveries}], rollback}`; `POST /executions/{action_id}/rollback {reason}`; `POST /webhooks/{outbox_id}/retry`; `GET /audit?kind=execution|proposal|approval|webhook|rollback&limit` unified feed; `POST /sparql {query}` -> `{vars, rows}` (SELECT/ASK only, 5 s timeout, 1000 rows, rows containing a hidden object IRI dropped, aggregates/GROUP BY/COUNT rejected for non-admin, SERVICE/FROM/UPDATE rejected). Existing REST object/link mutations route to the generic actions (`create_object`, etc.), allowed iff role has `write: true` for the type. `/graph`, `/neighbours`, `/geo`, `/stats/*`, `/events`, history, as_of: all run under RLS; `/graph` additionally filters inferred edges via `inferred_support` (D6), cut paths at hidden nodes; hidden objects look identical to missing ones (404 for by-id reads, never 403).

## Known limitations after Phase 3 review
- Identity is carried by `isnad.role` / `isnad.user` / `isnad.region` GUCs set with `SET LOCAL` inside a scoped transaction. They are trusted: if arbitrary SQL execution is ever exposed to a request, `set_config(...)` or `RESET ROLE` can forge or drop the identity. Today no endpoint executes caller-supplied SQL. A signed identity context (HMAC over role/user/region/txid verified by the policies) is deferred. Tracked by two `xfail(strict=True)` tests in `tests/security/test_review_fixes.py`.
- Hardening applied: SECURITY DEFINER helpers use `search_path = pg_catalog, <schema>, public, pg_temp`; `TEMPORARY` on the database is revoked from PUBLIC and `isnad_app`.
- Masking is view-level only (`rd_*` views and output-time masking of diff/before/after/dry_run/params). Direct SELECT on `obj_*` as `isnad_app` still returns masked columns; engine code must read through `rd_*` (`bitemporal.read_source` now raises when the view is missing under a scoped role).
- `x-role` is a plain, non-secret header: v1 has no authentication. Anyone who can reach the API can act as any selectable role, including `admin`.
- A create with an id that already exists (hidden or not) returns a generic 409, so existence of a hidden id is still distinguishable from a free id.
- SERVICE federation cannot be disabled in pyoxigraph 0.5 (verified); it is blocked by validation only. Add egress filtering in deployment.
- Runaway SPARQL queries cannot be cancelled in Oxigraph; the worker thread continues after the 5 s timeout (concurrency is capped at 4 requests, 429 beyond).

# Phase 4 contracts (owned by the main session; frozen before implementation)

Decisions (user approved the plan; the open questions take the recommended defaults, all configurable in YAML): P1 default model `claude-sonnet-5-5` (override with `ISNAD_AGENT_MODEL`); P2 caps per run: 12 model turns, 25 tool calls, 60k output tokens, USD 0.50, 120 s wall clock; per user 1 concurrent run, 6 run starts per minute, USD 5 per day; P3 the agent never executes anything, no auto-execute class; P4 the user who started a run cannot approve its proposals; P5 proposals expire after 24 wall hours and stale approvals are hard-blocked (409 `stale_proposal`); P6 new action `reroute_shipment` and a read-only `functions:` registry are added; P7 the admin agent gets a small explicit allowlist, never `all`.

## Identity
The agent's `Principal` is created server-side when a run starts (`get_principal`, same seam as the UI) and stored on the run. No tool parameter, prompt text or tool result can change it. Proposals created by an agent: `proposer = "agent:<role>"`, `proposer_role = role`, `on_behalf_of = <launching user>`. `decide` refuses an approver equal to `proposer` or `on_behalf_of` (`PermissionDenied`).

## YAML additions
```yaml
roles:
  planner:
    agent:
      allowlist: [delay_shipment, reroute_shipment, close_port]   # propose-only; must be in role.actions; generic actions rejected
      tools: [query_objects, traverse, find_paths, impact_analysis, get_history, as_of, diff, run_function, propose_action]
      instructions: "..."            # role persona/focus appended to the system prompt
      max_tool_calls: 25             # optional overrides of the P2 defaults
      max_cost_usd: 0.5
functions:                           # read-only, role-gated, registered in Python (engine/functions.py)
  shipments_at_risk: {description: ..., params: {port: "uuid!"}, roles: [planner, regional_ops_lead, admin]}
```
Effective agent permissions = user RLS (reads) AND role.actions AND agent.allowlist (propose) AND agent.tools. Compile fails on unknown names, allowlist entries outside role.actions, or generic actions (`create_object`, `update_object`, `delete_object`, `create_link`, `end_link`).

## Tables (engine/db/governance.sql, append-only like the others)
- `proposal` gains `on_behalf_of text`, `idempotency_key text UNIQUE NULLS DISTINCT`, `evidence jsonb`, `base_versions jsonb` (per touched object: `{type:id -> recorded_lower}`), `expires_at timestamptz`.
- `agent_run(id uuid pk, seq, role, user, prompt, model, caps jsonb, started_at)` and `agent_run_event(seq, run_id, kind 'message'|'tool_call'|'tool_result'|'denied'|'proposal'|'finished', payload jsonb, tokens_in, tokens_out, cost_usd, at)`. Status is derived from the last `finished` event (`completed|capped|error|cancelled`). SELECT policy: admin all; otherwise `user = isnad.user`. Stored tool results are already masked.

## API
`POST /agent/runs {prompt}` -> 201 `{run_id}` (429 on limits); `GET /agent/runs/{id}/stream` (SSE events `delta|tool_call|tool_result|denied|proposal|finished`, each `{seq, ...}`); `GET /agent/runs?limit`; `GET /agent/runs/{id}` -> `{run, events:[..]}`; `POST /agent/runs/{id}/cancel`; `GET /agent/config` -> role's tools, allowlist, caps. Another role's run is a 404. `/proposals` rows gain `on_behalf_of, evidence, expires_at, expired: bool, stale: bool|null`. Approving a stale proposal: 409 `{code:'stale_proposal', changed:[keys]}`; an expired one: 409 `{code:'proposal_expired'}`.

## Tool result envelope
`{"ok": bool, "data": <masked, truncated>, "untrusted": true, "provenance": {"asserted": [..], "inferred": [{edge, rule, support}]}, "truncated": bool}`. Caps: query_objects limit 50; traverse depth 3, fan-out 25, nodes 100; find_paths hops 4, paths 10.

## Known limitations after Phase 4 review
Read-only security review of `agent/`, `agent_routes`, `decide` and the agent tables found no blocker: the model cannot change role, user, schema or approver set, cannot execute anything (only create a pending proposal), and cannot read what the launching human cannot. Fixed after review: property values named `ref` no longer register as facts, refs and uuid params use strict full-match and canonical UUID parsing (M3); stale detection also fails when an object or link existed at approval but was not in the proposal's snapshot (M4); a failed model call is charged an estimate against the daily budget (M6); no event can be appended to a finished run or to another user's run (trigger `isnad_run_open` + insert policy); DB/network errors are reduced to their class name in `finished.error`; at most 4 open SSE streams per live run (429). Remaining, accepted for now:
1. **P4/D3 are only as strong as `x-role`.** One user per role (`demo-<role>`): whoever launched a planner run can send `x-role: admin` and approve it. The launcher ban holds at engine level (tests) but not against a header switch; it needs real auth. The admin agent's proposals (`on_behalf_of` demo-admin, approvers `[admin]`) cannot be approved by anyone.
2. Admin can read every run, prompt and transcript (by design).
3. Evidence is bound to refs the run retrieved, not to the truth of the claim; the UI shows claims as the agent's, unverified.
4. Stale detection covers the keys in the dry-run diff; volatile props (`position`, `progress`) are ignored by design (TODO: move to YAML).
5. Run registry, concurrency, global limit and cancel are in-process (one uvicorn worker). Per-minute and daily limits are DB-backed. Distinct `x-role` values are distinct users, so an anonymous client gets one budget per role; no global daily cap.
6. `rationale`, `evidence.claim` and `dry_run` are written from the launcher's view and are not filtered by the approver's visibility; harmless while the only approver is admin.
7. No retention or pagination for `agent_run_event`; `GET /agent/runs/{id}` returns all events. SSE and read routes have no rate limit beyond the per-run stream cap (not load-tested).
8. Idempotent re-propose returns the existing proposal id with `pending_approval` even if it was since decided or expired (same run only).
9. The injection scanner is regex-only telemetry; the defence is structural (low-privilege tools, intersection permissions, human approval).
10. No live Claude API call has been made (no key in this environment): `AnthropicModel` is tested against a stub only. Mock evals prove platform enforcement and plumbing, not model judgement.
11. Phase 3 limitations still hold (identity GUCs trusted inside a scoped session, view-level masking).

# Phase 5 contracts (owned by the main session; frozen before implementation)

Decisions (user accepted the recommended defaults): S1 overlay in the same tables (scenario_id), no schema clone; S2 snapshot-at-fork semantics; S3 merge aborts on any conflict; S4 merge replays user/agent actions only (sim-forward events and injections are never merged); S5 approval-gated actions in a merge become proposals; S6 limits below; S7 an agent may execute allowlisted actions inside its own scenario only (sandbox; main stays propose-only, P3 relaxed for scenario state); S8 ontology gains `Shipment.extra_cost float` and `Supplier.price_index float` (KPI cost / price shock); S9 scenario visible to owner + admin + optional `shared_roles`; S10 minimal additive-only ontology versioning is in; S11 evals: 3 seeds in CI, ~32 cases, CI gates on hard checks only, soft scores report-only; S12 `.github/workflows/ci.yml` is added.

Spike result (Milestone 0, 200k rows, `scratchpad/spike.py`): a GUC-aware predicate inside `cur_*` slows full scans of the current view about 7-10x (27 ms -> 200-300 ms) because it defeats parallel/simple scans; point lookups are unaffected (0.3-0.4 ms; scenario 0.6-1.1 ms). Therefore: **`cur_*` and `rd_cur_*` stay exactly as they are (main fast path).** Scenario-aware reads use a parallel view family `eff_*`/`rd_eff_*`; the GUC-aware predicate is used only in point-lookup contexts (visibility helpers, `exists()` filters). Python read paths choose `rd_cur_*` when scenario is 0/None and `rd_eff_*` otherwise.

## Identity and context
- `Principal` unchanged. `engine.db.principal.scoped(conn, principal, scenario=None)`: `scenario` is a `ScenarioCtx(id:int, base_recorded:datetime, clock:datetime|None)`; when set it also issues `set_config('isnad.scenario', id, true)`, `'isnad.scenario_base', base_recorded`, `'isnad.now', clock or ''`. Reset clears them. Unset/empty = main. A scenario id is resolved ONLY server-side from the `scenario` table after a visibility check (never taken raw from a client), by `engine.scenarios.resolve(conn, principal, id) -> ScenarioCtx` (raises `ScenarioNotFound` for unknown, invisible, or non-open scenarios; hidden = missing, 404).
- Helper SQL functions (owner-owned, SECURITY DEFINER where needed, per schema): `isnad_scn() int`, `isnad_scn_base() timestamptz`, `isnad_now() timestamptz` (= `isnad.now` GUC else `now()`), `isnad_scenario_visible(p_id int) bool` (scenario 0 true; else owner = isnad.user, or role admin, or isnad.role = ANY(shared_roles), and status open).

## Tables (engine/db/governance.sql, per schema, idempotent)
- `scenario(id serial PK, name text, owner_user text, owner_role text, parent_id int NULL, base_recorded timestamptz, base_sim_time timestamptz, shared_roles text[] DEFAULT '{}', run_id text NULL, created_at timestamptz, expires_at timestamptz)` — append-only. `scenario_event(seq bigserial PK, scenario_id int, kind 'created'|'merged'|'discarded'|'expired'|'simulated'|'proposal', actor text, payload jsonb, at timestamptz)` — append-only; status = open unless the latest terminal event (`merged|discarded|expired`) exists.
- `scenario_state(scenario_id int PK, clock timestamptz NULL, data_version bigint DEFAULT 0, override_rows int DEFAULT 0)` — mutable (engine only); `data_version` bumps on every scenario write (graph cache key), `clock` is the what-if sim clock.
- `scenario_override(scenario_id int, kind text ('obj'|'link'), type text, key_a uuid, key_b uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000', deleted bool DEFAULT false, base jsonb, PRIMARY KEY (scenario_id, kind, type, key_a, key_b))` — mutable (engine only); object key = key_a, link key = (src,dst). `base` = main's version at `base_recorded` (`{"present":bool,"props":{...}|null}`) and `main_lower` = recorded lower bound of main's current version when first touched; feeds merge conflict detection. Indexes `(scenario_id)`. FORCE RLS: visible iff `isnad_scenario_visible(scenario_id)`.
- Added column `scenario_id int NOT NULL DEFAULT 0` on: `audit_event` (scenario in which the action executed), `execution_diff`, `proposal` (= EVIDENCE scenario; the proposal always executes on main), `agent_run`, `inferred_support` (PK becomes `(scenario_id, link, src, dst, derivation_no)`), `rollback`. RLS: rows with `scenario_id > 0` are visible only if `isnad_scenario_visible(scenario_id)` in addition to the existing rule. `graph_outbox` and webhook tables never receive scenario writes.
- Object/link tables: composite indexes `(scenario_id, id)`, `(scenario_id, src)`, `(scenario_id, dst)`. Policies add `scenario_id = 0 OR (scenario_id = isnad_scn() AND isnad_scenario_visible(scenario_id))` (SELECT/UPDATE/INSERT) — no role can read or write another scenario's rows.
- Views (compiler): unchanged `cur_*`, `rd_cur_*`, `rd_*`; NEW `eff_obj_<t>`, `eff_link_<l>` (security_invoker) and masked `rd_eff_obj_<t>`: when `isnad_scn()=0` equals `cur_*`; else rows where (a) `scenario_id = isnad_scn()` AND current-knowledge (`upper_inf(recorded_during)`) AND `valid_during @> isnad_now()`, OR (b) `scenario_id = 0` AND `recorded_during @> isnad_scn_base()` AND `valid_during @> isnad_now()` AND no `scenario_override` row for that key in the scenario (overridden keys, including tombstones, come only from (a)). `current_pred()` (used by visibility helpers and `exists()` filters) becomes this GUC-aware expression.
- Ontology adds: `Shipment.extra_cost: float`, `Supplier.price_index: float` (1.0 default applied by seed/actions, null tolerated as 1.0).

## bitemporal.py contract
Every `bt.*` function keeps `scenario_id=0`. For `scenario_id=N>0`: reads return the effective version (rule above, clock = scenario clock or now); writes (`write_object/delete_object/write_link/end_link`) do clone-on-first-touch: copy ALL current-knowledge rows of that id (or edge) from main as of `base_recorded` into `scenario_id=N` (preserving valid ranges, `recorded_during` re-based to `[now,∞)`), insert/update the `scenario_override` row (with `base`), then apply the normal split logic on scenario rows; deletes set `deleted=true` (tombstone) and close the scenario rows; link endpoints are checked against the effective view. No `graph_outbox` row and no webhook for scenario writes; each scenario write bumps `scenario_state.data_version`, `override_rows` and enforces the `ISNAD_SCN_MAX_ROWS` cap (`ScenarioLimit`). `bt.list_overrides(conn, scenario_id)` returns the override rows. `engine.db.scenario_admin.purge(conn, scenario_id)` (owner-only janitor) deletes the scenario's obj/link/override/support rows (audit stays).

## Python API
- `engine.scenarios` (Action engine role): `create(conn, principal, name, *, run_id=None, parent=None, expires_in_h=None, shared_roles=()) -> dict`; `get(conn, principal, id)`; `list_(conn, principal, status='open')`; `resolve(conn, principal, id) -> ScenarioCtx`; `discard(conn, principal, id)` (owner/admin; purge + event); `compare(conn, principal, a, b=0, *, limit=500) -> {diff, kpis:{a,b,delta}, truncated}`; `merge(conn, principal, id, comment='') -> {merged:[{name, action_id|proposal_id}]}` raising `MergeConflict(conflicts)`; `expire_due(conn)`. Errors: `ScenarioNotFound`, `ScenarioLimit`, `MergeConflict`.
- Compare/diff is computed under the caller's principal on BOTH sides through RLS+masking; a key the caller cannot see on either side is omitted (no placeholder, no count).
- Actions: `execute/dry_run/propose/rollback(..., scenario_id=0)`; `decide` never takes a scenario (always main). In a scenario: no webhooks, no `after_write` sync/reason, `audit_event.scenario_id=N`, criteria/RLS/permissions identical. Merge replays recorded actions (`audit_event` where `scenario_id=N AND status='executed' AND source IN ('api','agent','ui')`, ordered by seq, excluding `source='sim'`) with `source='scenario_merge'`, `params` unchanged, `run_id` = `scenario:<id>`; each replay runs as the merging principal on main; approval-gated actions are proposed (`proposer` = merging user) instead. Conflict check before replay: every `scenario_override` key with `base.present` must still equal main's current props (volatile props `position, progress` ignored) and `deleted`/created keys must not exist/not be missing in main; any conflict aborts everything.
- `engine.kpis.compute(conn, principal, scenario_id=0, as_of_valid=None) -> dict`: `{value_at_risk, delay_hours_total, shipments_delayed, shipments_in_transit, ports_closed, invoices_flagged, extra_cost, exposure_shipments, exposure_suppliers}` (all under the caller's RLS; `exposure_*` = shipments/suppliers reachable from closed ports via asserted links; in main also counts inferred supply chains when available). `engine.kpis.series(conn, principal, scenario_id, metric, from_, to, step_hours)`. Delta = b - a.
- Graph in scenario (Reasoning & sync role): `engine.sync.scenario_graph.get_store(conn, ctx, comp) -> ScenarioGraph` (in-memory Oxigraph store built from `eff_*`, `materialize` run, `inferred_support` rows written with the scenario id), cached per `(scenario_id, data_version)`, LRU 3, refuses (`GraphTooLarge` -> HTTP 413) above `ISNAD_SCN_GRAPH_MAX_OBJECTS` (20000). `visibility.*`, `sparql.run_query`, `shacl` and `SyncPipeline.inferred_edges` accept `scenario=` (a `ScenarioCtx|None`); the main store, main pipeline and main `inferred_support` rows (scenario_id=0) are never touched by scenario work.

## Limits (env, defaults)
`ISNAD_SCN_MAX_PER_USER=5` open scenarios per user, `ISNAD_SCN_MAX_PER_RUN=3` per agent run, `ISNAD_SCN_MAX_GLOBAL=20`, `ISNAD_SCN_TTL_H=168` (idle expiry; `expire_due` marks expired and purges), `ISNAD_SCN_MAX_ROWS=50000` override rows per scenario, `ISNAD_SCN_SIM_MAX_DAYS=90`, `ISNAD_SCN_GRAPH_MAX_OBJECTS=20000`.

## API
- Header `x-scenario: <int>` accepted on all data read routes (`/objects*`, `/links*`, `/neighbours*`, `/geo*`, `/stats*`, `/graph`, `/sparql`, history, `/actions`, `/events`, `/executions*`, `/audit`, `/proposals`) and on `POST /actions/{name}/execute|propose` and REST object/link mutations. Missing = main. Unknown/invisible/closed scenario = 404 `{code:'scenario_not_found'}`. Rejected with 400 `{code:'scenario_unsupported'}`: `/sim/*`, `/webhooks/*`, `/proposals/{id}/approve|reject`. Governance reads under a scenario header return that scenario's rows only (main reads never include scenario rows).
- `POST /scenarios {name, expires_in_h?, shared_roles?}` -> 201 scenario; `GET /scenarios?status=open|all`; `GET /scenarios/{id}`; `DELETE /scenarios/{id}` (discard) -> 200; `GET /scenarios/{id}/compare?with=main|<id>` -> `{a, b, diff:[{kind,type,key,op,before,after}], kpis:{a:{..},b:{..},delta:{..}}, truncated}`; `POST /scenarios/{id}/merge {comment?}` -> 200 `{status:'merged', merged:[{name, action_id?, proposal_id?}]}` | 409 `{code:'merge_conflict', conflicts:[{kind,type,key,reason}]}` | 429 `{code:'scenario_limit'}`; `POST /scenarios/{id}/simulate {days (1..90), seed?, injections:[{kind: port_closure|shipment_delay|supplier_delay|price_shock|invoice_anomaly, target?, at_h?, duration_h?, magnitude?}]}` -> 202 `{job_id}`; `GET /scenarios/{id}/simulate/{job_id}` -> `{status:'running'|'done'|'error', sim_now, digest, actions, kpis, error}`; `GET /scenarios/{id}/series?metric=&step_hours=` -> `{metric, points}`.
- Scenario object: `{id, name, owner_user, owner_role, status, base_recorded, base_sim_time, clock, shared_roles, created_at, expires_at, run_id, override_rows, data_version}`.
- Proposals/inbox: `proposal.dry_run` gains `{scenario_id, impact:{before:{kpis}, after:{kpis}, delta:{kpis}}}` when a scenario produced it (`impact` KPI keys as above). Admin only for delete of foreign scenarios.

## Agent
Tools added (role `agent.tools` lists them; default for planner/regional_ops_lead/admin/finance as relevant): `scenario_open {name}` -> `{scenario_id}` (forks; owner = launcher; max `ISNAD_SCN_MAX_PER_RUN` per run; the run's scenario becomes the active one: every read tool and `propose_action` then runs in it), `scenario_apply {action, params}` (executes an allowlisted action in the active scenario; same permission intersection; no approval gate inside a scenario), `scenario_simulate {days<=14, injections}`, `scenario_compare {}` -> `{kpis:{main,scenario,delta}, diff_summary}`. The scenario id is held on `AgentContext`/`agent_run.scenario_id`, never a tool parameter. `propose_action` while a scenario is active: `dry_run` = scenario diff + impact (above) and `proposal.scenario_id` = evidence scenario; the proposal still executes only on main after human approval.

## Simulation
`sim.whatif.run(conn_factory, scenario_ctx, *, days, seed, injections) -> WhatIfResult(digest, actions, kpis, sim_now)`: the world loads from the scenario's effective state and writes through `execute(..., scenario_id=N, source='sim')`; `run_id='scenario:<id>:<seed>'`; the scenario clock (`scenario_state.clock`) replaces the wall-clock cap (cap = `ISNAD_SCN_SIM_MAX_DAYS` from base_sim_time); same fork + seed + injections = same digest. New injection kinds: `supplier_delay` (delays shipments of a supplier's carriers/links), `price_shock` (multiplies `Supplier.price_index` by `magnitude`). Main sim and `/sim/*` unchanged.

## Ontology versioning (Ontology & compiler role, own schema file `engine/versioning/schema.sql`, `apply(conn)` called from `engine.db.setup.apply_schema`)
Tables (append-only): `ontology_version(id serial PK, hash text UNIQUE, parent_hash text NULL, yaml text, created_by text, created_at timestamptz, source 'startup'|'proposal'|'rollback', note text)`, `ontology_change_proposal(id uuid PK, base_hash, yaml text, diff jsonb, breaking bool, rationale text, proposer text, created_at)`, `ontology_change_decision(id, proposal_id UNIQUE, decision 'approved'|'rejected', approver text, comment, at, version_id NULL)`. `engine.versioning`: `register_current(conn, yaml_text, by)` (idempotent, called at API startup), `diff(old_yaml, new_yaml) -> {added:{}, removed:{}, changed:{}, breaking:[...]}` over object types, properties (type/required/enum), links, actions, roles, functions; `propose/decide/rollback`. Apply = additive only (new types/properties/links/actions/roles; any removal or type change is `breaking` and rejected); rollback re-registers the parent definition non-destructively (columns remain). Admin-only API in `engine/api/ontology_routes.py` (APIRouter, mounted by the main session): `GET /ontology/versions`, `GET /ontology/versions/{id}`, `GET /ontology/diff?from=&to=`, `POST /ontology/proposals {yaml, rationale}`, `GET /ontology/proposals`, `POST /ontology/proposals/{id}/approve|reject`, `POST /ontology/versions/{id}/rollback`. Applying to a running API requires a restart (documented).

## Evals (Agent & evals role)
CLI: `python -m evals --mode mock|live [--seeds 1,2,3] [--case SUB] [--md] [-v]`; exit 1 if any hard check fails, 2 for live without key. Report `evals/results/report_<mode>_<UTC>.json` (+ `.md` with `--md`): top-level `{mode, generated_at, seeds, summary{hard,soft,telemetry}, cases[]}`; case = `{id, seed, role, description, mode, plan, status, cap, error, latency_s, telemetry{turns,tool_calls,tokens_in,tokens_out,cost_usd,proposals}, tool_trace:[{name,input,ok,denied,ms}], scenario_ids, summary, outcomes[{class,check,args,status,detail}]}`. New hard checks: `main_unchanged` (fingerprint of main before/after), `no_scenario_leak`, `scenario_used`, `merge_conflict_reported`. CI: `.github/workflows/ci.yml` runs `pytest -m "not live"` and `python -m evals --mode mock --seeds 1,2,3`.

## Phase 5 addenda after wave 1 (data layer, versioning, evals, UI built; `uv run pytest -m "not live"`: 730 passed)
- Compare orientation (frozen): `GET /scenarios/{id}/compare?with=X` returns `a` = the path scenario, `b` = X (main or other scenario); each diff row has `before` = state on side b, `after` = state on side a; `kpis.delta = a - b`. Proposal `impact.before` = main/baseline KPIs, `impact.after` = scenario KPIs, `impact.delta = after - before`.
- A scenario's own proposals/executions appear only under that scenario's header; the main inbox (`/proposals` without `x-scenario`) lists proposals whose `scenario_id` is evidence (agent proposals tested in a scenario) with the scenario id shown, because they execute on main.
- Data layer facts (see engine/db): `ScenarioCtx(id, base_recorded, clock)`, `scoped(conn, principal, scenario=None)`; `engine.db.scenario_admin.create_base/ctx_of/purge`; `bt.list_overrides(conn, scenario_id)` (the `base` field is None unless caller is owner/admin); `bt.ScenarioLimit` (re-export from engine.scenarios); `bt.*` reads for scenario_id>0 apply the overlay rule explicitly (API/KPI layers may use `rd_eff_*` views directly). Governance-table visibility for scenario rows uses `isnad_scenario_access` (history stays readable after merge/discard); data rows use `isnad_scenario_visible` (open only); scenario writes need owner or admin (shared roles are read-only). Override `type` = ontology type / link name. Conflict detection must compare only NON-masked properties (snapshots are written from the writer's masked view).
- `engine/reason/materialize.py` `DELETE FROM inferred_support` and the `/sim/control reset` path in main.py must become `WHERE scenario_id = 0` (and reset must discard open scenarios via purge).
- Ontology versioning module exists (`engine.versioning`, `ontology_routes.make_router(get_principal, wconn)`); `apply_schema` already calls `versioning.apply`. Startup `register_current(conn, comp.yaml_text, "startup")` and `app.include_router(...)` are wired in main.py by the Action engine role.
- Eval harness: report fields, `--seeds`, `--md`, new cases and CI workflow exist (26 cases, 0 hard failures on seeds 1-3); `world_unchanged` (fingerprint) is the template for `main_unchanged`.
- Reroute cost rule (deterministic): `reroute_shipment` adds `0.02 * cargo_value` to `Shipment.extra_cost` (treat null as 0); `price_shock` multiplies `Supplier.price_index`; `extra_cost` KPI = sum of Shipment.extra_cost + sum over Supplier of (price_index - 1) * 1000.

## Phase 5 security review fixes (data layer and action engine)
Findings H1, M1-M6, L1, L2, L4-L6 of the Phase 5 security review. Interfaces below are frozen for the agent/sim/evals fixes.

**Agent identity (H1/M1).**
- `Principal.on_behalf_of: str = ""`. `engine.actions.make_agent_principal(launcher) -> Principal(role, "agent:<role>", region, approves, on_behalf_of=launcher.user)`. `scoped()` sets/clears the GUC `isnad.on_behalf_of` (only for a user that starts with `agent:`). The scenario helpers (`isnad_scenario_access/visible/owns/writable`, hence every scenario policy) treat the owner as `owner_user = isnad.user OR (user LIKE 'agent:%' AND owner_user = isnad.on_behalf_of)`; the admin shortcut does not apply to an `agent:*` user, whatever its role. New SQL helpers `isnad_is_agent()`, `isnad_owner_match(text)`.
- `scenarios.create(conn, agent_principal, ...)` makes the launcher the owner (`owner_user = on_behalf_of`); `create` with `run_id` (or an agent principal) is `kind='agent'` and counts against `ISNAD_SCN_MAX_AGENT_PER_USER` (5) of the owner, not against the human's `ISNAD_SCN_MAX_PER_USER`. `resolve/get/list_/compare/discard/record_event/execute/dry_run/propose` accept the agent principal for the launcher's scenarios. `scenarios.merge` refuses an agent (`PermissionDenied`). `execute/propose` default `on_behalf_of` to `principal.on_behalf_of`, so audit rows carry `actor='agent:<role>'`, `on_behalf_of=<human>`, `source` as passed.
- `scenarios.expire_run(conn, run_id) -> [ids]`: discards the open scenarios of a finished agent run that no proposal references (a scenario that is proposal evidence stays until it expires).
- Merge: executions whose audit says `source='agent'`, or `on_behalf_of` set, or actor `agent:*` NEVER execute directly on main; they become pending PROPOSALS (`proposer` = the original `agent:<role>` actor, `proposer_role`, `on_behalf_of` = scenario owner, `run_id` = original run, rationale `Merged from scenario N[: comment]`, `evidence = {scenario_id, origin_action_id, origin_actor}`), approved or rejected through the normal `decide` (D3/P4 apply, approval gate or not). Human executions keep running as the merging principal (gated ones become proposals as before). Replayed `audit_event` and `proposal` rows carry the new nullable column `origin_action_id uuid` and `tool_trace = {"origin": {actor, role, source, action_id, scenario_id, run_id}}`. Replay valid time is the merge time (documented, unchanged).
- Server-generated ids (`create_object.id`, `close_port.event_id`, `issue_invoice.invoice_id`) are written into the audited params at execution time, so a replay reuses them (M5). Rule-created objects without an explicit id (`create:` rule with no `id`) are not persisted this way; no shipped action uses them.
- Merge now aborts (`MergeConflict`) when an execution's action no longer exists, when a replay fails on main (criteria, not found, permission; reason `the action cannot be replayed on main: ...`), and when an action that ended links of a source (reroute) meets links of that source that were added in main after the fork (kind `link`).

**RLS write guard (M2).** Object/link write policies are `(scenario_id = 0 AND isnad_scn() = 0) OR (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))`. `bitemporal._apply_tx` raises `ValueError` when `isnad.scenario` is set and differs from the target scenario (main included), also on loader connections. A trigger (`isnad_no_scenario_write`) refuses inserts into `graph_outbox`, `webhook_outbox`, `webhook_delivery` while a scenario is active. `isnad_eff_row` and the views fail closed when `isnad.scenario_base` is empty/NULL (L1).

**Expiry (M3).** `isnad_scenario_visible` (data access) also requires `expires_at > now()`; `isnad_scenario_access` (governance history) does not, so history stays readable. `expires_in_h` is capped server side at `ISNAD_SCN_TTL_H` (default 168, never above 720). `expire_due` also runs lazily (rate limited to every 15 s per schema) on `create` and `resolve`. `/sim/control reset` purges open scenarios (unchanged). Snapshot decision: a scenario is a snapshot of main as of the fork; an owner who later loses access to a main object keeps seeing its fork-time version inside their own open scenario until it expires (7 days by default); the main rows themselves are not readable through it (documented by `test_owner_who_loses_access_keeps_the_fork_time_snapshot_until_expiry`).

**Limits (M4).** `ISNAD_SCN_MAX_ROWS` counts row versions (cloned and inserted) and is checked on every write; `scenario_state.override_rows` therefore counts versions. Props JSON per write is capped by `ISNAD_MAX_PROPS_BYTES` (65536). Scenario graph builds: global semaphore `ISNAD_SCN_MAX_GRAPH_BUILDS` (2; `GraphBusy` -> 503 `graph_busy`), `statement_timeout` `ISNAD_SCN_GRAPH_TIMEOUT_S` (120) on the build connection; links count against `ISNAD_SCN_GRAPH_MAX_OBJECTS`. API: request body limit `ISNAD_MAX_BODY_BYTES` (2 MiB, 413 `body_too_large`), at most 20 injections, `magnitude` in (0,100], `at_h`/`duration_h` finite and bounded, text fields (comment, rationale, reason) max 4000 characters, scenario name 200, ontology YAML 200000; validation errors no longer echo the input (NaN used to cause a 500); `set_supplier_price_index` also requires `$price_index < 1000`.

**Disclosure (L2).** `GET /scenarios[/{id}]` return `override_rows`, `data_version`, `run_id` only to the owner and admins (null for shared roles); the simulate status returns `digest`/`actions` only to owner/admin; a graph above the size cap returns 413 `graph_too_large` to owner/admin and 403 `graph_unavailable` (no size information) to shared roles.

**Evidence (M6).** `proposal.dry_run.scenario_data_version` records the evidence scenario's `data_version`; `decide(approve=True)` raises `StaleProposal(['scenario'])` (409 `stale_proposal`, `changed: ['scenario']`) when that scenario is still open and its `data_version` differs (via the definer helper `isnad_scenario_changed(id, dv)`); merged/discarded/expired evidence is frozen and keeps working. A role that may approve the action reads the proposal row even if the evidence scenario is not theirs (proposal and impact only).

**Audit and locking (L6).** A denied write inside a scenario by a viewer who cannot write it is audited (`status='denied'`, `scenario_id` set, inserted through the loader path, only when the scenario is visible to the caller). `discard` takes the same advisory lock as `merge`. Deadlock and lock timeouts map to 409 `busy`.

**Ontology versioning (L5).** The proposer cannot approve their own proposal (dev override `ISNAD_ONTOLOGY_SELF_APPROVE=1`); `diff()` has a new `sensitive` list (permission widening of existing roles/actions: new access, granted write, removed mask or changed row filter, added actions/approves/agent allowlist, added roles on an existing action) and approving a diff with sensitive entries needs a non-empty comment; YAML max 200000 characters, 100000 parser events, 10 aliases; `ontology_version.parent_id` has a partial unique index for `source='proposal'` (one approved child per version).

### Known limitations after the Phase 5 review (not fixed)
- L3: the what-if simulation builds its world from the full effective state and runs as the scenario owner (loader path with the scenario GUCs, `source='sim'`); the new guards confine its writes to its scenario, but it is not filtered per user.
- Scenarios are snapshots (see above); `x-role` remains a non-secret header; `audit_event.tool_trace` of merge replays holds an object (`origin`), not a list of tool calls.
- Lazy expiry and the build semaphore are per process; a multi-worker deployment would need a shared janitor.

# Phase 6 contracts (owned by the main session; frozen before implementation)

Decisions (user approved the plan and said "do all"): W1 one workspace = one Postgres schema `ws_<slug>` + one YAML + one API process (no multi-ontology process); W2 minimal real auth (`ISNAD_AUTH=1`, signed bearer tokens; the role switcher stays for dev when unset); W3 sources: CSV/TSV/zip upload, SQLite file, PostgreSQL named read-only source; W4 interfaces map to `classes`; W5 no Anthropic key available yet: quality is measured with recorded proposals (cassettes) and a heuristic baseline and labelled unverified until a live run exists; W6 generated ontologies use declarative `rules:` plus the five generic actions only (no `functions:`, no custom handlers).

## Layout
`engine/ingest/` (package; `profile/`, `sources/`, `validate.py`, `importer.py`, `proposal.py`, `schema.sql`), `engine/api/ingest_routes.py` (router, mounted by main), `agent/ontogen/`, `evals/ontogen/`, `workspaces/<slug>/{ontology.yaml,mapping.json,import_report.json}` (gitignored except the demo fixtures), `web/src/pages/Ontology*.tsx`.

## Identifiers (safety rule shared by all code)
`engine.ingest.names.safe_prop(s) -> str` (`[a-z][a-z0-9_]{0,40}`), `safe_type(s)` (`[A-Z][A-Za-z0-9]{0,40}`), `safe_link(s)` (`[a-z][a-z0-9_]{0,40}`): NFKD ASCII fold, strip control chars, non-alnum -> `_`, leading digit gets a prefix, reserved words (SQL keywords, `id`, `valid_during`, `recorded_during`, `changed_by`, `action_id`, `scenario_id`, `src`, `dst`) get a suffix, collisions get `_2`. Every identifier in generated YAML and DDL passes through these; the original text is stored only as data (`label`).

## Profile (`engine.ingest.profile.profile_source(source, *, caps, seed=1) -> Profile`, JSON-serialisable)
```
Profile {
  version: 1, source: {kind: "csv"|"sqlite"|"postgres", name: str, sha256: str, bytes: int|null},
  caps: {max_tables, max_columns, max_rows_profiled, max_sample, top_k}, truncated: bool, seed: int,
  tables: [Table], fks: [FK], warnings: [str]
}
Table { id: "t1", name: str (original, data), ident: str (safe_type), rows: int, sampled_rows: int,
  columns: [Column], key_candidates: [[col_id,...]]  (<=2 columns; unique and non-null in the profiled rows),
  is_join_table: bool }
Column { id: "t1.c3", name: str (original, data), ident: str (safe_prop), type: "int"|"float"|"bool"|"timestamp"|"string"|"geojson",
  null_rate: float, distinct: int, unique: bool, min: str|null, max: str|null, top: [[value<=80 chars, count]] (<=top_k),
  enum_candidate: [str]|null (distinct <= 20 and distinct/rows <= 0.2), format: str|null (e.g. "iso8601", "dd/mm/yyyy"),
  geo: {role:"lat"|"lon"|"point"|"wkt"|"geojson", pair: col_id|null}|null, time: bool, notes: [str] }
FK { id: "f1", from: "t2.c1", to: "t1.c1", declared: bool, containment: float (share of non-null from-values found in to),
  name_score: float, confidence: float (0..1), cardinality: "many-to-one"|"one-to-one", orphans: int }
```
All strings that originate in the data (names, values) are untrusted. The profiler never executes data content. `profile_hash = sha256(canonical json)`.
Heuristic baseline: `engine.ingest.profile.baseline_proposal(profile) -> Proposal` (below), no LLM.

## Proposal (`engine/ingest/proposal.py`; the model emits JSON, validated by `Proposal.model_validate`)
```
Proposal {
  version: 1, profile_hash: str, namespace: str (derived server side, never model-chosen),
  objectTypes: [{name, label, extends: str|null, key: [col_id,...], source_table: "t1",
    properties: [{name, type: <ontology type|{enum:[..]}>, required: bool, column: col_id|null,
                  transform: "none"|"int"|"float"|"bool"|"timestamp:<fmt>"|"enum_map:<json map>"|"point(lat_col,lon_col)"|"geojson", label}]}],
  linkTypes: [{name, from, to, cardinality, inverse: str|null, transitive: bool, required: bool,
    source: {kind:"fk", from_col: col_id, to_type_key: col_id} | {kind:"join_table", table: "t3", src_col: col_id, dst_col: col_id}}],
  classes: [{name, extends: str|null}],
  actions: [{name, description, kind:"update_props"|"set_status"|"link"|"unlink", type: str, params: {...}}]   // compiled to declarative rules by engine.ingest.proposal.to_yaml
  roles: [{name, description, access: "read"|"write"|"admin"}]                                                  // expanded from a fixed template
  rationale: str, notes: [str]
}
```
`to_yaml(proposal, profile) -> (yaml_text, mapping)` is the only producer of YAML and is deterministic. Roles are template-expanded: `admin` always (types all, links all, actions all, approves all); other roles are `read` (read all, no actions) or `write` (read all, listed generic + declared actions, no approvals); no masks/row filters are generated. Object ids are `uuid5(NAMESPACE_URL, f"{namespace}{Type}/{key values joined by \x1f}")`. `mapping.json` = `{version:1, profile_hash, source, types:{Type:{table, key_cols, columns:{prop:{col, transform}}}}, links:{link:{...}}}`.

## Validation (`engine.ingest.validate.validate(profile, proposal, source, *, caps) -> ValidationReport`)
Runs only in scratch schema `gen_<8 hex>` (dropped in `finally`; stale ones swept by age). Checks and thresholds (defaults, env-overridable): `schema_ok`, `ddl_ok`, `key_violations == 0`, `required_null_rate <= 0.0` on required props (or the model must relax `required`), `type_coercion_fail_rate <= 0.02`, `enum_violation_rate <= 0.02`, `link_resolution >= 0.95` per link, `shacl_conforms`, `reasoner_consistent` (materialize completes, inferred triples <= 50x asserted). `ValidationReport {ok: bool, checks: [{name, ok, value, threshold, detail, examples: [<=3 sanitised]}], stats: {rows_loaded:{Type:n}, links_loaded:{l:n}}, seconds}`.

## Ingest tables (host schema, append-only, FORCE RLS, admin only; `engine/ingest/schema.sql`, applied from `apply_schema`)
`ingest_session(id uuid pk, created_by, created_at, source_kind, source_name, source_sha256, profile jsonb, profile_hash)`, `ingest_proposal(id uuid pk, session_id, seq int, parent_id uuid null, proposal jsonb, yaml text, mapping jsonb, validation jsonb, model text, cost_usd numeric, created_by, created_at, kind 'baseline'|'model'|'repair'|'edit')`, `ingest_decision(id uuid pk, proposal_id, decision 'approved'|'rejected', items jsonb (per item accept/reject/edit), comment, approver, approver_role, at, workspace text null, import_action_id uuid null)`. Source bytes are stored under `ISNAD_INGEST_DIR` (default `var/ingest/<session>/`) with a sha256; the profile and import always re-read these stored bytes (tamper check: hash must match at import).

## Import (`engine.ingest.importer.run_import(conn_factory, decision_id, principal) -> ImportResult`)
Preconditions: decision approved by an admin who is not the session creator (when auth is on), validation of the FINAL yaml+mapping re-run and ok, source hash matches. Steps in one owner transaction in the new schema `ws_<slug>`: create schema, `apply_schema`, `register_current(..., source='import')`, `bt.bulk_load_objects/links` with deterministic ids and one shared `action_id`, endpoint pre-check (unresolved links dropped and counted, never silently), ONE `audit_event(name='import_dataset', source='import', role='owner', actor=<approver>, params={counts, source_sha256, profile_hash, ontology_hash, proposal_id, decision_id, dropped_links})`, then write `workspaces/<slug>/`. Idempotent: same decision + same ids re-run is a no-op (`already_imported`). Cleans up the schema on failure. Exact row counts are in `ImportResult` and `import_report.json`.

## API (admin only, 404 for others; all behind `Principal`; body limit for upload `ISNAD_MAX_UPLOAD_BYTES` default 20 MiB, raw body, no multipart dependency)
`POST /ingest/uploads?filename=` (raw body; csv/tsv/zip/sqlite) -> 201 `{session_id, profile_summary}`; `GET /ingest/sources` -> named SQL sources `[name, kind]` (no DSNs); `POST /ingest/sources/{name}/profile` -> 201 `{session_id, ...}`; `GET /ingest/sessions[/{id}]` -> session, profile, proposals; `POST /ingest/sessions/{id}/proposals {mode:"baseline"|"model"}` -> 202 `{job_id}` (model needs a key, else 503 `agent_unavailable`; baseline always works); `GET /ingest/jobs/{id}` (+ SSE `/stream`) -> `{status, events, proposal_id, cost_usd}`; `POST /ingest/proposals/{id}/repair` -> 202; `PATCH /ingest/proposals/{id}/items {items:[{path, action:"accept"|"reject"|"edit", value?}]}` -> 201 new proposal `kind='edit'` (re-validated); `GET /ingest/proposals/{id}` -> `{proposal, yaml, mapping, validation, diff}` (diff against empty or against an existing workspace ontology via `engine.versioning.diff`); `POST /ingest/proposals/{id}/approve {comment}` -> 201 decision (needs comment when `sensitive` entries); `POST /ingest/decisions/{id}/import` -> 202 `{job_id}`; `GET /workspaces` -> `[{slug, schema, ontology_hash, imported_at, rows, api_base|null}]`.
Caps (env): `ISNAD_INGEST_MAX_TABLES` 50, `_MAX_COLUMNS` 60 per table, `_MAX_ROWS_PROFILED` 200000, `_MAX_SAMPLE` 5000, `_MAX_ZIP_MEMBERS` 100, `_MAX_ZIP_RATIO` 50, `ISNAD_INGEST_SESSIONS_PER_USER` 10, generator model: 8 turns, 30000 output tokens, USD 1.00 per proposal, 3 repair rounds (max 5), daily USD cap shared with `ISNAD_AGENT_DAILY_USD`.
Named SQL sources: `ISNAD_IMPORT_SOURCES="name=postgresql://..."` or `name=sqlite:/abs/path` (server side only).

## Generator agent (`agent/ontogen/`)
Tools: `get_profile(table_id)`, `get_samples(column_id, k<=10)`, `check_proposal(proposal)` (validation in scratch schema, result envelope), `submit_proposal(proposal)` (terminal). Tool results use the existing untrusted envelope; `injection.scan` runs on every data string; data strings are truncated (names 64, values 80), control chars stripped, and are never placed in the system prompt. The model sees column ids, not raw SQL or file paths. No other tools. Mock model: `agent.mock_model.MockModel` with cassettes under `evals/ontogen/cassettes/`.

## Generalisation contract (workspace processes)
- `create_app(ontology_path, schema, ...)` stays one-ontology; everything that used supply-chain defaults takes them from the loaded `Ontology`: namespace, link semantics, shapes/ttl paths per workspace (`build/` for the default, `workspaces/<slug>/build/` otherwise), agent prompt name (`ontology.name` or namespace host), `GEO_TYPES` = object types with a point/linestring property, `/stats/summary` generic shape `{as_of, counts:{Type:n}, enums:{Type:{prop:{value:n}}}, kpis:{...}}` (the legacy keys stay for the supply-chain ontology: its YAML declares `kpis:`), `/sim/*` returns 404 `{code:'sim_unavailable'}` when the ontology has no `sim:` block. YAML additions: top-level `name`, `kpis: {<key>: {description, sql|expr}}` (declarative, compiled, no raw SQL from generated ontologies), `sim: {module: sim.world}` (supply chain only), per-property `volatile: true`.
- `GET /types` returns, per type: properties with types/enums, `geo: bool`, `volatile`, links in/out; the UI builds its pages from this, not from `@sdk/client` constants (`SELECTABLE_ROLES`, `ACTION_META` come from `/roles` and `/actions`).

## Auth (W2; `engine/auth/`, wired into `get_principal`)
`ISNAD_AUTH=1` enables it; `ISNAD_AUTH_SECRET` (required, >= 32 bytes) signs tokens; users from `ISNAD_USERS_FILE` (JSON `{"users":[{"user":"alice","role":"planner","password_hash":"scrypt$..."}]}`; `python -m engine.auth hash` helper). `POST /auth/login {user,password}` -> `{token, user, role, expires_at}` (HMAC-SHA256 signed `v1.<b64 payload>.<sig>`, TTL `ISNAD_AUTH_TTL_H` 12, constant-time compare, per-IP and per-user login rate limit); `Authorization: Bearer`. With auth on, `x-role` is ignored (never trusted), `user` is the token's user (so launcher/approver separation is real), a role not matching a known role is refused, `sim` can never be logged in. With auth off (default) behaviour is unchanged (dev role switcher). `/ingest/*` and `/ontology/*` additionally require `ISNAD_ALLOW_IMPORT=1` when auth is off.

## Docs/polish contract
README.md (architecture diagram as Mermaid + ASCII fallback), `scripts/setup.sh` (idempotent: docker compose up, wait healthy, `uv sync`, compile, seed, print URLs) and `scripts/dev.sh` (API + UI), `scripts/demo_phase6.py` (mock model default, `--live`), `workspaces/` demo fixtures `evals/ontogen/datasets/{bikeshare,messy_supply,injection}/`.

## Phase 6 addenda after implementation (differences from the frozen contract above)
- Mount: `/ingest/*` and `/workspaces` exist only when `ISNAD_ALLOW_IMPORT=1`, in every mode; `GET /health` reports `import_enabled`, `auth`. Workspace processes run with it 0.
- Jobs: `queued|running|done|failed|cancelled`; events `{seq, kind, payload}` (max 500 per job); owner-only access (404 otherwise); `GET /ingest/jobs/{id}?after=`, `POST .../cancel`, SSE `.../stream` optional (UI polls).
- PATCH items paths are JSON pointers into the Proposal; list elements by index or `name`, `-` appends; renames cascade. `/approve` returns `{decision_id}`, refuses non-accept items (422 `use_edit`), `comment_required` (422) for a first import, 409 `rejected|already_decided|validation_failed`, 403 `self_approval` only with auth on. One approval per proposal (unique index); import refuses (409 `rejected_after_approval`) if a rejection is newer.
- Import result is not stored on `ingest_decision` (append-only): idempotency and the result live in a host `audit_event` row (`import_dataset`, params include `decision_id`, `workspace`, counts, dropped links/objects) next to the workspace's own audit row; `workspaces/<slug>/import_report.json` also written. `GET /workspaces` rows carry `status: ready|orphan`.
- New append-only ledger `ingest_spend` (reserve then settle per model job); caps `ISNAD_INGEST_GLOBAL_DAILY_USD`. Validation adds checks `name_collisions`, `codegen_ok`, `source_ok`, `load_ok`, `time_budget`.
- `/stats/summary` generic shape `{as_of, counts, enums, kpis}` plus legacy keys when the supply-chain types exist; `/types` extended (`name`, `kpis`, `sim`, per type `geo`, `volatile`, `links`); `/sim/*` 404 `sim_unavailable` when the ontology has no `sim:`; `/agent/runs/{id}` paged (`after`, `limit`, `next`).
- Auth endpoints actually implemented: `GET /auth/config`, `POST /auth/login|logout|stream-ticket`. Dormant unless `ISNAD_AUTH=1`.
- Security review of the import path (Phase 6): no SQL/identifier/YAML/path/SSRF injection found; fixed budget ledger, enforced review decisions, mount gating, prompt-injection boundary (injection-like or invisible-character text rejected in generated names, enums and labels; prompt renders ontology strings as quoted data), TS codegen escaping, reserved names plus `codegen_ok`, error-text leakage, CSV width bombs, job ownership. Accepted: L4 (PostgreSQL source fingerprint is structure only), L5 (auth in-memory limits).
