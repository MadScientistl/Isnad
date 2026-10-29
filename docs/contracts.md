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
