"""Scenario overlay semantics of the data layer (Phase 5): fork snapshot, clone-on-first-touch, tombstones,
history, caps, main immutability. Owner connection (no RLS) against a schema built from build/schema.sql."""
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from engine.db import OWNER, Principal, ScenarioCtx, bitemporal as bt, connect, get_dsn, scenario_admin, scoped
from engine.db.setup import apply_schema

NOW = datetime.now(UTC)
T0 = NOW - timedelta(days=10)
W = "t-writer"
TABLES = ("obj_port", "obj_warehouse", "obj_shipment", "link_ships_to", "link_ships_via", "graph_outbox",
          "scenario_override", "scenario_state", "inferred_support")


@pytest.fixture(scope="module")
def schema_name():
    name = f"sc_{uuid.uuid4().hex[:10]}"
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
    for t in TABLES:
        conn.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(t)))
    conn.commit()
    conn.close()


def fork(c, name="s", clock=None) -> int:
    return scenario_admin.create_base(c, name=name, owner_user="u-plan", owner_role="planner", clock=clock)["id"]


def port(c, id_, name="P", region="EMEA", status="open", vf=T0, **kw):
    return bt.write_object(c, "Port", id_, {"name": name, "region": region, "status": status}, vf, W, **kw)


def checksum(c) -> str:
    parts = []
    for t in ("obj_port", "obj_warehouse", "obj_shipment", "link_ships_to", "link_ships_via"):
        parts.append(c.execute(sql.SQL("SELECT coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') "
                                       "FROM {} x WHERE scenario_id = 0").format(sql.Identifier(t))).fetchone()[0])
    return "|".join(parts)


def test_overlay_read_and_merge_rule(c):
    a, b = uuid.uuid4(), uuid.uuid4()
    port(c, a, "A"); port(c, b, "B")
    sid = fork(c)
    port(c, a, "A-scn", scenario_id=sid)
    assert bt.get_object(c, "Port", a, scenario_id=sid)["name"] == "A-scn"
    assert bt.get_object(c, "Port", a)["name"] == "A"
    assert bt.get_object(c, "Port", b, scenario_id=sid)["name"] == "B"       # untouched: main as of fork
    names = {r["name"] for r in bt.list_objects(c, "Port", scenario_id=sid)}
    assert names == {"A-scn", "B"}
    assert {r["name"] for r in bt.list_objects(c, "Port")} == {"A", "B"}
    # a filter on a column works against the effective rows
    assert [r["name"] for r in bt.list_objects(c, "Port", scenario_id=sid, where="name = %s", params=["B"])] == ["B"]


def test_scenario_only_object_is_invisible_in_main(c):
    sid = fork(c)
    n = uuid.uuid4()
    port(c, n, "New", scenario_id=sid)
    assert bt.get_object(c, "Port", n, scenario_id=sid)["name"] == "New"
    assert bt.get_object(c, "Port", n) is None
    assert bt.list_objects(c, "Port") == []
    ov = bt.list_overrides(c, sid)
    assert len(ov) == 1 and ov[0]["base"]["present"] is False and ov[0]["base"]["props"] is None


def test_clone_on_first_touch_keeps_surrounding_ranges(c):
    a = uuid.uuid4()
    t1, t2 = T0 + timedelta(days=2), T0 + timedelta(days=4)
    port(c, a, "v1", vf=T0)
    port(c, a, "v2", vf=t1)                        # main: v1 [T0,t1)  v2 [t1,inf)
    sid = fork(c)
    port(c, a, "mid", vf=t2, valid_to=t2 + timedelta(days=1), scenario_id=sid)
    at = lambda t: bt.get_object(c, "Port", a, as_of_valid=t, scenario_id=sid)["name"]  # noqa: E731
    assert at(T0 + timedelta(hours=1)) == "v1"
    assert at(t1 + timedelta(hours=1)) == "v2"
    assert at(t2 + timedelta(hours=1)) == "mid"
    assert at(t2 + timedelta(days=1, hours=1)) == "v2"
    # main untouched
    assert bt.get_object(c, "Port", a, as_of_valid=t2 + timedelta(hours=1))["name"] == "v2"
    ov = bt.list_overrides(c, sid)[0]
    assert ov["base"]["present"] is True and ov["base"]["props"]["name"] == "v2" and ov["base"]["main_lower"]


