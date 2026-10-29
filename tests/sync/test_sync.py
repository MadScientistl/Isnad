import random
from datetime import datetime, timedelta, UTC
from uuid import uuid4

from engine.db import bitemporal as bt
from engine.sync import open_store, consume_outbox, rebuild, snapshot, G_ASSERTED

T0 = datetime(2020, 1, 1, tzinfo=UTC)
NS = "https://example.org/sc#"


def _setup(conn, n=30, seed=1):
    rnd = random.Random(seed)
    ids = [uuid4() for _ in range(n)]
    for i in ids:
        bt.write_object(conn, "Supplier", i, {"name": f"s{i}", "country": "DE"}, T0, "t")
    for _ in range(n * 2):
        a, b = rnd.sample(ids, 2)
        bt.write_link(conn, "supplies", a, b, T0, "t")
    conn.commit()
    return ids, rnd


def test_initial_and_iris(conn):
    ids, _ = _setup(conn, 5)
    st = open_store()
    stats = consume_outbox(conn, st)
    assert stats["rows"] > 0
    snap = snapshot(st)
    assert (f"{NS}obj/{ids[0]}", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"{NS}Supplier") in snap
    # idempotent
    assert consume_outbox(conn, st)["rows"] == 0


def test_incremental_equals_rebuild(conn):
    ids, rnd = _setup(conn, 40)
    inc = open_store()
    consume_outbox(conn, inc, batch=17)  # small batches exercise chunking
    for step in range(60):
        r = rnd.random()
        a, b = rnd.sample(ids, 2)
        if r < 0.3:
            bt.write_link(conn, "supplies", a, b, T0 + timedelta(days=1), "t")
        elif r < 0.6:
            bt.end_link(conn, "supplies", a, b, T0 + timedelta(days=2), "t")
        elif r < 0.8:
            bt.delete_object(conn, "Supplier", a, T0 + timedelta(days=3), "t")
        else:
            bt.write_object(conn, "Supplier", a, {"name": "again", "country": "FR"}, T0 + timedelta(days=4), "t")
        conn.commit()
        if step % 15 == 14:
            consume_outbox(conn, inc)
    consume_outbox(conn, inc)
    full = open_store()
    rebuild(conn, full)
    assert snapshot(inc) == snapshot(full)
    assert len(snapshot(full)) > 40


def test_delete_removes_type_and_link_end_removes_edge(conn):
    a, b = uuid4(), uuid4()
    for i in (a, b):
        bt.write_object(conn, "Supplier", i, {"name": "x"}, T0, "t")
    bt.write_link(conn, "supplies", a, b, T0, "t")
    conn.commit()
    st = open_store()
    consume_outbox(conn, st)
    assert len(snapshot(st)) == 3
    bt.end_link(conn, "supplies", a, b, T0 + timedelta(days=1), "t")
    bt.delete_object(conn, "Supplier", b, T0 + timedelta(days=1), "t")
    conn.commit()
    consume_outbox(conn, st)
    assert len(snapshot(st)) == 1


def test_bulk_load_sync_and_on_disk(conn, tmp_path):
    ids = [uuid4() for _ in range(200)]
    bt.bulk_load_objects(conn, "Supplier", [(i, {"name": "n"}) for i in ids], T0, "bulk")
    bt.bulk_load_links(conn, "supplies", [(ids[i], ids[i + 1]) for i in range(199)], T0, "bulk")
    conn.commit()
    path = tmp_path / "ox"
    st = open_store(path)
    consume_outbox(conn, st)
    n = len(snapshot(st))
    assert n == 200 + 199
    del st
    st2 = open_store(path)  # persisted, cursor persisted too
    assert len(snapshot(st2)) == n
    assert consume_outbox(conn, st2)["rows"] == 0
    full = open_store()
    rebuild(conn, full)
    assert snapshot(full) == snapshot(st2)
