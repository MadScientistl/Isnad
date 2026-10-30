"""Single-stage workers, run as subprocesses by bench.gate so each gets a clean process
(own peak RSS, store lock released between stages).  Prints one JSON line on stdout."""
from __future__ import annotations

import argparse
import json
import random
import resource
import statistics
import sys
import time
from datetime import datetime, timedelta, UTC
from uuid import UUID

from pyoxigraph import Store

from engine.db import connect
from engine.db import bitemporal as bt
from engine.reason import materialize, validate_changed
from engine.reason.shacl import pg_props_loader
from engine.sync import open_store, consume_outbox, rebuild, G_ASSERTED, G_INFERRED, obj_iri

NS = "https://example.org/sc#"


def rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summ(xs):
    return {"n": len(xs), "median_s": statistics.median(xs), "p95_s": pct(xs, 0.95), "max_s": max(xs), "min_s": min(xs)}


def stage_outbox_load(a):
    conn = connect(schema=a.schema)
    st = open_store(a.store)
    t = time.perf_counter()
    s = consume_outbox(conn, st)
    dt = time.perf_counter() - t
    return {"seconds": dt, "outbox_rows": s["rows"], "quads_added": s["added"], "peak_rss_mb": rss_mb()}


def stage_rebuild(a):
    conn = connect(schema=a.schema)
    st = open_store(a.store)
    t = time.perf_counter()
    s = rebuild(conn, st)
    return {"seconds": time.perf_counter() - t, "quads": s["quads"], "peak_rss_mb": rss_mb()}


def stage_materialize(a):
    st = open_store(a.store)
    r = materialize(st, NS, backend="reasonable")
    return {"seconds": r.seconds, "inferred_triples": r.inferred_triples, "base_triples": r.base_triples,
            "dropped_non_instance": r.dropped_non_instance, "phases": r.phases, "peak_rss_mb": rss_mb()}


Q_FIXED = "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE {{ GRAPH <urn:isnad:g:asserted> {{ <{s}> sc:supplies/sc:supplies/sc:supplies/sc:supplies ?o }} }}"
Q_PLUS = "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE {{ GRAPH <urn:isnad:g:asserted> {{ <{s}> sc:supplies+ ?o }} }}"
Q_MIXED = "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE {{ GRAPH <urn:isnad:g:asserted> {{ <{s}> sc:ships_via/^sc:ships_via/sc:operated_by/^sc:operated_by ?o }} }}"
Q_INF = "SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <urn:isnad:g:inferred> {{ <{s}> sc:supplies ?o }} }}"
PFX = "PREFIX sc: <https://example.org/sc#>\n"


def _run(st, q):
    t = time.perf_counter()
    n = int(next(iter(st.query(PFX + q, use_default_graph_as_union=False)))["n"].value)
    return time.perf_counter() - t, n


def stage_queries(a):
    t_open = time.perf_counter()
    st = open_store(a.store)
    rnd = random.Random(a.seed)
    sup_starts = sorted({r["s"].value for r in st.query(
        PFX + "SELECT DISTINCT ?s WHERE { GRAPH <urn:isnad:g:asserted> { ?s sc:supplies ?o } }")})
    ship_starts = sorted({r["s"].value for r in st.query(
        PFX + "SELECT DISTINCT ?s WHERE { GRAPH <urn:isnad:g:asserted> { ?s sc:operated_by ?o } } LIMIT 5000")})
    n = a.n_starts
    out = {"open_s": time.perf_counter() - t_open}
    sets = {"fixed_4hop": (Q_FIXED, rnd.sample(sup_starts, n)), "plus_transitive": (Q_PLUS, rnd.sample(sup_starts, n)),
            "mixed_4hop_shipment": (Q_MIXED, rnd.sample(ship_starts, n)),
            "inferred_lookup": (Q_INF, rnd.sample(sup_starts, n))}
    for name, (q, starts) in sets.items():
        cold, warm, sizes = [], [], []
        for s in starts:
            dt, cnt = _run(st, q.format(s=s)); cold.append(dt); sizes.append(cnt)
        for s in starts:
            dt, _ = _run(st, q.format(s=s)); warm.append(dt)
        out[name] = {"cold": summ(cold), "warm": summ(warm), "first_query_s": cold[0],
                     "result_size_median": statistics.median(sizes), "result_size_max": max(sizes)}
    out["peak_rss_mb"] = rss_mb()
    return out


