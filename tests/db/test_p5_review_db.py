"""Phase 5 security-review fixes at the data layer: RLS write guard (M2), fail-closed eff_row (L1),
row-version cap and props size cap (M4a), expiry on use (M3), agent identity ownership (H1)."""
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from engine.db import OWNER, Principal, bitemporal as bt, connect, get_dsn, scenario_admin, scoped
from engine.db.setup import apply_schema

NOW = datetime.now(UTC)
T0 = NOW - timedelta(days=10)
W = "t-writer"
PLAN = Principal("planner", "u-plan", "")
OTHER = Principal("planner", "u-other", "")
ADMIN = Principal("admin", "u-admin", "")
AGENT = Principal("planner", "agent:planner", "", (), "u-plan")
AGENT_ADMIN = Principal("admin", "agent:admin", "", (), "u-plan")


@pytest.fixture(scope="module")
def schema_name():
    name = f"rv_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def c(schema_name):
    conn = connect(schema=schema_name)
    yield conn
    conn.rollback()
    for t in ("obj_port", "obj_warehouse", "graph_outbox", "scenario_override", "scenario_state"):
        conn.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(t)))
    conn.commit()
    conn.close()


def fork(c, owner="u-plan", role="planner", **kw) -> int:
    return scenario_admin.create_base(c, name="s", owner_user=owner, owner_role=role, **kw)["id"]


def port(c, id_, name="P", vf=T0, **kw):
    return bt.write_object(c, "Port", id_, {"name": name, "region": "EMEA", "status": "open"}, vf, W, **kw)


# ---------------------------------------------------------------- M2: write guard
def test_scoped_scenario_session_cannot_write_main_rows(c):
    sid = fork(c)
    ctx = scenario_admin.ctx_of(c, sid)
    with scoped(c, PLAN, ctx):
        # direct SQL: a main row (scenario_id 0) cannot be inserted while a scenario is active (RLS WITH CHECK)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute("INSERT INTO obj_port (id, name, region, status, valid_during, recorded_during, changed_by, "
                          "scenario_id) VALUES (%s,'x','EMEA','open', tstzrange(now(), NULL), tstzrange(now(), NULL),"
                          " 'u', 0)", [uuid.uuid4()])
        # and the python path refuses to write main from a connection scoped to a scenario
        with pytest.raises(ValueError):
            port(c, uuid.uuid4(), "main-from-scn")


def test_owner_connection_scoped_to_scenario_x_cannot_write_scenario_y(c):
    x, y = fork(c), fork(c)
    cx = scenario_admin.ctx_of(c, x)
    with scoped(c, OWNER, cx):
        with pytest.raises(ValueError):
            port(c, uuid.uuid4(), "to-y", scenario_id=y)
        with pytest.raises(ValueError):
            port(c, uuid.uuid4(), "to-main")
        assert port(c, uuid.uuid4(), "to-x", scenario_id=x)


def test_outbox_triggers_refuse_inserts_while_a_scenario_is_active(c):
    sid = fork(c)
    ctx = scenario_admin.ctx_of(c, sid)
    with scoped(c, OWNER, ctx):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            with c.transaction():
                c.execute("INSERT INTO graph_outbox (kind, type, id, op) VALUES ('obj','Port',%s,'upsert')",
                          [uuid.uuid4()])
        for ddl in ("INSERT INTO webhook_outbox (id, action_id, hook, idempotency_key) VALUES (%s,%s,'h',%s)",):
            with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
                with c.transaction():
                    c.execute(ddl, [uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())])
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            with c.transaction():
                c.execute("INSERT INTO webhook_delivery (outbox_id, attempt, status) VALUES (%s,0,'pending')",
                          [uuid.uuid4()])
    # outside a scenario the same insert is fine (main path unchanged)
    c.execute("INSERT INTO graph_outbox (kind, type, id, op) VALUES ('obj','Port',%s,'upsert')", [uuid.uuid4()])


# ---------------------------------------------------------------- L1: fail closed
def test_forged_scenario_base_or_now_never_exposes_superseded_versions(c):
    a = uuid.uuid4()
    port(c, a, "v1")
    sid = fork(c)
    port(c, a, "v2")                                  # main moves on after the fork (v1 superseded in recorded time)
    ctx = scenario_admin.ctx_of(c, sid)
    with scoped(c, OWNER, ctx):
        assert [r[0] for r in c.execute("SELECT name FROM eff_obj_port").fetchall()] == ["v1"]
    with c.transaction():
        c.execute("SELECT set_config('isnad.scenario', %s, true), set_config('isnad.scenario_base', '', true), "
                  "set_config('isnad.now', '', true)", [str(sid)])
        # inline view predicate AND the plpgsql helper both fail closed with an empty fork base
        assert c.execute("SELECT count(*) FROM eff_obj_port").fetchone()[0] == 0
        row = c.execute("SELECT isnad_eff_row('obj', 'Port', 0, tstzrange(now() - interval '1 day', NULL), "
                        "tstzrange(now() - interval '2 day', now() - interval '1 hour'), %s, NULL)",
                        [a]).fetchone()[0]
        assert row is False


