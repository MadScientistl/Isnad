from datetime import datetime, UTC
from uuid import uuid4

from pyoxigraph import Quad

from engine.db import bitemporal as bt
from engine.reason import materialize, validate_changed, extract_subgraph
from engine.reason.shacl import pg_props_loader
from engine.sync import open_store, consume_outbox, snapshot, G_ASSERTED, G_INFERRED, G_META, obj_iri

T0 = datetime(2020, 1, 1, tzinfo=UTC)
NS = "https://example.org/sc#"
SUP, SBY = NS + "supplies", NS + "supplied_by"


def _chain(conn, n=6, cycle=False):
    ids = [uuid4() for _ in range(n)]
    for i in ids:
        bt.write_object(conn, "Supplier", i, {"name": "s"}, T0, "t")
    for a, b in zip(ids, ids[1:]):
        bt.write_link(conn, "supplies", a, b, T0, "t")
    if cycle:
        bt.write_link(conn, "supplies", ids[-1], ids[0], T0, "t")
    conn.commit()
    st = open_store()
    consume_outbox(conn, st)
    return ids, st


def _inf(st):
    return {(q.subject.value, q.predicate.value, q.object.value)
            for q in st.quads_for_pattern(None, None, None, G_INFERRED)}


def test_transitive_inverse_separate_graph_flagged(conn):
    ids, st = _chain(conn, 6)
    before = snapshot(st)
    r = materialize(st)
    assert snapshot(st) == before  # asserted graph untouched
    inf = _inf(st)
    i = lambda k: obj_iri(NS, ids[k]).value
    assert (i(0), SUP, i(5)) in inf and (i(5), SBY, i(0)) in inf
    assert (i(0), SUP, i(1)) not in inf  # asserted edge is not re-listed as inferred
    assert r.inferred_triples == len(inf) == 10 + 15  # 10 transitive supplies + 15 supplied_by
    flags = {(q.predicate.value, q.object.value) for q in st.quads_for_pattern(G_INFERRED, None, None, G_META)}
    assert ("urn:amarh:isInferredGraph", "true") in flags


def test_cycle_terminates(conn):
    ids, st = _chain(conn, 4, cycle=True)
    materialize(st)
    inf = _inf(st)
    a = obj_iri(NS, ids[0]).value
    assert (a, SUP, a) in inf  # reflexive via cycle


def test_reasonable_matches_owlrl(conn):
    ids, st = _chain(conn, 8, cycle=True)
    materialize(st, backend="reasonable")
    r_set = _inf(st)
    materialize(st, backend="owlrl")
    o_set = {t for t in _inf(st) if not t[1].endswith("owl#sameAs")}
    assert r_set == o_set


def test_shacl_detects_violation(conn):
    car, port, s_ok, s_bad = uuid4(), uuid4(), uuid4(), uuid4()
    bt.write_object(conn, "Carrier", car, {"name": "c"}, T0, "t")
    bt.write_object(conn, "Port", port, {"name": "p", "lat": 1.0, "lon": 2.0, "status": "open"}, T0, "t")
    for s in (s_ok, s_bad):
        bt.write_object(conn, "Shipment", s, {"ref": str(s), "status": "x"}, T0, "t")
        bt.write_link(conn, "ships_via", s, port, T0, "t")
    bt.write_link(conn, "operated_by", s_ok, car, T0, "t")  # s_bad lacks required carrier
    conn.commit()
    st = open_store()
    consume_outbox(conn, st)
    loader = pg_props_loader(conn, ["Shipment", "Port", "Carrier"])
    ok = validate_changed(st, [s_ok, port, car], props_loader=loader)
    assert ok.conforms, ok.violations
    bad = validate_changed(st, [s_ok, s_bad], props_loader=loader)
    assert not bad.conforms
    assert bad.focus_nodes == {obj_iri(NS, s_bad).value}
    sub = extract_subgraph(st, [s_bad])
    assert any(q.predicate.value == NS + "ships_via" for q in sub)


def test_rematerialize_drops_stale_inferences(conn):
    ids, st = _chain(conn, 5)
    materialize(st)
    n1 = len(_inf(st))
    bt.end_link(conn, "supplies", ids[1], ids[2], T0.replace(year=2021), "t")
    conn.commit()
    consume_outbox(conn, st)
    materialize(st)
    inf = _inf(st)
    assert (obj_iri(NS, ids[0]).value, SUP, obj_iri(NS, ids[4]).value) not in inf
    assert len(inf) < n1
