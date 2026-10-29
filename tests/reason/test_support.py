"""inferred_support: derivations of inferred `supplies` / `supplied_by` edges (D6)."""
import uuid

import pytest

from engine.db import connect
from engine.reason.materialize import MAX_DERIVATIONS, enumerate_paths, link_semantics, materialize, write_support
from engine.sync import G_ASSERTED, SyncConfig, obj_iri, open_store
from pyoxigraph import Quad

NS = "https://example.org/sc#"


def store_with(edges):
    cfg = SyncConfig()
    st = open_store(None, cfg)
    ids = {}

    def i(n):
        ids.setdefault(n, uuid.uuid4())
        return ids[n]

    from engine.sync import class_iri, link_iri
    from engine.sync.iri import RDF_TYPE
    for a, b in edges:
        for n in (a, b):
            st.add(Quad(obj_iri(NS, i(n)), RDF_TYPE, class_iri(NS, "Supplier"), G_ASSERTED))
        st.add(Quad(obj_iri(NS, i(a)), link_iri(NS, "supplies"), obj_iri(NS, i(b)), G_ASSERTED))
    materialize(st, NS)
    return st, ids


def rows(c):
    return c.execute("SELECT link, src, dst, derivation_no, support FROM inferred_support "
                     "ORDER BY link, src, dst, derivation_no").fetchall()


def test_semantics():
    assert link_semantics() == {"supplies": ("supplies", False), "supplied_by": ("supplies", True)}


def test_enumerate_paths_shortest_first_and_cycles():
    adj = {"a": ["b", "c"], "b": ["d"], "c": ["d", "b"], "d": ["a"]}
    reach = {"a", "b", "c", "d"}
    ps = enumerate_paths(adj, reach, "a", "d")
    assert ps[0] == ("a", "b", "d") or ps[0] == ("a", "c", "d")
    assert set(ps) == {("a", "b", "d"), ("a", "c", "d"), ("a", "c", "b", "d")}
    assert all(len(set(p)) == len(p) for p in ps)
    assert enumerate_paths(adj, reach, "a", "d", cap=1) == ps[:1]


def test_write_support_diamond(gschema):
    st, ids = store_with([("a", "b"), ("b", "d"), ("a", "c"), ("c", "d"), ("d", "e")])
    c = connect(schema=gschema)
    try:
        stats = write_support(c, st, NS)
        assert stats["no_derivation"] == 0
        r = rows(c)
        a, d, e = ids["a"], ids["d"], ids["e"]
        ad = [x for x in r if x[0] == "supplies" and x[1] == a and x[2] == d]
        assert len(ad) == 2 and {tuple(x[4]) for x in ad} == {(a, ids["b"], d), (a, ids["c"], d)}
        assert [x[3] for x in ad] == [0, 1]
        ae = [x for x in r if x[0] == "supplies" and x[1] == a and x[2] == e]
        assert len(ae) == 2 and all(x[4][0] == a and x[4][-1] == e and len(x[4]) == 4 for x in ae)
        # inverse edge: ordered along the BASE link (dst -> src of the inferred edge)
        inv = [x for x in r if x[0] == "supplied_by" and x[1] == e and x[2] == a]
        assert len(inv) == 2 and all(x[4][0] == a and x[4][-1] == e for x in inv)
        # inverse of an ASSERTED edge: one 2-node derivation
        direct = [x for x in r if x[0] == "supplied_by" and x[1] == ids["b"] and x[2] == a]
        assert [tuple(x[4]) for x in direct] == [(a, ids["b"])]
        # asserted edges themselves are not recorded as inferred
        assert not [x for x in r if x[0] == "supplies" and x[1] == a and x[2] == ids["b"]]
        # re-running replaces, never accumulates
        n = len(r)
        write_support(c, st, NS)
        assert len(rows(c)) == n
    finally:
        c.rollback(); c.close()


def test_derivation_cap(gschema):
    # ladder: 7 layers of 2 nodes => 2^7 = 128 paths a..z
    edges, prev = [], ["a"]
    for L in range(7):
        cur = [f"l{L}x", f"l{L}y"]
        edges += [(p, n) for p in prev for n in cur]
        prev = cur
    edges += [(p, "z") for p in prev]
    st, ids = store_with(edges)
    c = connect(schema=gschema)
    try:
        write_support(c, st, NS)
        az = [x for x in rows(c) if x[0] == "supplies" and x[1] == ids["a"] and x[2] == ids["z"]]
        assert len(az) == MAX_DERIVATIONS == 50
        assert len({tuple(x[4]) for x in az}) == 50
    finally:
        c.rollback(); c.close()


def test_pipeline_populates_support(gschema):
    from engine.db import bitemporal as bt
    from engine.sync.pipeline import SyncPipeline
    from datetime import datetime, UTC, timedelta
    c = connect(schema=gschema)
    try:
        for t in ("obj_supplier", "link_supplies", "graph_outbox", "inferred_support"):
            c.execute(f"TRUNCATE {t}")
        c.commit()
        t0 = datetime.now(UTC) - timedelta(days=1)
        ids = [uuid.uuid4() for _ in range(3)]
        for i, n in zip(ids, "xyz"):
            bt.write_object(c, "Supplier", i, {"name": n}, t0, "t")
        bt.write_link(c, "supplies", ids[0], ids[1], t0, "t")
        bt.write_link(c, "supplies", ids[1], ids[2], t0, "t")
        c.commit()
        p = SyncPipeline(schema=gschema, min_reason_interval_s=0)
        p.sync(); p.maybe_reason(force=True)
        r = rows(c)
        assert any(x[0] == "supplies" and x[1] == ids[0] and x[2] == ids[2] and list(x[4]) == ids for x in r)
        assert p.support_stats["no_derivation"] == 0
    finally:
        c.rollback(); c.close()