def test_noop_write_in_scenario_leaves_nothing_behind(c):
    a = uuid.uuid4()
    port(c, a, "A")
    sid = fork(c)
    assert port(c, a, "A", scenario_id=sid) is False
    assert bt.list_overrides(c, sid) == []
    assert c.execute("SELECT count(*) FROM obj_port WHERE scenario_id = %s", [sid]).fetchone()[0] == 0
    assert c.execute("SELECT override_rows, data_version FROM scenario_state WHERE scenario_id=%s", [sid]).fetchone() == (0, 0)


def test_tombstone_does_not_resurrect_main(c):
    a = uuid.uuid4()
    port(c, a, "A")
    sid = fork(c)
    assert bt.delete_object(c, "Port", a, NOW - timedelta(days=1), W, scenario_id=sid)
    assert bt.get_object(c, "Port", a, scenario_id=sid) is None
    assert bt.list_objects(c, "Port", scenario_id=sid) == []
    assert bt.get_object(c, "Port", a)["name"] == "A"
    assert bt.list_overrides(c, sid)[0]["deleted"] is True
    # a later main change must not bring it back inside the scenario
    port(c, a, "A2", vf=NOW - timedelta(hours=1))
    assert bt.get_object(c, "Port", a, scenario_id=sid) is None
    # re-creating it in the scenario clears the tombstone flag
    port(c, a, "Back", vf=NOW - timedelta(minutes=1), scenario_id=sid)
    assert bt.get_object(c, "Port", a, scenario_id=sid)["name"] == "Back"
    assert bt.list_overrides(c, sid)[0]["deleted"] is False


def test_snapshot_semantics(c):
    a, b, late = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    port(c, a, "A"); port(c, b, "B")
    sid = fork(c)
    port(c, a, "A-after", vf=NOW - timedelta(hours=2))      # main changes after the fork
    port(c, late, "Late")
    bt.delete_object(c, "Port", b, NOW - timedelta(hours=1), W)
    assert bt.get_object(c, "Port", a)["name"] == "A-after"
    assert bt.get_object(c, "Port", late)["name"] == "Late" and bt.get_object(c, "Port", b) is None
    assert bt.get_object(c, "Port", a, scenario_id=sid)["name"] == "A"
    assert bt.get_object(c, "Port", late, scenario_id=sid) is None
    assert bt.get_object(c, "Port", b, scenario_id=sid)["name"] == "B"
    assert {r["name"] for r in bt.list_objects(c, "Port", scenario_id=sid)} == {"A", "B"}
    # first touch AFTER the main change still clones the fork-time version
    port(c, a, "A-scn", scenario_id=sid)
    assert bt.list_overrides(c, sid)[0]["base"]["props"]["name"] == "A"


def test_main_rows_are_byte_identical_after_scenario_work_and_purge(c):
    ids = [uuid.uuid4() for _ in range(3)]
    for i, x in enumerate(ids):
        port(c, x, f"P{i}")
    ship, wh = uuid.uuid4(), uuid.uuid4()
    bt.write_object(c, "Warehouse", wh, {"name": "W", "region": "EMEA"}, T0, W)
    bt.write_object(c, "Shipment", ship, {"ref": "S", "status": "in_transit"}, T0, W)
    bt.write_link(c, "ships_to", ship, wh, T0, W)
    before = checksum(c)
    outbox_before = c.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]
    sid = fork(c)
    port(c, ids[0], "changed", scenario_id=sid)
    bt.delete_object(c, "Port", ids[1], NOW - timedelta(days=1), W, scenario_id=sid)
    bt.end_link(c, "ships_to", ship, wh, NOW - timedelta(days=1), W, scenario_id=sid)
    bt.write_object(c, "Shipment", ship, {"ref": "S", "status": "delayed"}, NOW - timedelta(days=1), W, scenario_id=sid)
    assert checksum(c) == before
    # no graph outbox rows from scenario writes
    assert c.execute("SELECT count(*) FROM graph_outbox").fetchone()[0] == outbox_before
    n = c.execute("SELECT count(*) FROM obj_port WHERE scenario_id = %s", [sid]).fetchone()[0]
    assert n > 0
    purged = scenario_admin.purge(c, sid)
    assert purged["obj_port"] == n and "scenario_override" in purged
    assert checksum(c) == before
    assert bt.list_overrides(c, sid) == []
    assert c.execute("SELECT override_rows FROM scenario_state WHERE scenario_id=%s", [sid]).fetchone()[0] == 0


