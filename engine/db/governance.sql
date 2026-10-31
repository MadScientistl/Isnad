-- Phase 3 governance tables. Applied with the target schema first on search_path. Idempotent.
-- Append-only tables: BEFORE UPDATE/DELETE/TRUNCATE triggers raise for EVERY role (owner included),
-- FORCE RLS, isnad_app has INSERT/SELECT only. There is deliberately no escape hatch;
-- DROP SCHEMA ... CASCADE is the only way to remove rows.

-- BEGIN scenario_core
-- Everything the compiler's generated DDL (eff_* views, scenario policies) depends on. The compiler splices this
-- section verbatim into build/schema.sql so a schema built from the compiler output alone is self-sufficient;
-- keep it idempotent and free of references to anything outside the section.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'isnad_app') THEN
    CREATE ROLE isnad_app NOLOGIN NOBYPASSRLS;
  END IF;
EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
END $$;
DO $$ BEGIN EXECUTE format('GRANT USAGE ON SCHEMA %I TO isnad_app', current_schema()); END $$;
-- isnad_app may not create temp tables (a temp table could shadow an unqualified relation); the loader
-- (connection user) keeps the privilege.
DO $$
DECLARE n int := 0;
BEGIN
  LOOP
    BEGIN
      EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
      EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM isnad_app', current_database());
      EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO %I', current_database(), current_user);
      EXIT;
    EXCEPTION WHEN internal_error THEN
      -- "tuple concurrently updated": another session is applying the same grants; retry briefly
      n := n + 1;
      IF n >= 20 THEN RAISE; END IF;
      PERFORM pg_sleep(0.05 * n);
    END;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION isnad_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'integrity_constraint_violation';
END $$;


-- helpers: GUC readers (invoker, inlinable)
CREATE OR REPLACE FUNCTION isnad_scn() RETURNS int LANGUAGE sql STABLE AS $fn$
  SELECT coalesce(nullif(current_setting('isnad.scenario', true), '')::int, 0) $fn$;
CREATE OR REPLACE FUNCTION isnad_scn_base() RETURNS timestamptz LANGUAGE sql STABLE AS $fn$
  SELECT nullif(current_setting('isnad.scenario_base', true), '')::timestamptz $fn$;
CREATE OR REPLACE FUNCTION isnad_now() RETURNS timestamptz LANGUAGE sql STABLE AS $fn$
  SELECT coalesce(nullif(current_setting('isnad.now', true), '')::timestamptz, now()) $fn$;

-- main-only tables (graph_outbox, webhook_outbox/delivery): inserts are refused while a scenario is active
CREATE OR REPLACE FUNCTION isnad_no_scenario_write() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF coalesce(nullif(current_setting('isnad.scenario', true), ''), '0') <> '0' THEN
    RAISE EXCEPTION '% is main-only: no writes while a scenario is active', TG_TABLE_NAME
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $fn$;

