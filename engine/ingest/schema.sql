-- Phase 6 ingest tables (host schema). Append-only (UPDATE/DELETE/TRUNCATE rejected), FORCE row-level security,
-- admin-only: the isnad_app role can SELECT/INSERT only when isnad.role = 'admin'. Idempotent.
CREATE TABLE IF NOT EXISTS ingest_session (
    id uuid PRIMARY KEY,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_kind text NOT NULL CHECK (source_kind IN ('csv', 'sqlite', 'postgres')),
    source_name text NOT NULL,
    source_sha256 text NOT NULL,
    profile jsonb NOT NULL,
    profile_hash text NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_proposal (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES ingest_session (id),
    seq int NOT NULL,
    parent_id uuid REFERENCES ingest_proposal (id),
    proposal jsonb NOT NULL,
    yaml text NOT NULL,
    mapping jsonb NOT NULL,
    validation jsonb,
    model text,
    cost_usd numeric NOT NULL DEFAULT 0,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    kind text NOT NULL CHECK (kind IN ('baseline', 'model', 'repair', 'edit')),
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS ingest_proposal_session_idx ON ingest_proposal (session_id, seq);

CREATE TABLE IF NOT EXISTS ingest_decision (
    id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL REFERENCES ingest_proposal (id),
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    items jsonb NOT NULL DEFAULT '[]'::jsonb,
    comment text,
    approver text NOT NULL,
    approver_role text NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    workspace text,
    import_action_id uuid
);
CREATE INDEX IF NOT EXISTS ingest_decision_proposal_idx ON ingest_decision (proposal_id);

-- Model-spend ledger: every model job writes a 'reserved' row (its maximum cost) when it starts and a settling row
-- (done / failed / cancelled / error, actual or worst-case cost) in a finally. A reservation is counted only until its
-- job has a settling row. Failed and cancelled runs therefore still count against the daily budget.
CREATE TABLE IF NOT EXISTS ingest_spend (
    id uuid PRIMARY KEY,
    username text NOT NULL,
    job_id text NOT NULL,
    cost_usd numeric NOT NULL CHECK (cost_usd >= 0),
    status text NOT NULL CHECK (status IN ('reserved', 'done', 'failed', 'cancelled', 'error', 'rejected')),
    at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ingest_spend_user_idx ON ingest_spend (username, at);
CREATE INDEX IF NOT EXISTS ingest_spend_job_idx ON ingest_spend (job_id);

-- At most one approval per proposal (reviewers change a proposal by editing it, which creates a new proposal).
CREATE UNIQUE INDEX IF NOT EXISTS ingest_decision_one_approval ON ingest_decision (proposal_id) WHERE decision = 'approved';

DO $$
DECLARE t text; p record; owner_role text := current_user;
BEGIN
  FOREACH t IN ARRAY ARRAY['ingest_session','ingest_proposal','ingest_decision','ingest_spend'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS isnad_no_update ON %I', t);
    EXECUTE format('CREATE TRIGGER isnad_no_update BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION isnad_append_only()', t);
    EXECUTE format('DROP TRIGGER IF EXISTS isnad_no_truncate ON %I', t);
    EXECUTE format('CREATE TRIGGER isnad_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION isnad_append_only()', t);
    EXECUTE format('GRANT INSERT, SELECT ON %I TO isnad_app', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    FOR p IN SELECT policyname FROM pg_policies WHERE schemaname = current_schema() AND tablename = t LOOP
      EXECUTE format('DROP POLICY %I ON %I', p.policyname, t);
    END LOOP;
    EXECUTE format('CREATE POLICY owner_all ON %I AS PERMISSIVE FOR ALL TO %I USING (true) WITH CHECK (true)', t, owner_role);
    EXECUTE format('CREATE POLICY admin_sel ON %I FOR SELECT TO isnad_app USING (coalesce(current_setting(''isnad.role'', true), '''') = ''admin'')', t);
    EXECUTE format('CREATE POLICY admin_ins ON %I FOR INSERT TO isnad_app WITH CHECK (coalesce(current_setting(''isnad.role'', true), '''') = ''admin'')', t);
  END LOOP;
END $$;