def stage_shacl(a):
    conn = connect(schema=a.schema)
    st = open_store(a.store)
    rnd = random.Random(a.seed)
    ships = sorted({r["s"].value.rsplit("/", 1)[1] for r in st.query(
        PFX + "SELECT DISTINCT ?s WHERE { GRAPH <urn:isnad:g:asserted> { ?s sc:operated_by ?o } }")})
    changed = [UUID(x) for x in rnd.sample(ships, min(a.shacl_ships, len(ships)))]
    loader = pg_props_loader(conn, ["Shipment", "Port", "Carrier"])
    clean = validate_changed(st, changed, NS, loader)
    # deliberate violation: remove one shipment's required carrier link (real edit + sync)
    victim = changed[0]
    car = conn.execute("SELECT dst FROM cur_link_operated_by WHERE src=%s", [victim]).fetchone()[0]
    now = datetime.now(UTC)
    bt.end_link(conn, "operated_by", victim, car, now - timedelta(seconds=1), "bench"); conn.commit()
    consume_outbox(conn, st)
    bad = validate_changed(st, changed, NS, loader)
    detected = (not bad.conforms) and bad.focus_nodes == {obj_iri(NS, victim).value}
    # restore
    bt.write_link(conn, "operated_by", victim, car, now, "bench"); conn.commit()
    consume_outbox(conn, st)
    return {"clean": {"conforms": clean.conforms, "seconds": clean.seconds, "nodes": clean.nodes,
                      "triples": clean.triples, "phases": clean.phases},
            "violated": {"conforms": bad.conforms, "seconds": bad.seconds, "violations": bad.violations[:3],
                         "n_violations": len(bad.violations)},
            "deliberate_violation_detected": detected, "peak_rss_mb": rss_mb()}


def stage_edits(a):
    conn = connect(schema=a.schema)
    st = open_store(a.store)
    rnd = random.Random(a.seed + 7)
    ships = [r[0] for r in conn.execute("SELECT id FROM cur_obj_shipment LIMIT 3000").fetchall()]
    ports = [r[0] for r in conn.execute("SELECT id FROM cur_obj_port").fetchall()]
    cars = [r[0] for r in conn.execute("SELECT id FROM cur_obj_carrier").fetchall()]
    sups = [r[0] for r in conn.execute("SELECT src FROM cur_link_supplies LIMIT 5000").fetchall()]
    conn.rollback()
    now = datetime.now(UTC) - timedelta(minutes=5)
    t = time.perf_counter()
    n = 0
    for k in range(100):
        r = k % 4
        if r == 0:
            s = rnd.choice(ships)
            bt.write_object(conn, "Shipment", s, {"ref": f"EDIT-{k}", "status": "delayed"}, now, "bench")
        elif r == 1:  # new supplies edge
            x, y = rnd.sample(sups, 2)
            bt.write_link(conn, "supplies", x, y, now, "bench")
        elif r == 2:  # reroute via a port
            s = rnd.choice(ships); p = rnd.choice(ports)
            bt.write_link(conn, "ships_via", s, p, now, "bench")
        else:  # end a supplies edge
            row = conn.execute("SELECT src, dst FROM cur_link_supplies OFFSET %s LIMIT 1", [rnd.randrange(1000)]).fetchone()
            if row:
                bt.end_link(conn, "supplies", row[0], row[1], now, "bench")
        n += 1
        conn.commit()
    t_edit = time.perf_counter() - t
    t = time.perf_counter()
    s = consume_outbox(conn, st)
    t_sync = time.perf_counter() - t
    r = materialize(st, NS)
    return {"edits": n, "pg_write_s": t_edit, "sync_s": t_sync, "outbox_rows": s["rows"],
            "quads_added": s["added"], "quads_removed": s["removed"], "rematerialize_s": r.seconds,
            "rematerialize_phases": r.phases, "resync_plus_rematerialize_s": t_sync + r.seconds, "inferred_triples": r.inferred_triples,
            "peak_rss_mb": rss_mb()}


STAGES = {k[6:]: v for k, v in globals().items() if k.startswith("stage_")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--store", required=True)
    p.add_argument("--schema", default="bench")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--n-starts", type=int, default=40)
    p.add_argument("--shacl-ships", type=int, default=330)
    a = p.parse_args()
    print("RESULT " + json.dumps(STAGES[a.stage](a)))


if __name__ == "__main__":
    main()