-- scenario tables (scenario, scenario_event: append-only; scenario_state, scenario_override: engine-mutable)
CREATE TABLE IF NOT EXISTS scenario (
    id serial PRIMARY KEY CHECK (id > 0),
    name text NOT NULL,
    owner_user text NOT NULL,
    owner_role text NOT NULL,
    parent_id int NULL,
    base_recorded timestamptz NOT NULL,
    base_sim_time timestamptz NULL,
    shared_roles text[] NOT NULL DEFAULT '{}',
    run_id text NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NULL
);
-- 'user' | 'agent' (opened by a runtime-agent run: counted in a separate quota)
ALTER TABLE scenario ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'user';
CREATE INDEX IF NOT EXISTS scenario_owner_idx ON scenario (owner_user);
CREATE TABLE IF NOT EXISTS scenario_event (
    seq bigserial PRIMARY KEY,
    scenario_id int NOT NULL,
    kind text NOT NULL CHECK (kind IN ('created','merged','discarded','expired','simulated','proposal')),
    actor text NOT NULL,
    payload jsonb,
    at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS scenario_event_scn_idx ON scenario_event (scenario_id, seq);
CREATE TABLE IF NOT EXISTS scenario_state (
    scenario_id int PRIMARY KEY,
    clock timestamptz NULL,
    data_version bigint NOT NULL DEFAULT 0,
    override_rows int NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scenario_override (
    scenario_id int NOT NULL,
    kind text NOT NULL CHECK (kind IN ('obj','link')),
    type text NOT NULL,
    key_a uuid NOT NULL,
    key_b uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    deleted boolean NOT NULL DEFAULT false,
    base jsonb,
    PRIMARY KEY (scenario_id, kind, type, key_a, key_b)
);
CREATE INDEX IF NOT EXISTS scenario_override_scn_idx ON scenario_override (scenario_id);

-- access / visibility / writability. SECURITY DEFINER (they read scenario tables whatever the caller may see).
-- `current_setting('role') = 'none'` is the loader/owner connection (never SET ROLE isnad_app): owner code is
-- not subject to RLS anyway. A scoped request has role = isnad_app and needs an identified isnad.role.
-- Phase 5 review (H1): a runtime-agent identity (isnad.user = 'agent:<role>') owns the scenarios of the human it
-- acts for (isnad.on_behalf_of, set only by principal.scoped()); it never gets the admin shortcut.
CREATE OR REPLACE FUNCTION isnad_is_agent() RETURNS boolean LANGUAGE sql STABLE AS $fn$
  SELECT coalesce(current_setting('isnad.user', true), '') LIKE 'agent:%' $fn$;
CREATE OR REPLACE FUNCTION isnad_owner_match(p_owner text) RETURNS boolean LANGUAGE sql STABLE AS $fn$
  SELECT p_owner = current_setting('isnad.user', true)
      OR (isnad_is_agent() AND coalesce(current_setting('isnad.on_behalf_of', true), '') <> ''
          AND p_owner = current_setting('isnad.on_behalf_of', true)) $fn$;
CREATE OR REPLACE FUNCTION isnad_scenario_access(p_id int) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
SELECT p_id = 0 OR current_setting('role') = 'none' OR (
  coalesce(current_setting('isnad.role', true), '') <> '' AND EXISTS (
    SELECT 1 FROM scenario s WHERE s.id = p_id AND (
         (current_setting('isnad.role', true) = 'admin' AND NOT isnad_is_agent())
      OR isnad_owner_match(s.owner_user)
      OR current_setting('isnad.role', true) = ANY (s.shared_roles)))) $fn$;
-- visible = access AND open (no terminal event) AND not past its expiry (an expired scenario is closed for data
-- access even before the janitor has marked it; its governance history stays readable through _access)
CREATE OR REPLACE FUNCTION isnad_scenario_visible(p_id int) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
SELECT p_id = 0 OR (isnad_scenario_access(p_id) AND NOT EXISTS (
    SELECT 1 FROM scenario_event e WHERE e.scenario_id = p_id AND e.kind IN ('merged','discarded','expired'))
    AND EXISTS (SELECT 1 FROM scenario s WHERE s.id = p_id AND (s.expires_at IS NULL OR s.expires_at > now()))) $fn$;
CREATE OR REPLACE FUNCTION isnad_scenario_owns(p_id int) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
SELECT p_id = 0 OR current_setting('role') = 'none' OR (
  coalesce(current_setting('isnad.role', true), '') <> '' AND EXISTS (
    SELECT 1 FROM scenario s WHERE s.id = p_id AND (
         (current_setting('isnad.role', true) = 'admin' AND NOT isnad_is_agent())
      OR isnad_owner_match(s.owner_user)))) $fn$;
CREATE OR REPLACE FUNCTION isnad_scenario_writable(p_id int) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
SELECT p_id = 0 OR (isnad_scenario_owns(p_id) AND isnad_scenario_visible(p_id)) $fn$;
CREATE OR REPLACE FUNCTION isnad_scenario_overrides(p_id int)
RETURNS TABLE (scenario_id int, kind text, type text, key_a uuid, key_b uuid, deleted boolean, base jsonb)
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
SELECT o.scenario_id, o.kind, o.type, o.key_a, o.key_b, o.deleted,
       CASE WHEN isnad_scenario_owns(p_id) THEN o.base END
  FROM scenario_override o WHERE o.scenario_id = p_id AND p_id > 0 AND isnad_scenario_visible(p_id)
 ORDER BY o.kind, o.type, o.key_a, o.key_b $fn$;
DO $do$ DECLARE f text; BEGIN
  FOREACH f IN ARRAY ARRAY['isnad_scenario_access(int)','isnad_scenario_visible(int)','isnad_scenario_overrides(int)',
                           'isnad_scenario_owns(int)','isnad_scenario_writable(int)'] LOOP
    EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, %I, public, pg_temp', f, current_schema());
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO isnad_app', f);
  END LOOP;
  FOREACH f IN ARRAY ARRAY['isnad_scn()','isnad_scn_base()','isnad_now()','isnad_is_agent()','isnad_owner_match(text)'] LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO isnad_app', f);
  END LOOP;
END $do$;

-- row-level overlay rule as one function call (see compiler exprs.eff_pred_fn). INVOKER: reads scenario_override
-- under the caller's RLS. Fails closed when the scenario in the GUC is not visible to the caller.
CREATE OR REPLACE FUNCTION isnad_eff_row(p_kind text, p_type text, p_sid int, p_valid tstzrange,
                                        p_rec tstzrange, p_a uuid, p_b uuid) RETURNS boolean
LANGUAGE plpgsql STABLE AS $fn$
DECLARE scn int := isnad_scn(); nw timestamptz;
BEGIN
  IF scn = 0 THEN
    RETURN p_sid = 0 AND (upper_inf(p_rec) OR upper(p_rec) = 'infinity') AND p_valid @> now();
  END IF;
  IF NOT coalesce(isnad_scenario_visible(scn), false) THEN RETURN false; END IF;
  nw := isnad_now();
  -- fail closed: a NULL/empty fork base or clock must never make a superseded version appear
  IF NOT coalesce(p_valid @> nw, false) THEN RETURN false; END IF;
  IF p_sid = scn THEN RETURN upper_inf(p_rec) OR upper(p_rec) = 'infinity'; END IF;
  IF p_sid <> 0 OR NOT coalesce(p_rec @> isnad_scn_base(), false) THEN RETURN false; END IF;
  RETURN NOT EXISTS (SELECT 1 FROM scenario_override o WHERE o.scenario_id = scn AND o.kind = p_kind
                     AND o.type = p_type AND o.key_a = p_a AND (p_kind = 'obj' OR o.key_b = p_b));
END $fn$;
GRANT EXECUTE ON FUNCTION isnad_eff_row(text, text, int, tstzrange, tstzrange, uuid, uuid) TO isnad_app;

-- append-only: scenario, scenario_event
DO $$
DECLARE t text; p record; owner_role text := current_user;
BEGIN
  FOREACH t IN ARRAY ARRAY['scenario','scenario_event'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS isnad_no_update ON %I', t);
    EXECUTE format('CREATE TRIGGER isnad_no_update BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION isnad_append_only()', t);
    EXECUTE format('DROP TRIGGER IF EXISTS isnad_no_truncate ON %I', t);
    EXECUTE format('CREATE TRIGGER isnad_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION isnad_append_only()', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['scenario','scenario_event','scenario_state','scenario_override'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    FOR p IN SELECT policyname FROM pg_policies WHERE schemaname = current_schema() AND tablename = t LOOP
      EXECUTE format('DROP POLICY %I ON %I', p.policyname, t);
    END LOOP;
    EXECUTE format('CREATE POLICY owner_all ON %I AS PERMISSIVE FOR ALL TO %I USING (true) WITH CHECK (true)', t, owner_role);
  END LOOP;
  GRANT SELECT, INSERT ON scenario, scenario_event TO isnad_app;
  GRANT SELECT, INSERT, UPDATE ON scenario_state TO isnad_app;
  -- `base` (fork-time property snapshot, written from the writer's view) is NOT column-readable: shared roles
  -- must not read an admin's unmasked values. It is read through isnad_scenario_overrides() (owner/admin only).
  REVOKE SELECT, UPDATE ON scenario_override FROM isnad_app;
  GRANT INSERT ON scenario_override TO isnad_app;
  GRANT SELECT (scenario_id, kind, type, key_a, key_b, deleted) ON scenario_override TO isnad_app;
  GRANT UPDATE (deleted) ON scenario_override TO isnad_app;
  REVOKE DELETE, TRUNCATE ON scenario, scenario_event, scenario_state, scenario_override FROM isnad_app;
  EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO isnad_app', pg_get_serial_sequence('scenario', 'id'));
  EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO isnad_app', pg_get_serial_sequence('scenario_event', 'seq'));
END $$;
CREATE POLICY sel ON scenario FOR SELECT TO isnad_app USING (isnad_scenario_access(id));
CREATE POLICY ins ON scenario FOR INSERT TO isnad_app WITH CHECK (
    coalesce(current_setting('isnad.role', true), '') <> ''
    AND isnad_owner_match(owner_user) AND owner_role = current_setting('isnad.role', true));
CREATE POLICY sel ON scenario_event FOR SELECT TO isnad_app USING (isnad_scenario_access(scenario_id));
CREATE POLICY ins ON scenario_event FOR INSERT TO isnad_app WITH CHECK (
    coalesce(current_setting('isnad.role', true), '') <> '' AND actor = current_setting('isnad.user', true)
    AND isnad_scenario_owns(scenario_id));
CREATE POLICY sel ON scenario_state FOR SELECT TO isnad_app USING (isnad_scenario_access(scenario_id));
CREATE POLICY ins ON scenario_state FOR INSERT TO isnad_app WITH CHECK (isnad_scenario_owns(scenario_id));
CREATE POLICY upd ON scenario_state FOR UPDATE TO isnad_app USING (isnad_scenario_writable(scenario_id))
    WITH CHECK (isnad_scenario_writable(scenario_id));
CREATE POLICY sel ON scenario_override FOR SELECT TO isnad_app USING (isnad_scenario_visible(scenario_id));
CREATE POLICY ins ON scenario_override FOR INSERT TO isnad_app WITH CHECK (
    scenario_id > 0 AND scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id));
CREATE POLICY upd ON scenario_override FOR UPDATE TO isnad_app USING (
    scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))
    WITH CHECK (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id));
-- END scenario_core

-- review M6: has the open evidence scenario changed since the proposal saw data_version p_dv? (definer: an approver
-- who may not read the scenario learns only this boolean)
CREATE OR REPLACE FUNCTION isnad_scenario_changed(p_id int, p_dv bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
SELECT coalesce((SELECT st.data_version <> p_dv FROM scenario_state st WHERE st.scenario_id = p_id
                  AND NOT EXISTS (SELECT 1 FROM scenario_event e WHERE e.scenario_id = p_id
                                  AND e.kind IN ('merged','discarded','expired'))), false) $fn$;
DO $do$ BEGIN
  EXECUTE format('ALTER FUNCTION isnad_scenario_changed(int, bigint) SET search_path = pg_catalog, %I, public, pg_temp', current_schema());
  REVOKE ALL ON FUNCTION isnad_scenario_changed(int, bigint) FROM PUBLIC;
  GRANT EXECUTE ON FUNCTION isnad_scenario_changed(int, bigint) TO isnad_app;
END $do$;

-- ---------------------------------------------------------------- tables
CREATE TABLE IF NOT EXISTS audit_event (
    seq bigserial PRIMARY KEY,
    action_id uuid NOT NULL,
    name text NOT NULL,
    actor text NOT NULL,
    source text NOT NULL,
    run_id text,
    params jsonb,
    sim_time timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    before jsonb,
    after jsonb
);
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS on_behalf_of text;
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS proposal_id uuid;
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'executed';
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS tool_trace jsonb;
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS parent_execution_id uuid;
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS scenario_id int NOT NULL DEFAULT 0;
-- provenance of a replayed (scenario merge) execution/proposal: the original scenario action's id
ALTER TABLE audit_event ADD COLUMN IF NOT EXISTS origin_action_id uuid;
CREATE INDEX IF NOT EXISTS audit_event_sim_time_idx ON audit_event (sim_time);
CREATE INDEX IF NOT EXISTS audit_event_recorded_at_idx ON audit_event (recorded_at);
CREATE INDEX IF NOT EXISTS audit_event_action_id_idx ON audit_event (action_id);
CREATE INDEX IF NOT EXISTS audit_event_actor_idx ON audit_event (actor);
CREATE INDEX IF NOT EXISTS audit_event_name_idx ON audit_event (name);
CREATE INDEX IF NOT EXISTS audit_event_proposal_idx ON audit_event (proposal_id) WHERE proposal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS proposal (
    id uuid PRIMARY KEY,
    seq bigserial,
    action text NOT NULL,
    params jsonb,
    proposer text NOT NULL,
    proposer_role text NOT NULL,
    rationale text,
    dry_run jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    run_id text,
    tool_trace jsonb
);
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS on_behalf_of text;
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS evidence jsonb;
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS base_versions jsonb;
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS scenario_id int NOT NULL DEFAULT 0;  -- EVIDENCE scenario; executes on main
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS origin_action_id uuid;  -- merge: the original scenario action id
CREATE UNIQUE INDEX IF NOT EXISTS proposal_idem_idx ON proposal (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS proposal_proposer_idx ON proposal (proposer);
CREATE INDEX IF NOT EXISTS proposal_seq_idx ON proposal (seq);

CREATE TABLE IF NOT EXISTS approval (
    id uuid PRIMARY KEY,
    seq bigserial,
    proposal_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    approver text NOT NULL,
    approver_role text NOT NULL,
    comment text,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    execution_id uuid NULL,
    -- one decision per proposal (a proposal is pending iff it has no approval row)
    CONSTRAINT approval_one_decision UNIQUE (proposal_id)
);
CREATE INDEX IF NOT EXISTS approval_approver_idx ON approval (approver);

CREATE TABLE IF NOT EXISTS execution_diff (
    seq bigserial PRIMARY KEY,
    action_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('object', 'link')),
    type text NOT NULL,
    key text NOT NULL,
    op text NOT NULL CHECK (op IN ('create', 'modify', 'delete')),
    before jsonb,
    after jsonb
);
ALTER TABLE execution_diff ADD COLUMN IF NOT EXISTS scenario_id int NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS execution_diff_action_idx ON execution_diff (action_id);

CREATE TABLE IF NOT EXISTS rollback (
    id uuid PRIMARY KEY,
    seq bigserial,
    original_action_id uuid NOT NULL,
    rollback_action_id uuid NOT NULL,
    actor text NOT NULL,
    reason text,
    at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE rollback ADD COLUMN IF NOT EXISTS scenario_id int NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS rollback_original_idx ON rollback (original_action_id);

CREATE TABLE IF NOT EXISTS webhook_outbox (
    id uuid PRIMARY KEY,
    seq bigserial,
    action_id uuid NOT NULL,
    hook text NOT NULL,
    url text,
    payload jsonb,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS webhook_outbox_action_idx ON webhook_outbox (action_id);

CREATE TABLE IF NOT EXISTS webhook_delivery (
    seq bigserial PRIMARY KEY,
    outbox_id uuid NOT NULL,
    attempt int NOT NULL DEFAULT 0,
    status text NOT NULL CHECK (status IN ('pending', 'delivered', 'retry', 'dead')),
    http_status int,
    error text,
    next_attempt_at timestamptz,
    at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS webhook_delivery_outbox_idx ON webhook_delivery (outbox_id, seq DESC);
CREATE INDEX IF NOT EXISTS webhook_delivery_due_idx ON webhook_delivery (next_attempt_at)
    WHERE status IN ('pending', 'retry');

DROP TRIGGER IF EXISTS isnad_no_scn ON webhook_outbox;
CREATE TRIGGER isnad_no_scn BEFORE INSERT ON webhook_outbox FOR EACH ROW EXECUTE FUNCTION isnad_no_scenario_write();
DROP TRIGGER IF EXISTS isnad_no_scn ON webhook_delivery;
CREATE TRIGGER isnad_no_scn BEFORE INSERT ON webhook_delivery FOR EACH ROW EXECUTE FUNCTION isnad_no_scenario_write();

-- Phase 4 agent audit: a run row plus an event stream (status = last 'finished' event; no updates needed).
CREATE TABLE IF NOT EXISTS agent_run (
    id uuid PRIMARY KEY,
    seq bigserial,
    role text NOT NULL,
    "user" text NOT NULL,
    prompt text NOT NULL,
    model text,
    caps jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS scenario_id int NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS agent_run_user_idx ON agent_run ("user", seq DESC);
CREATE TABLE IF NOT EXISTS agent_run_event (
    seq bigserial PRIMARY KEY,
    run_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('message','tool_call','tool_result','denied','proposal','finished')),
    payload jsonb,
    tokens_in int NOT NULL DEFAULT 0,
    tokens_out int NOT NULL DEFAULT 0,
    cost_usd numeric NOT NULL DEFAULT 0,
    at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS agent_run_event_run_idx ON agent_run_event (run_id, seq);

-- latest delivery row per outbox id (no DISTINCT, so FOR UPDATE SKIP LOCKED works on it)
CREATE OR REPLACE VIEW webhook_state WITH (security_invoker = true) AS
  SELECT d.* FROM webhook_delivery d
  WHERE NOT EXISTS (SELECT 1 FROM webhook_delivery n WHERE n.outbox_id = d.outbox_id AND n.seq > d.seq);

-- NOT append-only: derived, rebuilt by the reasoner / cleared by reset.
CREATE TABLE IF NOT EXISTS inferred_support (
    link text NOT NULL,
    src uuid NOT NULL,
    dst uuid NOT NULL,
    derivation_no int NOT NULL,
    support uuid[] NOT NULL,
    data_version bigint,
    scenario_id int NOT NULL DEFAULT 0,
    PRIMARY KEY (scenario_id, link, src, dst, derivation_no)
);
-- upgrade of a pre-Phase-5 table: add the column and move the primary key (idempotent)
ALTER TABLE inferred_support ADD COLUMN IF NOT EXISTS scenario_id int NOT NULL DEFAULT 0;
DO $$ DECLARE cn text; BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                  WHERE c.conrelid = to_regclass('inferred_support') AND c.contype = 'p' AND a.attname = 'scenario_id') THEN
    SELECT conname INTO cn FROM pg_constraint WHERE conrelid = to_regclass('inferred_support') AND contype = 'p';
    IF cn IS NOT NULL THEN EXECUTE format('ALTER TABLE inferred_support DROP CONSTRAINT %I', cn); END IF;
    ALTER TABLE inferred_support ADD PRIMARY KEY (scenario_id, link, src, dst, derivation_no);
  END IF;
END $$;

-- ------------------------------------------------- append-only + RLS + grants
DO $$
DECLARE t text; p record; owner_role text := current_user;
BEGIN
  FOREACH t IN ARRAY ARRAY['audit_event','proposal','approval','execution_diff','rollback',
                           'webhook_outbox','webhook_delivery','agent_run','agent_run_event'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS isnad_no_update ON %I', t);
    EXECUTE format('CREATE TRIGGER isnad_no_update BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION isnad_append_only()', t);
    EXECUTE format('DROP TRIGGER IF EXISTS isnad_no_truncate ON %I', t);
    EXECUTE format('CREATE TRIGGER isnad_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION isnad_append_only()', t);
    EXECUTE format('GRANT INSERT, SELECT ON %I TO isnad_app', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['audit_event','proposal','approval','execution_diff','rollback',
                           'webhook_outbox','webhook_delivery','inferred_support','agent_run','agent_run_event'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    FOR p IN SELECT policyname FROM pg_policies WHERE schemaname = current_schema() AND tablename = t LOOP
      EXECUTE format('DROP POLICY %I ON %I', p.policyname, t);
    END LOOP;
    -- the owner/loader role (not BYPASSRLS outside dev superusers) keeps full access; triggers still bind it
    EXECUTE format('CREATE POLICY owner_all ON %I AS PERMISSIVE FOR ALL TO %I USING (true) WITH CHECK (true)', t, owner_role);
  END LOOP;
  GRANT SELECT ON webhook_state TO isnad_app;
  GRANT SELECT ON inferred_support TO isnad_app;
END $$;

-- identity helpers are inlined as current_setting(...) calls; unset/empty role => no rows, no writes.
-- INSERT: any identified role.
CREATE POLICY ins_known ON audit_event      FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '' AND (scenario_id = 0 OR (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))));
CREATE POLICY ins_known ON proposal         FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '' AND (scenario_id = 0 OR (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))));
CREATE POLICY ins_known ON approval         FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '');
CREATE POLICY ins_known ON execution_diff   FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '' AND (scenario_id = 0 OR (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))));
CREATE POLICY ins_known ON rollback         FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '' AND (scenario_id = 0 OR (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))));
CREATE POLICY ins_known ON webhook_outbox   FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '');
CREATE POLICY ins_known ON webhook_delivery FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting('isnad.role', true), '') <> '');
-- a run row can only be written for the caller's own identity; events only into a run the caller can see
CREATE POLICY ins_known ON agent_run        FOR INSERT TO isnad_app WITH CHECK (
    coalesce(current_setting('isnad.role', true), '') <> '' AND "user" = current_setting('isnad.user', true)
    AND role = current_setting('isnad.role', true)
    AND (scenario_id = 0 OR (scenario_id = isnad_scn() AND isnad_scenario_writable(scenario_id))));
