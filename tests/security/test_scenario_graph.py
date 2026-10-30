"""Scenario copy of the multi-hop / inferred / SPARQL hidden-object suite (Phase 5).

A committed scenario changes the world (hidden intermediate nodes created, a node's region changed, a visible
derivation removed). Every role's view INSIDE the scenario is compared with the brute-force oracle run over
the scenario-effective world; main must not move; another user's scenario is unreadable.
"""
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).parent))
from conftest import NOW, PRINCIPALS, T0, W, build_pipeline, make_schema, wipe  # noqa: E402
from oracle import Oracle  # noqa: E402
from engine.compiler import compile_ontology  # noqa: E402
from engine.db import OWNER, Principal, bitemporal as bt, connect, scenario_admin, scoped  # noqa: E402
from engine.sync import scenario_graph as SG, sparql as S, visibility as V  # noqa: E402
from engine.sync.iri import G_ASSERTED, obj_iri  # noqa: E402
from engine.sync.store import DEFAULT_YAML, snapshot  # noqa: E402

NS = "https://example.org/sc#"
PFX = f"PREFIX sc: <{NS}> "
COMP = compile_ontology(DEFAULT_YAML)
ROLES = ["regional_ops_lead", "planner", "finance_analyst", "restricted", "admin"]
NONADMIN = [r for r in ROLES if r != "admin"]
ALL_ROLES = ("regional_ops_lead", "planner", "finance_analyst", "restricted")
ADMIN_P = PRINCIPALS["admin"]


def _build_base(c):
    wipe(c)
    w = W(c)
    w.obj("p_emea", "Port", name="Rotterdam", region="EMEA", status="open")
    w.obj("p_emea2", "Port", name="Hamburg", region="EMEA", status="open")
    w.obj("p_apac", "Port", name="Singapore", region="APAC", status="open")
    w.obj("p_amer", "Port", name="Houston", region="AMER", status="open")
    w.obj("w_emea", "Warehouse", name="W-EMEA", region="EMEA")
    w.obj("w_apac", "Warehouse", name="W-APAC", region="APAC")
    w.obj("w_emea2", "Warehouse", name="W-EMEA2", region="EMEA")
    w.obj("car", "Carrier", name="Maersk")
    for s in ("s1", "s2", "s4"):
        w.obj(s, "Shipment", ref=s.upper(), status="in_transit")
    w.link("ships_to", "s1", "w_emea"); w.link("ships_to", "s2", "w_apac"); w.link("ships_to", "s4", "w_emea2")
    w.link("ships_via", "s1", "p_emea"); w.link("ships_via", "s1", "p_apac")
    w.link("ships_via", "s2", "p_emea"); w.link("ships_via", "s2", "p_apac")
    w.link("ships_via", "s4", "p_apac")
    w.link("operated_by", "s1", "car"); w.link("operated_by", "s2", "car")
    w.obj("sup_a", "Supplier", name="A", country="DE")
    w.obj("inv1", "Invoice", ref="INV-1", amount=10.0, status="open")
    w.link("invoices_for", "inv1", "s1"); w.link("billed_by", "inv1", "sup_a")
    for k, ctry in (("b", "XX"), ("c", "FR"), ("d", "FR"), ("e", "FR"), ("f", "FR"), ("g", "XX"), ("h", "FR")):
        w.obj("sup_" + k, "Supplier", name=k.upper(), country=ctry)
    for a, b in (("sup_a", "sup_b"), ("sup_b", "sup_c"), ("sup_c", "sup_d"), ("sup_a", "sup_e"), ("sup_e", "sup_d"),
                 ("sup_f", "sup_g"), ("sup_g", "sup_h")):
        w.link("supplies", a, b)
    w.commit()
    return w


