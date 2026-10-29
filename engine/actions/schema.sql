-- Append-only audit log of applied actions. The code never UPDATEs or DELETEs it.
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
CREATE INDEX IF NOT EXISTS audit_event_sim_time_idx ON audit_event (sim_time);
CREATE INDEX IF NOT EXISTS audit_event_recorded_at_idx ON audit_event (recorded_at);
CREATE INDEX IF NOT EXISTS audit_event_action_id_idx ON audit_event (action_id);