CREATE POLICY ins_known ON agent_run_event  FOR INSERT TO isnad_app WITH CHECK (
    coalesce(current_setting('isnad.role', true), '') <> ''
    AND EXISTS (SELECT 1 FROM agent_run r WHERE r.id = agent_run_event.run_id
                AND r."user" = current_setting('isnad.user', true)));
-- a run is closed by its "finished" event: nothing can be appended after it (definer: must see all events)
CREATE OR REPLACE FUNCTION isnad_run_open() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM agent_run_event f WHERE f.run_id = NEW.run_id AND f.kind = 'finished') THEN
        RAISE EXCEPTION 'agent run % is already finished', NEW.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS isnad_run_open ON agent_run_event;
CREATE TRIGGER isnad_run_open BEFORE INSERT ON agent_run_event FOR EACH ROW EXECUTE FUNCTION isnad_run_open();
CREATE POLICY sel ON agent_run FOR SELECT TO isnad_app USING ((scenario_id = 0 OR isnad_scenario_access(scenario_id)) AND (
    current_setting('isnad.role', true) = 'admin'
 OR (coalesce(current_setting('isnad.role', true), '') <> '' AND "user" = current_setting('isnad.user', true))));
CREATE POLICY sel ON agent_run_event FOR SELECT TO isnad_app USING (
    EXISTS (SELECT 1 FROM agent_run r WHERE r.id = agent_run_event.run_id));