class SW:
    """Base world + one committed scenario (owner admin, shared with every role) + the oracle over its effective state."""

    def __init__(self, schema):
        self.schema = schema
        self.c = connect(schema=schema)
        self.w = _build_base(self.c)
        self.pipe = build_pipeline(schema)
        self.names = dict(self.w.names)
        self.types = dict(self.w.types)
        self.props = {k: dict(v) for k, v in self.w.props.items()}
        self.links = list(self.w.links)
        self.main_snap = (snapshot(self.pipe.store, G_ASSERTED),
                          snapshot(self.pipe.store, __import__("engine.sync.iri", fromlist=["x"]).G_INFERRED))
        self.main_support = self.support(0)
        self.ctx = self._scenario()
        self.main_support_after = None

    def __getitem__(self, k):
        return self.names[k]

    def support(self, sid):
        return self.c.execute("SELECT link, src, dst, derivation_no, support FROM inferred_support "
                              "WHERE scenario_id = %s ORDER BY 1,2,3,4", [sid]).fetchall()

    def _scenario(self):
        r = scenario_admin.create_base(self.c, name="leaks", owner_user=ADMIN_P.user, owner_role="admin",
                                       shared_roles=list(ALL_ROLES))
        self.c.commit()
        ctx = scenario_admin.ctx_of(self.c, r["id"])
        t = datetime.now(UTC) - timedelta(minutes=5)
        with scoped(self.c, OWNER, scenario=ctx):
            def obj(key, typ, **p):
                i = self.names.get(key) or uuid.uuid4()
                bt.write_object(self.c, typ, i, p, t, "u", scenario_id=ctx.id)
                self.names[key], self.types[i] = i, typ
                self.props[i] = dict(p)

            def link(l, a, b):
                bt.write_link(self.c, l, self[a], self[b], t, "u", scenario_id=ctx.id)
                self.links.append((l, self[a], self[b]))

            # (1) node whose region changes: p_apac becomes EMEA (visible to the lead), w_emea2 leaves EMEA (s4 hidden)
            obj("p_apac", "Port", name="Singapore", region="EMEA", status="open")
            obj("w_emea2", "Warehouse", name="W-EMEA2", region="APAC")
            # (2) hidden intermediate nodes created in the scenario
            obj("p_scn", "Port", name="Scn", region="APAC", status="open")           # hidden to the lead
            obj("w_scn", "Warehouse", name="W-SCN", region="APAC")                  # hidden to the lead
            obj("s6", "Shipment", ref="S6", status="planned")                       # heads to w_scn: hidden to the lead
            obj("s5", "Shipment", ref="S5", status="planned")                       # heads to w_emea: visible to the lead
            link("ships_to", "s6", "w_scn"); link("ships_to", "s5", "w_emea")
            link("ships_via", "s1", "p_scn"); link("ships_via", "s6", "p_scn"); link("ships_via", "s5", "p_apac")
            # supplier chain h -> n(XX hidden for `restricted`) -> m, d -> h
            obj("sup_n", "Supplier", name="N", country="XX")
            obj("sup_m", "Supplier", name="M", country="FR")
            link("supplies", "sup_h", "sup_n"); link("supplies", "sup_n", "sup_m"); link("supplies", "sup_d", "sup_h")
            # (3) remove the visible derivation e -> d: for `restricted`, a -> d now only exists through hidden b
            bt.end_link(self.c, "supplies", self["sup_e"], self["sup_d"], t, "u", scenario_id=ctx.id)
            self.links.remove(("supplies", self["sup_e"], self["sup_d"]))
        self.c.commit()
        return ctx

    @property
    def orc(self):
        return Oracle(self.types, self.props, self.links)


@pytest.fixture(scope="module")
def sw():
    admin, name = make_schema("scng")
    SG.clear_cache()
    s = SW(name)
    s.graph = SG.get_store(s.c, s.ctx, COMP)
    s.c.commit()
    yield s
    s.c.close()
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()
    SG.clear_cache()


@contextmanager
def in_scn(schema, role, ctx, principal=None):
    c = connect(schema=schema)
    try:
        with scoped(c, principal or PRINCIPALS[role], ctx):
            yield c
    finally:
        c.rollback()
        c.close()


def q(sw, role, text, ctx=None, **kw):
    ctx = ctx or sw.ctx
    with in_scn(sw.schema, role, ctx) as c:
        g = SG.get_store(c, ctx, COMP)
        return S.run_query(g, c, role == "admin", PFX + text, scenario=ctx, **kw)


def pairs(res, a="s", b="o"):
    pre = NS + "obj/"
    return {(r[a]["value"][len(pre):], r[b]["value"][len(pre):]) for r in res["rows"] if a in r and b in r}


