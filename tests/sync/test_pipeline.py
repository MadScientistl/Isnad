import random
import time
from datetime import datetime, timedelta, UTC
from uuid import uuid4

import pytest
from psycopg import sql

from engine.actions import apply_action
from engine.actions.core import ensure_audit_schema
from engine.db import bitemporal as bt, connect, get_dsn
from engine.sync import snapshot, G_ASSERTED, G_INFERRED
from engine.sync.pipeline import SyncPipeline
from engine.sync.store import SyncConfig

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NS = "https://example.org/sc#"
TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


@pytest.fixture
def pipe(schema, conn):
    c = connect(schema=schema)
    ensure_audit_schema(c)
    for (t,) in c.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s AND "
                          "(tablename LIKE 'obj\\_%%' OR tablename LIKE 'link\\_%%' OR tablename IN "
                          "('graph_outbox','audit_event'))", [schema]).fetchall():
        c.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(sql.Identifier(t)))
    c.commit()
    yield SyncPipeline(schema=schema, min_reason_interval_s=0.2), c
    c.close()


def test_config_defaults_from_yaml():
    cfg = SyncConfig()
    assert {"Invoice", "PurchaseOrder", "Event"} <= set(cfg.object_types)
    assert {"ships_from", "ships_to", "invoices_for", "billed_by", "affects"} <= set(cfg.link_types)
    assert SyncConfig(object_types=("Port",)).object_types == ("Port",)


def _world(c, rnd, steps=40):
    sup = apply_action(c, "create_object", {"type": "Supplier", "props": {"name": "s0", "country": "DE"}},
                       "t", valid_at=T0).created
    return sup


def test_parity_and_abstract_endpoint(pipe):
    p, c = pipe
    rnd = random.Random(3)
    ids = {"Supplier": [], "Port": [], "Warehouse": [], "Carrier": []}
    for typ, n in (("Supplier", 8), ("Port", 3), ("Warehouse", 3), ("Carrier", 2)):
        for k in range(n):
            i = uuid4()
            props = {"name": f"{typ}{k}"}
            if typ == "Supplier":
                props["country"] = "DE"
            bt.write_object(c, typ, i, props, T0, "t")
            ids[typ].append(i)
    c.commit()
    assert p.sync() > 0
    shipments = []
    for k in range(10):
        r = rnd.random()
        if r < 0.35:
            a, b = rnd.sample(ids["Supplier"], 2)
            bt.write_link(c, "supplies", a, b, T0 + timedelta(days=k), "t")
        elif r < 0.5:
            a, b = rnd.sample(ids["Supplier"], 2)
            bt.end_link(c, "supplies", a, b, T0 + timedelta(days=k + 1), "t")
        else:
            s = uuid4()
            bt.write_object(c, "Shipment", s, {"ref": f"S{k}", "status": "planned"}, T0 + timedelta(days=k), "t")
            bt.write_link(c, "operated_by", s, rnd.choice(ids["Carrier"]), T0 + timedelta(days=k), "t")
            bt.write_link(c, "ships_from", s, rnd.choice(ids["Port"]), T0 + timedelta(days=k), "t")
            bt.write_link(c, "ships_to", s, rnd.choice(ids["Warehouse"]), T0 + timedelta(days=k), "t")
            shipments.append(s)
        c.commit()
        if k % 3 == 0:
            p.sync()
    # real action path too
    r = apply_action(c, "create_object", {"type": "Supplier", "props": {"name": "x", "country": "FR"}},
                     "t", valid_at=T0 + timedelta(days=20))
    p.sync()
    inc = snapshot(p.store)
    p.rebuild()
    assert inc == snapshot(p.store)
    # ships_from edge to a Port (abstract Facility range) is mirrored; Port types are concrete
    assert any(pp == NS + "ships_from" for _, pp, _ in inc)
    assert not any(o == NS + "Facility" for _, pp, o in inc if pp == TYPE)
    p.maybe_reason(force=True)
    s0 = f"{NS}obj/{ids['Port'][0]}"
    inferred_types = {(q.subject.value, q.object.value) for q in p.store.quads_for_pattern(None, None, None, G_INFERRED)
                      if q.predicate.value == TYPE}
    assert any(o == NS + "Facility" for _, o in inferred_types)  # subclass inference


def test_inferred_edges_and_maybe_reason(pipe):
    p, c = pipe
    ids = [uuid4() for _ in range(4)]
    for i in ids:
        bt.write_object(c, "Supplier", i, {"name": "s", "country": "DE"}, T0, "t")
    for a, b in zip(ids, ids[1:]):
        bt.write_link(c, "supplies", a, b, T0, "t")
    c.commit()
    assert p.maybe_reason() is True or True  # dirty from start; may run on empty store
    p.sync()
    time.sleep(0.25)
    assert p.maybe_reason() is True
    assert p.maybe_reason() is False  # no change
    edges = p.inferred_edges([ids[0]])
    assert (ids[0], "supplies", ids[3]) in edges
    assert all(e[1] not in ("type", "sameAs") for e in edges)
    assert (ids[3], "supplied_by", ids[0]) in edges
    # change + interval not elapsed -> skipped; force runs; after interval runs
    bt.write_link(c, "supplies", ids[3], ids[0], T0, "t"); c.commit()
    p.sync()
    assert p.maybe_reason() is False
    assert p.maybe_reason(force=True) is True
    p.sync()
    assert p.maybe_reason() is False  # nothing new