-- SELECT. Policy dependency order (acyclic): proposal <- approval <- audit_event <- {execution_diff, rollback,
-- webhook_outbox} <- webhook_delivery. Each EXISTS subquery is itself subject to the target's policy.
-- proposal: proposer, or a role that may approve the action (GUC isnad.approves = comma list, set by scoped()).
-- (an approver of the action may read the proposal row even when the evidence scenario is not theirs: they see
-- the proposal and its impact, never the scenario's data)
CREATE POLICY sel ON proposal FOR SELECT TO isnad_app USING ((scenario_id = 0 OR isnad_scenario_access(scenario_id)
 OR action = ANY (string_to_array(coalesce(current_setting('isnad.approves', true), ''), ','))) AND (
    current_setting('isnad.role', true) = 'admin'
 OR (coalesce(current_setting('isnad.role', true), '') <> '' AND (
        proposer = current_setting('isnad.user', true)
     OR on_behalf_of = current_setting('isnad.user', true)
     OR action = ANY (string_to_array(coalesce(current_setting('isnad.approves', true), ''), ','))
 ))));
CREATE POLICY sel ON approval FOR SELECT TO isnad_app USING (
    current_setting('isnad.role', true) = 'admin'
 OR (coalesce(current_setting('isnad.role', true), '') <> '' AND (
        approver = current_setting('isnad.user', true)
     OR EXISTS (SELECT 1 FROM proposal p WHERE p.id = approval.proposal_id)
 )));
CREATE POLICY sel ON audit_event FOR SELECT TO isnad_app USING ((scenario_id = 0 OR isnad_scenario_access(scenario_id)) AND (
    current_setting('isnad.role', true) = 'admin'
 OR (coalesce(current_setting('isnad.role', true), '') <> '' AND (
        actor = current_setting('isnad.user', true)
     OR on_behalf_of = current_setting('isnad.user', true)
     OR EXISTS (SELECT 1 FROM approval a WHERE a.execution_id = audit_event.action_id
                AND a.approver = current_setting('isnad.user', true))
 ))));
CREATE POLICY sel ON execution_diff FOR SELECT TO isnad_app USING ((scenario_id = 0 OR isnad_scenario_access(scenario_id)) AND
    EXISTS (SELECT 1 FROM audit_event e WHERE e.action_id = execution_diff.action_id));
CREATE POLICY sel ON rollback FOR SELECT TO isnad_app USING ((scenario_id = 0 OR isnad_scenario_access(scenario_id)) AND (
    current_setting('isnad.role', true) = 'admin'
 OR (coalesce(current_setting('isnad.role', true), '') <> '' AND (
        actor = current_setting('isnad.user', true)
     OR EXISTS (SELECT 1 FROM audit_event e WHERE e.action_id = rollback.original_action_id)
 ))));
CREATE POLICY sel ON webhook_outbox FOR SELECT TO isnad_app USING (
    EXISTS (SELECT 1 FROM audit_event e WHERE e.action_id = webhook_outbox.action_id));
CREATE POLICY sel ON webhook_delivery FOR SELECT TO isnad_app USING (
    EXISTS (SELECT 1 FROM webhook_outbox o WHERE o.id = webhook_delivery.outbox_id));
-- inferred_support: a row is readable only if EVERY node id it mentions (support = all nodes of the
-- derivation, src/dst included) is visible to the caller, so a scoped SQL reader can never read a hidden
-- node uuid from it. isnad_support_visible is SECURITY INVOKER on purpose: its probe of the cur_obj_* views
-- therefore runs as isnad_app under the object RLS policies (no second policy implementation). It is dynamic
-- (views looked up in the current schema) so this file stays independent of the compiler DDL. Admin sees all.
CREATE OR REPLACE FUNCTION isnad_support_visible(p_support uuid[]) RETURNS boolean
LANGUAGE plpgsql STABLE AS $fn$
DECLARE q text; n int; want int;
BEGIN
  SELECT string_agg(format('SELECT id FROM %I.%I WHERE id = ANY($1)', ns.nspname, c.relname), ' UNION ')
    INTO q
    FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = current_schema() AND c.relkind = 'v' AND c.relname LIKE 'cur\_obj\_%';
  IF q IS NULL THEN RETURN false; END IF;
  SELECT count(DISTINCT x) INTO want FROM unnest(p_support) x;
  EXECUTE 'SELECT count(*) FROM (' || q || ') s' INTO n USING p_support;
  RETURN n = want;
END $fn$;
GRANT EXECUTE ON FUNCTION isnad_support_visible(uuid[]) TO isnad_app;
-- scenario variant: probes the eff_obj_* views (scenario-effective objects) instead of cur_obj_*
CREATE OR REPLACE FUNCTION isnad_support_visible_eff(p_support uuid[]) RETURNS boolean
LANGUAGE plpgsql STABLE AS $fn$
DECLARE q text; n int; want int;
BEGIN
  SELECT string_agg(format('SELECT id FROM %I.%I WHERE id = ANY($1)', ns.nspname, c.relname), ' UNION ')
    INTO q
    FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = current_schema() AND c.relkind = 'v' AND c.relname LIKE 'eff\_obj\_%';
  IF q IS NULL THEN RETURN false; END IF;
  SELECT count(DISTINCT x) INTO want FROM unnest(p_support) x;
  EXECUTE 'SELECT count(*) FROM (' || q || ') s' INTO n USING p_support;
  RETURN n = want;
END $fn$;
GRANT EXECUTE ON FUNCTION isnad_support_visible_eff(uuid[]) TO isnad_app;
CREATE POLICY sel ON inferred_support FOR SELECT TO isnad_app USING (
    CASE WHEN scenario_id = 0 THEN
        current_setting('isnad.role', true) = 'admin'
     OR (coalesce(current_setting('isnad.role', true), '') <> '' AND isnad_support_visible(support))
    ELSE scenario_id = isnad_scn() AND isnad_scenario_visible(scenario_id)
     AND (current_setting('isnad.role', true) = 'admin'
          OR (coalesce(current_setting('isnad.role', true), '') <> '' AND isnad_support_visible_eff(support)))
    END);

-- sequences
DO $$ DECLARE s record; BEGIN
  FOR s IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind = 'S' AND n.nspname = current_schema() LOOP
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO isnad_app', s.relname);
  END LOOP;
END $$;