# ----------------------------------------------------------------------------- scenario graph itself
def test_graph_has_scenario_edges_inferences_and_support_with_scenario_id(sw):
    g = sw.graph
    h, n, m, d = (sw[x] for x in ("sup_h", "sup_n", "sup_m", "sup_d"))
    inf = set(g.inferred_edges([sw["sup_a"]]))
    assert (d, "supplies", m) in set(g.inferred_edges([d]))
    assert (sw["sup_a"], "supplies", m) in inf                  # a->b->c->d->h->n->m (all asserted in scenario)
    assert (sw["sup_a"], "supplies", n) in inf and (d, "supplies", n) in set(g.inferred_edges([n]))
    rows = sw.support(sw.ctx.id)
    assert rows and any(r[1] == d and r[2] == m for r in rows)


def test_main_untouched_by_scenario_work(sw):
    p = sw.pipe
    p.sync(); p.maybe_reason(force=True)
    from engine.sync.iri import G_INFERRED
    assert snapshot(p.store, G_ASSERTED) == sw.main_snap[0]
    assert snapshot(p.store, G_INFERRED) == sw.main_snap[1]
    assert sw.support(0) == sw.main_support
    for k in ("sup_n", "sup_m", "p_scn", "s6", "s5"):
        assert not list(p.store.quads_for_pattern(obj_iri(NS, sw[k]), None, None, G_ASSERTED))
    assert not any(sw[k] in {x for r in sw.support(0) for x in (r[1], r[2], *r[4])} for k in ("sup_n", "sup_m"))


# ----------------------------------------------------------------------------- visibility == oracle
@pytest.mark.parametrize("role", ROLES)
def test_scenario_visible_ids_match_oracle(sw, role):
    orc = sw.orc
    with in_scn(sw.schema, role, sw.ctx) as c:
        assert V.visible_ids(c, list(sw.types), scenario=sw.ctx) == orc.vis_ids(role)
        flat = {i for ids in V.all_visible_ids(c, scenario=sw.ctx).values() for i in ids}
        assert flat == orc.vis_ids(role)
        ni = V.node_infos(c, list(sw.types), scenario=sw.ctx)
        assert set(ni) == {str(i) for i in orc.vis_ids(role)}


def test_lead_cannot_reach_hidden_scenario_objects(sw):
    lead_hidden = ["p_scn", "w_scn", "s6", "s2", "w_apac", "p_amer", "s4", "inv1", "w_emea2"]
    with in_scn(sw.schema, "regional_ops_lead", sw.ctx) as c:
        for k in lead_hidden:
            with pytest.raises(V.NotVisible):
                V.bfs(c, sw[k], 2, pipeline=sw.graph, scenario=sw.ctx)
            assert V.node_infos(c, [sw[k]], scenario=sw.ctx) == {}
        for depth in (1, 2, 3, 4):
            r = V.bfs(c, sw["s1"], depth, pipeline=sw.graph, scenario=sw.ctx, max_nodes=1000)
            assert not (set(r.order) & {sw[k] for k in lead_hidden})
        # s1 - p_scn(hidden) - s6(hidden): no path through the hidden port; s5 reached via the now-EMEA p_apac only
        assert V.find_paths(c, sw["s1"], sw["s5"], max_hops=4, scenario=sw.ctx)           # via p_apac (EMEA in scenario)
        for k in ("s6", "p_scn", "s4"):
            with pytest.raises(V.NotVisible):
                V.find_paths(c, sw["s1"], sw[k], scenario=sw.ctx)
    # the same lookups on main: the scenario-only objects do not exist, p_apac is still hidden
    with in_scn(sw.schema, "regional_ops_lead", None) as c:
        for k in ("s5", "s6", "p_scn", "p_apac"):
            with pytest.raises(V.NotVisible):
                V.bfs(c, sw[k], 1, pipeline=sw.pipe)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("depth", [1, 2, 3, 4])