# ---------------------------------------------------------------- M4a: row-version cap, props size
def test_cap_counts_row_versions_not_keys(c, monkeypatch):
    a = uuid.uuid4()
    port(c, a, "base")                                # one main version
    sid = fork(c)
    monkeypatch.setenv("ISNAD_SCN_MAX_ROWS", "3")
    port(c, a, "s1", scenario_id=sid)                 # clone (1 version replaced -> 0 cloned) + 1 inserted = 1
    port(c, a, "s2", scenario_id=sid)                 # +1 = 2
    port(c, a, "s3", scenario_id=sid)                 # +1 = 3
    assert c.execute("SELECT override_rows FROM scenario_state WHERE scenario_id=%s", [sid]).fetchone()[0] == 3
    with pytest.raises(bt.ScenarioLimit):
        port(c, a, "s4", scenario_id=sid)             # same key, still capped: every write is checked
    assert bt.get_object(c, "Port", a, scenario_id=sid)["name"] == "s3"   # the refused write left nothing behind


def test_clone_counts_the_cloned_versions(c, monkeypatch):
    a = uuid.uuid4()
    port(c, a, "v1", vf=T0)
    port(c, a, "v2", vf=NOW - timedelta(days=5))      # two valid-time versions in main
    sid = fork(c)
    monkeypatch.setenv("ISNAD_SCN_MAX_ROWS", "2")
    # editing a future slice clones both versions (2) and inserts one more -> over the cap of 2
    with pytest.raises(bt.ScenarioLimit):
        port(c, a, "v3", vf=NOW + timedelta(days=1), scenario_id=sid)


def test_props_json_size_is_capped(c):
    with pytest.raises(ValueError, match="too large"):
        bt.write_object(c, "Port", uuid.uuid4(), {"name": "x" * 70_000, "region": "EMEA", "status": "open"}, T0, W)


# ---------------------------------------------------------------- M3: expiry on use
def test_expired_scenario_is_closed_for_data_but_history_stays_readable(c):
    a = uuid.uuid4()
    port(c, a, "base")
    sid = fork(c, expires_at=datetime.now(UTC) + timedelta(hours=1))
    ctx = scenario_admin.ctx_of(c, sid)
    port(c, a, "scn", scenario_id=sid)
    with scoped(c, PLAN, ctx):
        assert c.execute("SELECT isnad_scenario_visible(%s)", [sid]).fetchone()[0] is True
    # the scenario table is append-only: make a second scenario that is already expired at creation
    sid2 = fork(c, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    ctx2 = scenario_admin.ctx_of(c, sid2)
    with scoped(c, PLAN, ctx2):
        assert c.execute("SELECT isnad_scenario_visible(%s)", [sid2]).fetchone()[0] is False
        assert c.execute("SELECT isnad_scenario_access(%s)", [sid2]).fetchone()[0] is True      # history readable
        assert c.execute("SELECT count(*) FROM eff_obj_port").fetchone()[0] == 0                 # data closed
        assert c.execute("SELECT isnad_scenario_writable(%s)", [sid2]).fetchone()[0] is False


# ---------------------------------------------------------------- H1: agent identity
def test_agent_identity_owns_only_the_launchers_scenarios_and_is_never_admin(c):
    sid = fork(c)                                      # owned by u-plan
    other = fork(c, owner="u-other")
    ctx = scenario_admin.ctx_of(c, sid)
    with scoped(c, AGENT, ctx):
        assert c.execute("SELECT isnad_scenario_visible(%s), isnad_scenario_writable(%s)", [sid, sid]).fetchone() \
            == (True, True)
        assert c.execute("SELECT isnad_scenario_visible(%s)", [other]).fetchone()[0] is False
    # an agent acting for someone else does not own u-plan's scenario
    with scoped(c, Principal("planner", "agent:planner", "", (), "u-other"), ctx):
        assert c.execute("SELECT isnad_scenario_visible(%s)", [sid]).fetchone()[0] is False
    # no admin shortcut for an agent identity, even for the admin role
    with scoped(c, AGENT_ADMIN, scenario_admin.ctx_of(c, other)):
        assert c.execute("SELECT isnad_scenario_visible(%s)", [other]).fetchone()[0] is False
    with scoped(c, ADMIN, scenario_admin.ctx_of(c, other)):
        assert c.execute("SELECT isnad_scenario_visible(%s)", [other]).fetchone()[0] is True
    # on_behalf_of is honoured ONLY for an agent:* user (a human principal cannot borrow another owner)
    with scoped(c, Principal("planner", "u-evil", "", (), "u-plan"), ctx):
        assert c.execute("SELECT isnad_scenario_visible(%s)", [sid]).fetchone()[0] is False
    # the agent writes the scenario rows of its launcher
    a = uuid.uuid4()
    with scoped(c, AGENT, ctx):
        assert bt.write_object(c, "Port", a, {"name": "by-agent", "region": "EMEA", "status": "open"}, T0,
                               "agent:planner", scenario_id=sid)
    assert bt.get_object(c, "Port", a, scenario_id=sid)["name"] == "by-agent"
    assert bt.get_object(c, "Port", a) is None
