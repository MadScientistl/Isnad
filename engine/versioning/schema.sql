-- Ontology versioning tables (Phase 5). Applied with the target schema first on search_path, after
-- governance.sql (needs isnad_app and isnad_append_only). Idempotent. Append-only, FORCE RLS, admin-only SELECT.

CREATE TABLE IF NOT EXISTS ontology_version (
    id serial PRIMARY KEY,
    hash text NOT NULL,
    parent_hash text,
    yaml text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source text NOT NULL CHECK (source IN ('startup', 'proposal', 'rollback')),
    note text
);
-- not UNIQUE: a rollback legitimately re-registers an older definition (see engine/versioning docstring)
CREATE INDEX IF NOT EXISTS ontology_version_hash_idx ON ontology_version (hash);

-- review L5: a version row has ONE approved child (concurrent approvals against the same latest version cannot
-- both win). parent_id = id of the latest version at approval time (a rollback may re-register an older hash,
-- so the hash alone cannot be unique).
ALTER TABLE ontology_version ADD COLUMN IF NOT EXISTS parent_id int;
CREATE UNIQUE INDEX IF NOT EXISTS ontology_version_parent_proposal_idx ON ontology_version (parent_id)
    WHERE source = 'proposal' AND parent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ontology_change_proposal (
    id uuid PRIMARY KEY,
    base_hash text NOT NULL,
    yaml text NOT NULL,
    diff jsonb NOT NULL,
    breaking bool NOT NULL,
    rationale text,
    proposer text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS ontology_change_decision (
    id serial PRIMARY KEY,
    proposal_id uuid NOT NULL UNIQUE,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    approver text NOT NULL,
    comment text,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    version_id int
);

DO $$
DECLARE t text; p record; owner_role text := current_user;
BEGIN
  FOREACH t IN ARRAY ARRAY['ontology_version','ontology_change_proposal','ontology_change_decision'] LOOP
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

DO $$ DECLARE s record; BEGIN
  FOR s IN SELECT c.relname FROM pg_class c WHERE c.relkind = 'S' AND c.relnamespace = current_schema()::regnamespace
           AND c.relname IN ('ontology_version_id_seq','ontology_change_decision_id_seq') LOOP
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO isnad_app', s.relname);
  END LOOP;
END $$;