def test_scenario_bfs_matches_oracle(sw, role, depth):
    orc = sw.orc
    vis = orc.vis_ids(role)
    with in_scn(sw.schema, role, sw.ctx) as c:
        for key in sw.names:
            cid = sw[key]
            if cid not in vis:
                with pytest.raises(V.NotVisible):
                    V.bfs(c, cid, depth, pipeline=sw.graph, scenario=sw.ctx)
                continue
            r = V.bfs(c, cid, depth, pipeline=sw.graph, scenario=sw.ctx, max_nodes=1000,
                      is_admin=role == "admin")
            assert set(r.order) == orc.reach(role, cid, depth), (role, key, depth)
            assert set(r.order) <= vis and not r.truncated
            assert set(r.edges) <= {(a, l, b) for l, a, b in orc.vedges(role)}
            assert {x for e in r.inferred for x in (e[0], e[2])} <= vis
            # inferred edges: exactly the oracle's, restricted to the returned nodes and not asserted
            asserted = {(a, l, b) for l, a, b in orc.vedges(role)}
            want = {e for e in orc.inferred(role) if e[0] in r.order and e[2] in r.order and e not in asserted}
            if role != "admin":
                assert set(r.inferred) == want, (role, key, depth)


@pytest.mark.parametrize("role", ROLES)
def test_scenario_find_paths_every_hop_visible(sw, role):
    orc = sw.orc
    vis = orc.vis_ids(role)
    with in_scn(sw.schema, role, sw.ctx) as c:
        for a in sw.names.values():
            for b in sw.names.values():
                if a == b or a not in vis or b not in vis:
                    continue
                ps = V.find_paths(c, a, b, max_hops=4, max_paths=50, scenario=sw.ctx)
                assert all(n in vis for p in ps for n, _, _ in p)
                assert bool(ps) == orc.shortest_paths_exist(role, a, b, 4)


def test_restricted_inferred_and_d6_in_scenario(sw):
    A, B, D, H, N, M = (sw[x] for x in ("sup_a", "sup_b", "sup_d", "sup_h", "sup_n", "sup_m"))
    with in_scn(sw.schema, "restricted", sw.ctx) as c:
        r = V.bfs(c, A, 6, pipeline=sw.graph, scenario=sw.ctx, max_nodes=1000)
        inf = set(r.inferred)
        # removing e->d leaves a->d only through hidden b: not visible; n (XX) cuts h -> m
        assert (A, "supplies", D) not in inf and (D, "supplied_by", A) not in inf
        assert N not in r.order and B not in r.order and M not in r.order
        assert (H, "supplies", M) not in inf and (D, "supplies", M) not in inf
        r = V.bfs(c, D, 3, pipeline=sw.graph, scenario=sw.ctx, max_nodes=1000)
        assert N not in r.order and M not in r.order and (D, "supplies", M) not in set(r.inferred)
        # probing the hidden inferred edges directly
        probe = [(A, "supplies", D), (D, "supplied_by", A), (D, "supplies", M), (M, "supplied_by", D), (H, "supplies", M)]
        assert V.inferred_visible(c, probe, scenario=sw.ctx) == set()
        # SQL: the scenario's support rows are readable only when every node of the derivation is visible
        rows = c.execute("SELECT src, dst, support FROM inferred_support WHERE scenario_id = %s", [sw.ctx.id]).fetchall()
        seen = {x for r_ in rows for x in (r_[0], r_[1], *r_[2])}
        assert not (seen & {B, N, sw["sup_g"]})
        # main-context rows of this scenario cannot be read with the main GUC
    with in_scn(sw.schema, "planner", sw.ctx) as c:               # planner sees every supplier: a ->...-> m exists
        r = V.bfs(c, A, 8, pipeline=sw.graph, scenario=sw.ctx, max_nodes=1000)
        assert (A, "supplies", M) in set(r.inferred) and (A, "supplies", D) in set(r.inferred)
    with in_scn(sw.schema, "admin", sw.ctx) as c:
        r = V.bfs(c, A, 8, pipeline=sw.graph, scenario=sw.ctx, max_nodes=1000, is_admin=True)
        assert (A, "supplies", M) in set(r.inferred)
    # main is unaffected: a->d still inferred on main for restricted (visible derivation a-e-d), m does not exist
    with in_scn(sw.schema, "restricted", None) as c:
        r = V.bfs(c, A, 6, pipeline=sw.pipe, max_nodes=1000)
        assert (A, "supplies", D) in set(r.inferred) and M not in r.order


