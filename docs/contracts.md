# Phase 1 contracts (owned by the main session)

Python 3.12 via `uv` (`uv run pytest`). Postgres 18 (no PostGIS in Phase 1): `docker compose up -d postgres`,
DSN `postgresql://amarh:amarh@localhost:55441/amarh`. Extension `btree_gist` required.

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
- `POST /sim/control`: a non-paused speed starts the runner thread, `paused` stops it. `reset` is refused (403) unless `AMARH_ALLOW_RESET=1`; when allowed it truncates obj_*, link_*, graph_outbox and audit_event in the API's schema, reseeds, rebuilds the sync store and resets the runner.
- The API creates its `SyncPipeline` lazily, syncs and reasons once on first use, and `/graph` (live) syncs again so rows written by another process (headless sim) appear.
- Benchmark on the PostGIS schema: `bench/results/report_phase2.md` (materialize 18.35 s, 4-hop p95 0.211 s, SHACL 0.30 s, all PASS).
