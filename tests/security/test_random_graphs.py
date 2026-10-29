"""Property-style test: random graphs vs a brute-force visibility oracle over the full data.

For every role, bfs / inferred edges / SPARQL must (a) never return a hidden id and (b) match the
oracle exactly (so the filter is not just safe but also not over-hiding).
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import W, as_role, make_schema, wipe  # noqa: E402
from psycopg import sql  # noqa: E402
from oracle import Oracle  # noqa: E402
from engine.db import connect  # noqa: E402
from engine.sync import sparql as S, visibility as V  # noqa: E402
from engine.sync.iri import obj_iri  # noqa: E402
from engine.sync.pipeline import SyncPipeline  # noqa: E402

NS = "https://example.org/sc#"
ROLES = ["regional_ops_lead", "planner", "finance_analyst", "restricted", "admin"]
SEEDS = list(range(8))


def random_world(c, rng: random.Random, cycles: bool) -> W:
    w = W(c)
    kinds = {"Port": 5, "Warehouse": 4, "Shipment": 7, "Carrier": 2, "Supplier": 8, "Invoice": 3}
    ids: dict[str, list] = {k: [] for k in kinds}
    for k, n in kinds.items():
        for j in range(n):
            nm = f"{k}{j}"
            if k in ("Port", "Warehouse"):
                w.obj(nm, k, name=nm, region=rng.choice(["EMEA", "APAC", "AMER"]))
            elif k == "Shipment":
                w.obj(nm, k, ref=nm, status="planned")
            elif k == "Invoice":
                w.obj(nm, k, ref=nm, amount=1.0, status="open")
            elif k == "Supplier":
                w.obj(nm, k, name=nm, country=rng.choice(["DE", "FR", "XX", "XX"]))
            else:
                w.obj(nm, k, name=nm)
            ids[k].append(nm)
    seen = set()

    def add(l, a, b):
        if (l, a, b) not in seen:
            seen.add((l, a, b))
            w.link(l, a, b)

    for s in ids["Shipment"]:
        for p in rng.sample(ids["Port"], rng.randint(0, 3)):
            add("ships_via", s, p)
        if rng.random() < .8:
            add("ships_to", s, rng.choice(ids["Warehouse"]))
        if rng.random() < .7:
            add("operated_by", s, rng.choice(ids["Carrier"]))
    for i in ids["Invoice"]:
        add("invoices_for", i, rng.choice(ids["Shipment"]))
        add("billed_by", i, rng.choice(ids["Supplier"]))
    sup = ids["Supplier"]
    for _ in range(rng.randint(6, 12)):
        a, b = rng.sample(range(len(sup)), 2)
        if not cycles and a > b:
            a, b = b, a
        add("supplies", sup[a], sup[b])
    w.commit()
    return w


@pytest.fixture(scope="module")
def rschema():
    """Own schema: these tests wipe and rebuild data, which would destroy the shared `world`."""
    admin, name = make_schema("rnd")
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.mark.parametrize("seed", SEEDS)
def test_random_graph_matches_oracle(rschema, seed):
    schema = rschema
    rng = random.Random(1000 + seed)
    c = connect(schema=schema)
    try:
        wipe(c)
        w = random_world(c, rng, cycles=seed % 2 == 1)
        pipe = SyncPipeline(schema=schema, min_reason_interval_s=0)
        pipe.sync()
        pipe.maybe_reason(force=True)
        orc = Oracle(w.types, w.props, w.links)
        for role in ROLES:
            vis = orc.vis_ids(role)
            hidden_iri = {obj_iri(NS, i).value for i in set(w.types) - vis}
            oracle_inf = orc.inferred(role)
            centers = sorted(vis, key=str)
            rng.shuffle(centers)
            for cid in centers[:5]:
                for depth in (1, 2, 3):
                    with as_role(schema, role) as conn:
                        r = V.bfs(conn, cid, depth, pipeline=pipe, max_nodes=1000, is_admin=role == "admin")
                    assert set(r.order) <= vis
                    assert set(r.order) == orc.reach(role, cid, depth), (seed, role, depth)
                    want_inf = {e for e in oracle_inf if e[0] in r.order and e[2] in r.order}
                    asserted = {(a, l, b) for l, a, b in w.links}
                    want_inf = {e for e in want_inf if e not in asserted}
                    assert set(r.inferred) == want_inf, (seed, role, cid, depth)
            with as_role(schema, role) as conn:
                res = S.run_query(pipe, conn, role == "admin",
                                  f"PREFIX sc: <{NS}> SELECT ?x ?y WHERE {{ ?x sc:supplies ?y }}", max_rows=10_000)
                inv = S.run_query(pipe, conn, role == "admin",
                                  f"PREFIX sc: <{NS}> SELECT ?x ?y WHERE {{ ?x sc:supplied_by ?y }}", max_rows=10_000)
                every = S.run_query(pipe, conn, role == "admin",
                                    "SELECT ?x ?y ?z WHERE { ?x ?y ?z }", max_rows=100_000)
                path = S.run_query(pipe, conn, role == "admin",
                                   f"PREFIX sc: <{NS}> SELECT ?x ?y WHERE {{ ?x (sc:supplies|sc:ships_via|^sc:ships_via)+ ?y }}",
                                   max_rows=100_000)
            for out in (res, inv, every, path):
                assert not ({t["value"] for r_ in out["rows"] for t in r_.values()} & hidden_iri), (seed, role)
            sup_pairs = {(t["x"]["value"], t["y"]["value"]) for t in res["rows"]}
            vedges = orc.vedges(role)
            want = {(obj_iri(NS, a).value, obj_iri(NS, b).value)
                    for l, a, b in vedges if l == "supplies"}
            want |= {(obj_iri(NS, a).value, obj_iri(NS, b).value) for a, l, b in oracle_inf if l == "supplies"}
            assert sup_pairs == want, (seed, role)
            if role == 'admin':
                assert any(e[1] == 'supplies' for e in oracle_inf) or seed % 2 == 0 or want
            inv_pairs = {(t["x"]["value"], t["y"]["value"]) for t in inv["rows"]}
            want_inv = {(y, x) for x, y in want}
            assert inv_pairs == want_inv, (seed, role)
    finally:
        c.rollback()
        c.close()