def test_main_pipeline_cannot_leak_into_scenario_reads(sw):
    """Passing the MAIN pipeline in a scenario read yields no inferred edges (never main's)."""
    A = sw["sup_a"]
    with in_scn(sw.schema, "planner", sw.ctx) as c:
        r = V.bfs(c, A, 4, pipeline=sw.pipe, scenario=sw.ctx, max_nodes=1000)
        assert r.inferred == [] and not r.inferred_available
        with pytest.raises(ValueError):
            S.run_query(sw.pipe, c, False, PFX + "SELECT ?s WHERE { ?s a sc:Port }", scenario=sw.ctx)


# ----------------------------------------------------------------------------- SPARQL
@pytest.mark.parametrize("role", NONADMIN)
def test_scenario_sparql_types_match_oracle(sw, role):
    orc = sw.orc
    for cls, typ in (("Port", "Port"), ("Shipment", "Shipment"), ("Supplier", "Supplier"), ("Invoice", "Invoice")):
        res = q(sw, role, f"SELECT ?s WHERE {{ ?s a sc:{cls} }}")
        got = {r["s"]["value"][len(NS + "obj/"):] for r in res["rows"]}
        assert got == {str(i) for i in orc.vis_ids(role) if sw.types[i] == typ}, (role, cls)


@pytest.mark.parametrize("role", NONADMIN)
def test_scenario_sparql_supplies_edges_match_oracle(sw, role):
    orc = sw.orc
    want = {(str(a), str(b)) for l, a, b in orc.vedges(role) if l == "supplies"}
    want |= {(str(a), str(b)) for a, l, b in orc.inferred(role) if l == "supplies"}
    assert pairs(q(sw, role, "SELECT ?s ?o WHERE { ?s sc:supplies ?o }")) == want
    # property path closure never steps through a hidden node
    closure = pairs(q(sw, role, "SELECT ?s ?o WHERE { ?s sc:supplies+ ?o }"))
    vis = {str(i) for i in orc.vis_ids(role)}
    assert {x for p in closure for x in p} <= vis
    assert closure == want        # asserted + inferred is already transitively closed over visible edges


@pytest.mark.parametrize("role", NONADMIN)
def test_scenario_sparql_cannot_probe_hidden_iris(sw, role):
    orc = sw.orc
    hidden = [i for i in sw.types if not orc.visible(role, i)]
    if role != "finance_analyst":              # finance sees every type of this world
        assert hidden
    for i in hidden:
        iri_ = f"<{obj_iri(NS, i).value}>"
        assert q(sw, role, f"ASK {{ {iri_} ?p ?o }}")["boolean"] is False
        assert q(sw, role, f"ASK {{ ?s ?p {iri_} }}")["boolean"] is False
        assert q(sw, role, f"SELECT ?x WHERE {{ {iri_} ?p ?x }}")["rows"] == []
        res = q(sw, role, f"SELECT ?s WHERE {{ ?s sc:supplies|sc:supplied_by|sc:ships_via|sc:ships_to|sc:operated_by {iri_} }}")
        assert res["rows"] == []
    res = q(sw, role, "SELECT ?s ?p ?o WHERE { ?s ?p ?o }", max_rows=100000)
    leaked = {obj_iri(NS, i).value for i in hidden} & {t["value"] for r in res["rows"] for t in r.values()}
    assert not leaked


def test_scenario_sparql_aggregates_rejected_for_nonadmin_allowed_for_admin(sw):
    for role in NONADMIN:
        with pytest.raises(S.SparqlError):
            q(sw, role, "SELECT (COUNT(*) AS ?n) WHERE { ?s a sc:Port }")
    res = q(sw, "admin", "SELECT (COUNT(*) AS ?n) WHERE { ?s a sc:Supplier }")
    assert int(res["rows"][0]["n"]["value"]) == len(sw.orc.vis_ids("admin") & {i for i, t in sw.types.items() if t == "Supplier"})


