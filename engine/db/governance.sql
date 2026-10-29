-- Phase 3 governance tables. Applied with the target schema first on search_path. Idempotent.
-- Append-only tables: BEFORE UPDATE/DELETE/TRUNCATE triggers raise for EVERY role (owner included),
-- FORCE RLS, amarh_app has INSERT/SELECT only. There is deliberately no escape hatch;
-- DROP SCHEMA ... CASCADE is the only way to remove rows.

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'amarh_app') THEN
    CREATE ROLE amarh_app NOLOGIN NOBYPASSRLS;
  END IF;
EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
END $$;
DO $$ BEGIN EXECUTE format('GRANT USAGE ON SCHEMA %I TO amarh_app', current_schema()); END $$;
-- amarh_app may not create temp tables (a temp table could shadow an unqualified relation); the loader
-- (connection user) keeps the privilege.
DO $$ BEGIN
  EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM amarh_app', current_database());
  EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO %I', current_database(), current_user);
END $$;

CREATE OR REPLACE FUNCTION amarh_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'integrity_constraint_violation';
END $$;

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
    PRIMARY KEY (link, src, dst, derivation_no)
);

-- ------------------------------------------------- append-only + RLS + grants
DO $$
DECLARE t text; p record; owner_role text := current_user;
BEGIN
  FOREACH t IN ARRAY ARRAY['audit_event','proposal','approval','execution_diff','rollback',
                           'webhook_outbox','webhook_delivery'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS amarh_no_update ON %I', t);
    EXECUTE format('CREATE TRIGGER amarh_no_update BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION amarh_append_only()', t);
    EXECUTE format('DROP TRIGGER IF EXISTS amarh_no_truncate ON %I', t);
    EXECUTE format('CREATE TRIGGER amarh_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION amarh_append_only()', t);
    EXECUTE format('GRANT INSERT, SELECT ON %I TO amarh_app', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['audit_event','proposal','approval','execution_diff','rollback',
                           'webhook_outbox','webhook_delivery','inferred_support'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    FOR p IN SELECT policyname FROM pg_policies WHERE schemaname = current_schema() AND tablename = t LOOP
      EXECUTE format('DROP POLICY %I ON %I', p.policyname, t);
    END LOOP;
    -- the owner/loader role (not BYPASSRLS outside dev superusers) keeps full access; triggers still bind it
    EXECUTE format('CREATE POLICY owner_all ON %I AS PERMISSIVE FOR ALL TO %I USING (true) WITH CHECK (true)', t, owner_role);
  END LOOP;
  GRANT SELECT ON webhook_state TO amarh_app;
  GRANT SELECT ON inferred_support TO amarh_app;
END $$;

-- identity helpers are inlined as current_setting(...) calls; unset/empty role => no rows, no writes.
-- INSERT: any identified role.
CREATE POLICY ins_known ON audit_event      FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');
CREATE POLICY ins_known ON proposal         FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');
CREATE POLICY ins_known ON approval         FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');
CREATE POLICY ins_known ON execution_diff   FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');
CREATE POLICY ins_known ON rollback         FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');
CREATE POLICY ins_known ON webhook_outbox   FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');
CREATE POLICY ins_known ON webhook_delivery FOR INSERT TO amarh_app WITH CHECK (coalesce(current_setting('amarh.role', true), '') <> '');

-- SELECT. Policy dependency order (acyclic): proposal <- approval <- audit_event <- {execution_diff, rollback,
-- webhook_outbox} <- webhook_delivery. Each EXISTS subquery is itself subject to the target's policy.
-- proposal: proposer, or a role that may approve the action (GUC amarh.approves = comma list, set by scoped()).
CREATE POLICY sel ON proposal FOR SELECT TO amarh_app USING (
    current_setting('amarh.role', true) = 'admin'
 OR (coalesce(current_setting('amarh.role', true), '') <> '' AND (
        proposer = current_setting('amarh.user', true)
     OR action = ANY (string_to_array(coalesce(current_setting('amarh.approves', true), ''), ','))
 )));
CREATE POLICY sel ON approval FOR SELECT TO amarh_app USING (
    current_setting('amarh.role', true) = 'admin'
 OR (coalesce(current_setting('amarh.role', true), '') <> '' AND (
        approver = current_setting('amarh.user', true)
     OR EXISTS (SELECT 1 FROM proposal p WHERE p.id = approval.proposal_id)
 )));
CREATE POLICY sel ON audit_event FOR SELECT TO amarh_app USING (
    current_setting('amarh.role', true) = 'admin'
 OR (coalesce(current_setting('amarh.role', true), '') <> '' AND (
        actor = current_setting('amarh.user', true)
     OR on_behalf_of = current_setting('amarh.user', true)
     OR EXISTS (SELECT 1 FROM approval a WHERE a.execution_id = audit_event.action_id
                AND a.approver = current_setting('amarh.user', true))
 )));
CREATE POLICY sel ON execution_diff FOR SELECT TO amarh_app USING (
    EXISTS (SELECT 1 FROM audit_event e WHERE e.action_id = execution_diff.action_id));
CREATE POLICY sel ON rollback FOR SELECT TO amarh_app USING (
    current_setting('amarh.role', true) = 'admin'
 OR (coalesce(current_setting('amarh.role', true), '') <> '' AND (
        actor = current_setting('amarh.user', true)
     OR EXISTS (SELECT 1 FROM audit_event e WHERE e.action_id = rollback.original_action_id)
 )));
CREATE POLICY sel ON webhook_outbox FOR SELECT TO amarh_app USING (
    EXISTS (SELECT 1 FROM audit_event e WHERE e.action_id = webhook_outbox.action_id));
CREATE POLICY sel ON webhook_delivery FOR SELECT TO amarh_app USING (
    EXISTS (SELECT 1 FROM webhook_outbox o WHERE o.id = webhook_delivery.outbox_id));
-- inferred_support: a row is readable only if EVERY node id it mentions (support = all nodes of the
-- derivation, src/dst included) is visible to the caller, so a scoped SQL reader can never read a hidden
-- node uuid from it. amarh_support_visible is SECURITY INVOKER on purpose: its probe of the cur_obj_* views
-- therefore runs as amarh_app under the object RLS policies (no second policy implementation). It is dynamic
-- (views looked up in the current schema) so this file stays independent of the compiler DDL. Admin sees all.
CREATE OR REPLACE FUNCTION amarh_support_visible(p_support uuid[]) RETURNS boolean
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
GRANT EXECUTE ON FUNCTION amarh_support_visible(uuid[]) TO amarh_app;
CREATE POLICY sel ON inferred_support FOR SELECT TO amarh_app USING (
    current_setting('amarh.role', true) = 'admin'
 OR (coalesce(current_setting('amarh.role', true), '') <> '' AND amarh_support_visible(support)));

-- sequences
DO $$ DECLARE s record; BEGIN
  FOR s IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind = 'S' AND n.nspname = current_schema() LOOP
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO amarh_app', s.relname);
  END LOOP;
END $$;
