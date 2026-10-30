"""Owner-side scenario data helpers (Phase 5): create the base rows, resolve a ctx, purge a scenario.

The full service (`engine.scenarios`: limits, sharing, merge, compare, expiry) builds on these. Everything
here runs as the connection user or, for `create_base`, as a scoped principal whose identity matches the
owner arguments (the RLS insert policies enforce that); nothing commits.
"""
from __future__ import annotations

from datetime import datetime

from psycopg import sql
from psycopg.types.json import Jsonb

from .principal import ScenarioCtx


def create_base(conn, *, name: str, owner_user: str, owner_role: str, shared_roles=(), parent_id: int | None = None,
                run_id: str | None = None, expires_at: datetime | None = None,
                base_sim_time: datetime | None = None, clock: datetime | None = None) -> dict:
    """Insert the `scenario` row (base_recorded = now, wall clock), its `scenario_state` row and the
    'created' event in the caller's transaction. Returns the scenario row as a dict (id, base_recorded,
    clock, ...). The fork is a snapshot: main rows recorded after `base_recorded` are invisible to it."""
    row = conn.execute(
        "INSERT INTO scenario (name, owner_user, owner_role, parent_id, base_recorded, base_sim_time, "
        "shared_roles, run_id, expires_at) VALUES (%s,%s,%s,%s,clock_timestamp(),%s,%s,%s,%s) "
        "RETURNING id, base_recorded, created_at", [name, owner_user, owner_role, parent_id, base_sim_time,
                                                    list(shared_roles), run_id, expires_at]).fetchone()
    sid = row[0]
    conn.execute("INSERT INTO scenario_state (scenario_id, clock) VALUES (%s,%s)", [sid, clock])
    conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES (%s,'created',%s,%s)",
                 [sid, owner_user, Jsonb({"name": name})])
    return {"id": sid, "name": name, "owner_user": owner_user, "owner_role": owner_role, "parent_id": parent_id,
            "base_recorded": row[1], "base_sim_time": base_sim_time, "shared_roles": list(shared_roles),
            "run_id": run_id, "created_at": row[2], "expires_at": expires_at, "clock": clock}


def ctx_of(conn, scenario_id: int) -> ScenarioCtx:
    """ScenarioCtx from the tables (trusted callers only: owner code, tests, the service after a visibility check)."""
    row = conn.execute(
        "SELECT s.base_recorded, st.clock FROM scenario s LEFT JOIN scenario_state st ON st.scenario_id = s.id "
        "WHERE s.id = %s", [scenario_id]).fetchone()
    if row is None:
        raise LookupError(f"scenario {scenario_id} not found")
    return ScenarioCtx(scenario_id, row[0], row[1])


def purge(conn, scenario_id: int) -> dict[str, int]:
    """Owner-only janitor: delete the scenario's object/link rows, override rows and inferred_support rows.
    Audit tables, the scenario row, its events and its state row stay (the state counters are reset).
    Refuses scenario 0 (main) and refuses to run as `amarh_app`. Returns {table: rows deleted}."""
    if type(scenario_id) is not int or scenario_id <= 0:
        raise ValueError("purge needs a scenario id > 0 (main cannot be purged)")
    if conn.execute("SELECT current_user = 'amarh_app' OR current_setting('role') <> 'none'").fetchone()[0]:
        raise PermissionError("purge is an owner-only operation")
    tables = [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND "
        "(tablename LIKE 'obj\\_%%' OR tablename LIKE 'link\\_%%') ORDER BY 1").fetchall()]
    tables += ["scenario_override", "inferred_support"]
    out: dict[str, int] = {}
    for t in tables:
        cur = conn.execute(sql.SQL("DELETE FROM {} WHERE scenario_id = %s").format(sql.Identifier(t)), [scenario_id])
        if cur.rowcount:
            out[t] = cur.rowcount
    conn.execute("UPDATE scenario_state SET override_rows = 0, data_version = data_version + 1 "
                 "WHERE scenario_id = %s", [scenario_id])
    return out