def test_admin_sparql_in_scenario_queries_scenario_store_not_main(sw):
    res = q(sw, "admin", "SELECT ?s WHERE { ?s a sc:Shipment }")
    got = {r["s"]["value"][len(NS + "obj/"):] for r in res["rows"]}
    assert str(sw["s5"]) in got and str(sw["s6"]) in got
    # main live store (same pipeline object used by the main API) has neither
    with in_scn(sw.schema, "admin", None) as c:
        main = S.run_query(sw.pipe, c, True, PFX + "SELECT ?s WHERE { ?s a sc:Shipment }")
    mg = {r["s"]["value"][len(NS + "obj/"):] for r in main["rows"]}
    assert str(sw["s5"]) not in mg and str(sw["s6"]) not in mg
    # inferred supplies edge only exists in the scenario graph
    a = q(sw, "admin", f"ASK {{ <{obj_iri(NS, sw['sup_d']).value}> sc:supplies <{obj_iri(NS, sw['sup_m']).value}> }}")
    assert a["boolean"] is True
    with in_scn(sw.schema, "admin", None) as c:
        a = S.run_query(sw.pipe, c, True, PFX + f"ASK {{ <{obj_iri(NS, sw['sup_d']).value}> sc:supplies ?x }}")
    assert a["boolean"] is False                          # main: d supplies nobody
    with in_scn(sw.schema, "admin", None) as c:
        a = S.run_query(sw.pipe, c, True, PFX + f"ASK {{ <{obj_iri(NS, sw['sup_d']).value}> sc:supplies <{obj_iri(NS, sw['sup_m']).value}> }}")
    assert a["boolean"] is False


# ----------------------------------------------------------------------------- other users' scenarios
def test_other_users_scenario_is_unreadable(sw):
    # a private scenario of the planner u-plan (not shared)
    r = scenario_admin.create_base(sw.c, name="private", owner_user="u-plan", owner_role="planner")
    sw.c.commit()
    pctx = scenario_admin.ctx_of(sw.c, r["id"])
    with scoped(sw.c, OWNER, scenario=pctx):
        bt.write_object(sw.c, "Shipment", uuid.uuid4(), {"ref": "PRIV", "status": "planned"},
                        datetime.now(UTC) - timedelta(minutes=5), "u", scenario_id=pctx.id)
    sw.c.commit()
    other = Principal("planner", "u-plan-2", "")
    with in_scn(sw.schema, "planner", pctx, principal=other) as c:
        with pytest.raises(SG.ScenarioNotVisible):
            SG.get_store(c, pctx, COMP)                                     # cache or not
        # forged context: every read is empty / fails closed
        assert V.visible_ids(c, list(sw.types), scenario=pctx) == set()
        assert V.all_visible_ids(c, scenario=pctx) == {t: [] for t in V.all_visible_ids(c, scenario=pctx)}
        assert c.execute("SELECT count(*) FROM inferred_support WHERE scenario_id = %s", [pctx.id]).fetchone()[0] == 0
    owner = PRINCIPALS["planner"]                                            # u-plan: allowed
    with in_scn(sw.schema, "planner", pctx, principal=owner) as c:
        g = SG.get_store(c, pctx, COMP)
        assert V.visible_ids(c, list(sw.types), scenario=pctx)
    # a graph built for scenario 1 cannot be passed as the graph of scenario 2
    with in_scn(sw.schema, "planner", pctx, principal=owner) as c:
        with pytest.raises(ValueError):
            S.run_query(sw.graph, c, False, PFX + "SELECT ?s WHERE { ?s a sc:Port }", scenario=pctx)
        r2 = V.bfs(c, sw["s1"], 2, pipeline=sw.graph, scenario=pctx, max_nodes=100)      # foreign graph is ignored
        assert r2.inferred == []
    # the scenario's own object is not visible in the other scenario or in main
    with in_scn(sw.schema, "planner", sw.ctx) as c:
        names = {x["label"] for x in V.node_infos(c, [i for i in sw.types], scenario=sw.ctx).values()}
        assert "PRIV" not in names
    SG.invalidate(pctx.id)


def test_main_unchanged_after_everything(sw):
    from engine.sync.iri import G_INFERRED
    sw.pipe.sync(); sw.pipe.maybe_reason(force=True)
    assert snapshot(sw.pipe.store, G_ASSERTED) == sw.main_snap[0]
    assert snapshot(sw.pipe.store, G_INFERRED) == sw.main_snap[1]
    assert sw.support(0) == sw.main_support