def test_purge_refuses_main_and_scoped_role(c):
    with pytest.raises(ValueError):
        scenario_admin.purge(c, 0)
    sid = fork(c)
    c.commit()
    with scoped(c, Principal("admin", "u-admin", "")):
        with pytest.raises(PermissionError):
            scenario_admin.purge(c, sid)


def test_scenario_state_counters_and_cap(c, monkeypatch):
    sid = fork(c)
    ids = [uuid.uuid4() for _ in range(3)]
    port(c, ids[0], "a", scenario_id=sid)
    port(c, ids[0], "a2", scenario_id=sid)                  # same key: one override key, two row versions
    st = c.execute("SELECT override_rows, data_version FROM scenario_state WHERE scenario_id=%s", [sid]).fetchone()
    assert st == (2, 2)                                      # review M4a: the cap counts row versions, not keys
    monkeypatch.setenv("AMARH_SCN_MAX_ROWS", "3")
    port(c, ids[1], "b", scenario_id=sid)
    with pytest.raises(bt.ScenarioLimit):
        port(c, ids[2], "c", scenario_id=sid)
    assert len(bt.list_overrides(c, sid)) == 2
    with pytest.raises(bt.ScenarioLimit):                    # re-writing an overridden key adds versions: capped too
        port(c, ids[0], "a3", scenario_id=sid)


def test_link_endpoints_are_checked_against_the_effective_view(c):
    ship, wh = uuid.uuid4(), uuid.uuid4()
    bt.write_object(c, "Shipment", ship, {"ref": "S", "status": "planned"}, T0, W)
    bt.write_object(c, "Warehouse", wh, {"name": "W", "region": "EMEA"}, T0, W)
    sid = fork(c)
    new_wh = uuid.uuid4()
    bt.write_object(c, "Warehouse", new_wh, {"name": "N", "region": "EMEA"}, NOW - timedelta(days=1), W, scenario_id=sid)
    # link to a scenario-only warehouse: fine in the scenario, refused on main
    assert bt.write_link(c, "ships_to", ship, new_wh, NOW - timedelta(hours=1), W, scenario_id=sid,
                         src_type="Shipment", dst_type="Warehouse")
    with pytest.raises(ValueError):
        bt.write_link(c, "ships_to", ship, new_wh, NOW - timedelta(hours=1), W, src_type="Shipment", dst_type="Warehouse")
    assert bt.neighbours(c, "ships_to", ship, scenario_id=sid) == [new_wh]
    assert bt.neighbours(c, "ships_to", ship) == []
    assert [r["dst"] for r in bt.list_links(c, "ships_to", scenario_id=sid)] == [new_wh]
    # tombstoned endpoint is refused inside the scenario
    bt.delete_object(c, "Warehouse", wh, NOW - timedelta(hours=2), W, scenario_id=sid)
    with pytest.raises(ValueError):
        bt.write_link(c, "ships_to", ship, wh, NOW - timedelta(hours=1), W, scenario_id=sid,
                      src_type="Shipment", dst_type="Warehouse")
    assert bt.list_links(c, "ships_to") == []


