-- Hand-written fixture following docs/contracts.md (one object type, one link type).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE obj_supplier (
  id uuid NOT NULL,
  name text NOT NULL,
  tier int,
  status text CHECK (status IN ('active','paused')),
  valid_during tstzrange NOT NULL,
  recorded_during tstzrange NOT NULL,
  changed_by text NOT NULL,
  action_id uuid NULL,
  scenario_id int NOT NULL DEFAULT 0,
  EXCLUDE USING gist (scenario_id WITH =, id WITH =, valid_during WITH &&, recorded_during WITH &&)
);
CREATE INDEX ON obj_supplier (id);

CREATE TABLE link_supplies (
  src uuid NOT NULL,
  dst uuid NOT NULL,
  valid_during tstzrange NOT NULL,
  recorded_during tstzrange NOT NULL,
  changed_by text NOT NULL,
  action_id uuid NULL,
  scenario_id int NOT NULL DEFAULT 0,
  EXCLUDE USING gist (scenario_id WITH =, src WITH =, dst WITH =, valid_during WITH &&, recorded_during WITH &&)
);
CREATE INDEX ON link_supplies (src);
CREATE INDEX ON link_supplies (dst);

CREATE VIEW cur_obj_supplier AS SELECT * FROM obj_supplier
  WHERE upper_inf(recorded_during) AND valid_during @> now() AND scenario_id = 0;
CREATE VIEW cur_link_supplies AS SELECT * FROM link_supplies
  WHERE upper_inf(recorded_during) AND valid_during @> now() AND scenario_id = 0;

CREATE TABLE graph_outbox (
  seq bigserial PRIMARY KEY, kind text, type text, id uuid, src uuid, dst uuid, op text,
  at timestamptz DEFAULT now()
);
CREATE TABLE ontology_registry (hash text PRIMARY KEY, yaml text, applied_at timestamptz);
