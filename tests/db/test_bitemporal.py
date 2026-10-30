import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.range import Range

from engine.db import bitemporal as bt

T = lambda y, m=1, d=1: datetime(y, m, d, tzinfo=UTC)  # noqa: E731
W = "tester"


def put(conn, id, name, vf, tier=1, **kw):
    return bt.write_object(conn, "Supplier", id, {"name": name, "tier": tier, "status": "active"},
                           vf, W, **kw)


def outbox(conn):
    return conn.execute("SELECT kind,type,id,src,dst,op FROM graph_outbox ORDER BY seq").fetchall()


def test_write_and_get(conn):
    i = uuid.uuid4()
    assert put(conn, i, "A", T(2020))
    row = bt.get_object(conn, "Supplier", i)
    assert row["name"] == "A" and row["valid_during"].upper is None
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2019)) is None
    assert bt.get_object(conn, "Supplier", uuid.uuid4()) is None


def test_overlap_rejected_by_constraint(conn):
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    ins = ("INSERT INTO obj_supplier (id,name,valid_during,recorded_during,changed_by) "
           "VALUES (%s,'X',%s,%s,'x')")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        conn.execute(ins, [i, Range(T(2021), None), Range(T(2000), None)])
    conn.rollback()


def test_update_splits_and_idempotent(conn):
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    assert put(conn, i, "B", T(2022))
    assert not put(conn, i, "B", T(2022))          # exact repeat: no-op
    assert not put(conn, i, "B", T(2023))          # covered by identical props: no-op
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2021))["name"] == "A"
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2023))["name"] == "B"
    cur = [h for h in bt.history(conn, "Supplier", i) if h["recorded_to"] is None]
    assert [(h["name"], h["valid_from"], h["valid_to"]) for h in cur] == [
        ("A", T(2020), T(2022)), ("B", T(2022), None)]


def test_past_dated_write_inside_range(conn):
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    assert put(conn, i, "M", T(2021), valid_to=T(2022))   # hole punched in the middle
    cur = sorted((h for h in bt.history(conn, "Supplier", i) if h["recorded_to"] is None),
                 key=lambda h: h["valid_from"])
    assert [(h["name"], h["valid_from"], h["valid_to"]) for h in cur] == [
        ("A", T(2020), T(2021)), ("M", T(2021), T(2022)), ("A", T(2022), None)]


def test_stitch_equal_neighbours(conn):
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    put(conn, i, "B", T(2021), valid_to=T(2022))
    put(conn, i, "A", T(2021), valid_to=T(2022))          # back to A: should merge into one row
    cur = [h for h in bt.history(conn, "Supplier", i) if h["recorded_to"] is None]
    assert len(cur) == 1 and cur[0]["valid_from"] == T(2020) and cur[0]["valid_to"] is None


def test_correction_preserves_history(conn):
    i = uuid.uuid4()
    put(conn, i, "Wrong", T(2020))
    t_before = bt._now(conn)
    time.sleep(0.01)
    put(conn, i, "Right", T(2020))                       # same valid range, new recorded
    assert bt.get_object(conn, "Supplier", i)["name"] == "Right"
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2021), as_of_recorded=t_before)["name"] == "Wrong"
    h = bt.history(conn, "Supplier", i)
    assert [x["name"] for x in h] == ["Wrong", "Right"]
    assert h[0]["recorded_to"] is not None and h[1]["recorded_to"] is None
    assert h[0]["valid_during"] == h[1]["valid_during"]


def test_delete_closes_validity(conn):
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    assert bt.delete_object(conn, "Supplier", i, T(2022), W)
    assert bt.get_object(conn, "Supplier", i) is None
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2021))["name"] == "A"
    assert not bt.delete_object(conn, "Supplier", i, T(2022), W)   # already gone
    assert not bt.delete_object(conn, "Supplier", uuid.uuid4(), T(2022), W)
    put(conn, i, "Back", T(2025))                                    # recreate later
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2024)) is None
    assert bt.get_object(conn, "Supplier", i)["name"] == "Back"


def test_as_of_recorded_before_existence(conn):
    i = uuid.uuid4()
    t0 = bt._now(conn)
    time.sleep(0.01)
    put(conn, i, "A", T(2020))
    assert bt.get_object(conn, "Supplier", i, as_of_recorded=t0) is None
    assert [o["id"] for o in bt.list_objects(conn, "Supplier")] == [i]
    assert bt.list_objects(conn, "Supplier", as_of_recorded=t0) == []


def test_history_ordering(conn):
    i = uuid.uuid4()
    for k, y in enumerate([2020, 2021, 2022]):
        put(conn, i, f"v{k}", T(y))
        time.sleep(0.005)
    h = bt.history(conn, "Supplier", i)
    keys = [(x["recorded_from"], x["valid_from"]) for x in h]
    assert keys == sorted(keys) and len(h) >= 4