def test_link_tombstone_and_snapshot(c):
    ship, wh = uuid.uuid4(), uuid.uuid4()
    bt.write_object(c, "Shipment", ship, {"ref": "S", "status": "planned"}, T0, W)
    bt.write_object(c, "Warehouse", wh, {"name": "W", "region": "EMEA"}, T0, W)
    bt.write_link(c, "ships_to", ship, wh, T0, W)
    sid = fork(c)
    assert bt.end_link(c, "ships_to", ship, wh, NOW - timedelta(days=1), W, scenario_id=sid)
    assert bt.neighbours(c, "ships_to", ship, scenario_id=sid) == []
    assert bt.neighbours(c, "ships_to", ship) == [wh]
    assert bt.neighbours(c, "ships_to", wh, "in", scenario_id=sid) == []
    ov = bt.list_overrides(c, sid)[0]
    assert ov["kind"] == "link" and ov["deleted"] is True and ov["key_a"] == ship and ov["key_b"] == wh


def test_history_in_scenario(c):
    a, b = uuid.uuid4(), uuid.uuid4()
    port(c, a, "A1"); port(c, a, "A2", vf=T0 + timedelta(days=1)); port(c, b, "B1")
    sid = fork(c)
    port(c, b, "B2", vf=NOW - timedelta(hours=3))                      # main change after the fork
    hb = bt.history(c, "Port", b, scenario_id=sid)
    assert [r["name"] for r in hb] == ["B1"] and hb[0]["recorded_to"] is None   # main up to the fork, still "open" there
    assert len(bt.history(c, "Port", b)) == 3
    port(c, a, "A3", vf=NOW - timedelta(hours=1), scenario_id=sid)
    ha = bt.history(c, "Port", a, scenario_id=sid)
    assert {r["name"] for r in ha} == {"A2", "A1", "A3"} and all(r["scenario_id"] == sid for r in ha)
    assert all(r["scenario_id"] == 0 for r in bt.history(c, "Port", a))
    assert len(bt.history(c, "Port", a)) == 3
    ship, wh = uuid.uuid4(), uuid.uuid4()
    bt.write_link(c, "ships_to", ship, wh, T0, W)
    sid2 = fork(c, "s2")
    assert len(bt.link_history(c, "ships_to", ship, wh, scenario_id=sid2)) == 1
    bt.end_link(c, "ships_to", ship, wh, NOW - timedelta(hours=1), W, scenario_id=sid2)
    assert bt.link_history(c, "ships_to", ship, wh, scenario_id=sid2)[-1]["valid_to"] is not None


def test_scenario_clock_drives_default_valid_time(c):
    a = uuid.uuid4()
    port(c, a, "now", vf=T0)
    future = NOW + timedelta(days=3)
    port(c, a, "future", vf=future)                                   # main knows a future-valid version
    sid = fork(c, clock=future + timedelta(hours=1))
    assert bt.get_object(c, "Port", a)["name"] == "now"
    assert bt.get_object(c, "Port", a, scenario_id=sid)["name"] == "future"
    sid2 = fork(c, "plain")
    assert bt.get_object(c, "Port", a, scenario_id=sid2)["name"] == "now"


def test_unknown_scenario_and_bulk_are_rejected(c):
    with pytest.raises(ValueError):
        bt.get_object(c, "Port", uuid.uuid4(), scenario_id=987654)
    with pytest.raises(ValueError):
        bt.bulk_load_objects(c, "Port", [(uuid.uuid4(), {"name": "x"})], T0, W, scenario_id=1)


def test_scoped_scenario_param_validation_and_gucs(c):
    sid = fork(c)
    ctx = scenario_admin.ctx_of(c, sid)
    for bad in (sid, str(sid), {"id": sid}):
        with pytest.raises(TypeError):
            with scoped(c, Principal("admin", "u-admin", ""), bad):
                pass
    with pytest.raises(ValueError):
        ScenarioCtx(0, NOW)
    with scoped(c, Principal("admin", "u-admin", ""), ctx):
        row = c.execute("SELECT amarh_scn(), amarh_scn_base() = %s, amarh_now() <= now() + interval '1 second'",
                        [ctx.base_recorded]).fetchone()
        assert row == (sid, True, True)
        # a scoped session cannot touch a scenario other than its own
        with pytest.raises(ValueError):
            bt.get_object(c, "Port", uuid.uuid4(), scenario_id=sid + 1)
    assert c.execute("SELECT amarh_scn(), current_setting('amarh.scenario_base', true), "
                     "current_setting('amarh.now', true)").fetchone() == (0, "", "")
    with scoped(c, Principal("admin", "u-admin", "")):          # no scenario: GUCs explicitly cleared
        assert c.execute("SELECT amarh_scn()").fetchone()[0] == 0
    with scoped(c, OWNER, ctx):
        assert c.execute("SELECT amarh_scn()").fetchone()[0] == sid
    assert c.execute("SELECT amarh_scn()").fetchone()[0] == 0


def test_eff_views_follow_the_rule(c):
    a, b = uuid.uuid4(), uuid.uuid4()
    port(c, a, "A"); port(c, b, "B")
    sid = fork(c)
    ctx = scenario_admin.ctx_of(c, sid)
    port(c, a, "A-scn", scenario_id=sid)
    bt.delete_object(c, "Port", b, NOW - timedelta(days=1), W, scenario_id=sid)
    new = uuid.uuid4()
    port(c, new, "N", scenario_id=sid)
    port(c, uuid.uuid4(), "late")                                    # created in main after the fork
    assert {r[0] for r in c.execute("SELECT name FROM eff_obj_port").fetchall()} == {"A", "B", "late"}  # no scenario set = cur_*
    with scoped(c, OWNER, ctx):
        assert {r[0] for r in c.execute("SELECT name FROM eff_obj_port").fetchall()} == {"A-scn", "N"}
    assert {r[0] for r in c.execute("SELECT name FROM cur_obj_port").fetchall()} == {"A", "B", "late"}
    with scoped(c, OWNER, ctx):                                      # cur_* never returns scenario rows
        assert {r[0] for r in c.execute("SELECT name FROM cur_obj_port").fetchall()} == {"A", "B", "late"}


def test_governance_upgrade_is_idempotent_and_keeps_old_inserts_working(c):
    """Simulate a pre-Phase-5 inferred_support (old PK, no scenario_id) and re-apply the schema twice."""
    c.execute("TRUNCATE inferred_support")
    c.execute("ALTER TABLE inferred_support DROP CONSTRAINT inferred_support_pkey")
    c.execute("ALTER TABLE inferred_support DROP COLUMN scenario_id CASCADE")
    c.execute("ALTER TABLE inferred_support ADD PRIMARY KEY (link, src, dst, derivation_no)")
    a, b = uuid.uuid4(), uuid.uuid4()
    c.execute("INSERT INTO inferred_support (link, src, dst, derivation_no, support) VALUES ('supplies',%s,%s,0,%s)",
              [a, b, [a, b]])
    apply_schema(c)
    apply_schema(c)
    pk = c.execute("SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_constraint x "
                   "JOIN LATERAL unnest(x.conkey) WITH ORDINALITY k(attnum, ord) ON true "
                   "JOIN pg_attribute a ON a.attrelid = x.conrelid AND a.attnum = k.attnum "
                   "WHERE x.conrelid = to_regclass('inferred_support') AND x.contype = 'p'").fetchone()[0]
    assert pk == ["scenario_id", "link", "src", "dst", "derivation_no"]
    assert c.execute("SELECT scenario_id FROM inferred_support").fetchall() == [(0,)]
    # materialize.py's existing INSERT (no scenario_id column) still lands in scenario 0
    c.execute("INSERT INTO inferred_support (link, src, dst, derivation_no, support, data_version) "
              "VALUES ('supplies',%s,%s,1,%s,7)", [a, b, [a, b]])
    assert c.execute("SELECT count(*) FROM inferred_support WHERE scenario_id = 0").fetchone()[0] == 2