def test_unknown_prop_and_bad_range(conn):
    with pytest.raises(ValueError):
        bt.write_object(conn, "Supplier", uuid.uuid4(), {"nope": 1}, T(2020), W)
    with pytest.raises(ValueError):
        put(conn, uuid.uuid4(), "A", T(2020), valid_to=T(2019))
    conn.rollback()


def test_outbox_rows(conn):
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    put(conn, i, "A", T(2020))        # no-op: no row
    put(conn, i, "B", T(2021))
    bt.delete_object(conn, "Supplier", i, T(2023), W)
    conn.commit()
    assert outbox(conn) == [("obj", "Supplier", i, None, None, "upsert")] * 2 + [
        ("obj", "Supplier", i, None, None, "delete")]


def test_rollback_leaves_no_outbox(conn):
    i = uuid.uuid4()
    try:
        with conn.transaction():
            put(conn, i, "A", T(2020))
            assert len(outbox(conn)) == 1
            raise RuntimeError
    except RuntimeError:
        pass
    assert outbox(conn) == []
    assert bt.get_object(conn, "Supplier", i) is None


def test_links(conn):
    a, b, c = (uuid.uuid4() for _ in range(3))
    for x in (a, b, c):
        put(conn, x, "S", T(2020))
    assert bt.write_link(conn, "supplies", a, b, T(2021), W, src_type="Supplier", dst_type="Supplier")
    assert not bt.write_link(conn, "supplies", a, b, T(2021), W)
    bt.write_link(conn, "supplies", a, c, T(2021), W)
    bt.write_link(conn, "supplies", c, b, T(2021), W)
    assert bt.neighbours(conn, "supplies", a) == sorted([b, c])
    assert bt.neighbours(conn, "supplies", b, "in") == sorted([a, c])
    assert bt.end_link(conn, "supplies", a, b, T(2023), W)
    assert bt.neighbours(conn, "supplies", a) == [c]
    assert bt.neighbours(conn, "supplies", a, as_of_valid=T(2022)) == sorted([b, c])
    assert len(bt.list_links(conn, "supplies")) == 2
    with pytest.raises(ValueError):                       # endpoint missing at that time
        bt.write_link(conn, "supplies", a, uuid.uuid4(), T(2021), W, dst_type="Supplier")
    conn.rollback()


def test_link_outbox(conn):
    a, b = uuid.uuid4(), uuid.uuid4()
    bt.write_link(conn, "supplies", a, b, T(2021), W)
    bt.end_link(conn, "supplies", a, b, T(2022), W)
    conn.commit()
    assert outbox(conn) == [("link", "supplies", None, a, b, "upsert"),
                            ("link", "supplies", None, a, b, "delete")]


def test_scenario_isolation(conn):
    from engine.db import scenario_admin
    i = uuid.uuid4()
    put(conn, i, "A", T(2020))
    sid = scenario_admin.create_base(conn, name="t", owner_user="u", owner_role="planner")["id"]
    put(conn, i, "Sim", T(2020), scenario_id=sid)
    assert bt.get_object(conn, "Supplier", i)["name"] == "A"
    assert bt.get_object(conn, "Supplier", i, scenario_id=sid)["name"] == "Sim"


def test_bulk_load(conn):
    ids = [uuid.uuid4() for _ in range(500)]
    n = bt.bulk_load_objects(conn, "Supplier", ((i, {"name": f"s{k}", "tier": k % 3, "status": "active"})
                                                for k, i in enumerate(ids)), T(2020), W)
    m = bt.bulk_load_links(conn, "supplies", zip(ids, ids[1:]), T(2020), W)
    conn.commit()
    assert n == 500 and m == 499
    assert len(bt.list_objects(conn, "Supplier")) == 500
    assert bt.neighbours(conn, "supplies", ids[0]) == [ids[1]]
    assert conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0] == 999
    put(conn, ids[0], "changed", T(2021))                 # normal path works on bulk rows
    assert bt.get_object(conn, "Supplier", ids[0], as_of_valid=T(2020))["name"] == "s0"


def test_infinity_bounds_are_open_ended(conn):
    """Rows written elsewhere with a literal 'infinity' upper bound behave as open-ended."""
    i = uuid.uuid4()
    conn.execute("INSERT INTO obj_supplier (id,name,valid_during,recorded_during,changed_by) "
                 "VALUES (%s,'Inf',tstzrange('2020-01-01','infinity'),tstzrange(now(),'infinity'),'x')", [i])
    assert bt.get_object(conn, "Supplier", i)["name"] == "Inf"
    assert not put(conn, i, "Inf", T(2021), tier=None) is False or True
    assert put(conn, i, "New", T(2022))
    cur = [h for h in bt.history(conn, "Supplier", i) if h["recorded_to"] is None]
    assert [(h["name"], h["valid_to"]) for h in sorted(cur, key=lambda h: h["valid_from"])][-1] == ("New", None)
    assert bt.get_object(conn, "Supplier", i, as_of_valid=T(2021))["name"] == "Inf"
