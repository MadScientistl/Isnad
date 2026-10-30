"""Scenario graph (Phase 5): build, isolation from main, cache, LRU, size cap, SHACL on a scenario subgraph."""
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pyoxigraph import Quad

from engine.compiler import compile_ontology
from engine.db import OWNER, bitemporal as bt, connect, scenario_admin, scoped
from engine.reason.materialize import link_semantics
from engine.sync import scenario_graph as SG, visibility as V
from engine.sync.iri import G_ASSERTED, G_INFERRED, link_iri, obj_iri
from engine.sync.pipeline import SyncPipeline
from engine.sync.store import DEFAULT_YAML, snapshot

NS = "https://example.org/sc#"
T0 = datetime.now(UTC) - timedelta(days=3)
COMP = compile_ontology(DEFAULT_YAML)


class World:
    def __init__(self, schema):
        self.schema = schema
        self.c = connect(schema=schema)
        self.ids = {}
        for n in "abcd":
            self.ids[n] = uuid.uuid4()
            bt.write_object(self.c, "Supplier", self.ids[n], {"name": n.upper(), "country": "DE"}, T0, "seed")
        bt.write_link(self.c, "supplies", self.ids["a"], self.ids["b"], T0, "seed")
        bt.write_link(self.c, "supplies", self.ids["b"], self.ids["c"], T0, "seed")
        self.c.commit()

    def fork(self, name="t", owner="u-plan", role="planner"):
        r = scenario_admin.create_base(self.c, name=name, owner_user=owner, owner_role=role)
        self.c.commit()
        return scenario_admin.ctx_of(self.c, r["id"])

    def put_link(self, ctx, src, dst, link="supplies"):
        with scoped(self.c, OWNER, scenario=ctx):
            bt.write_link(self.c, link, self.ids.get(src, src), self.ids.get(dst, dst),
                          datetime.now(UTC) - timedelta(minutes=5), "u", scenario_id=ctx.id)
        self.c.commit()

    def put_obj(self, ctx, typ, id_, props):
        with scoped(self.c, OWNER, scenario=ctx):
            bt.write_object(self.c, typ, id_, props, datetime.now(UTC) - timedelta(minutes=5), "u",
                            scenario_id=ctx.id)
        self.c.commit()

    def support(self, sid):
        return self.c.execute("SELECT link, src, dst, derivation_no, support FROM inferred_support "
                              "WHERE scenario_id = %s ORDER BY 1,2,3,4", [sid]).fetchall()


@pytest.fixture
def w(gschema):
    SG.clear_cache()
    w = World(gschema)
    yield w
    w.c.close()
    SG.clear_cache()


def edges(g, kind=G_ASSERTED, pred="supplies"):
    return {(q.subject.value[len(NS + "obj/"):], q.object.value[len(NS + "obj/"):])
            for q in g.store.quads_for_pattern(None, link_iri(NS, pred), None, kind)}


def test_build_asserted_and_scenario_edge(w):
    ctx = w.fork()
    g0 = SG.get_store(w.c, ctx, COMP)
    ids = {k: str(v) for k, v in w.ids.items()}
    assert edges(g0) == {(ids["a"], ids["b"]), (ids["b"], ids["c"])}
    w.put_link(ctx, "c", "d")
    g1 = SG.get_store(w.c, scenario_admin.ctx_of(w.c, ctx.id), COMP)
    assert g1 is not g0 and g1.data_version > g0.data_version
    assert (ids["c"], ids["d"]) in edges(g1)
    # transitive inference exists in the scenario graph (a -> d) ...
    assert (ids["a"], ids["d"]) in edges(g1, G_INFERRED)
    assert any(e[1] == "supplies" and e[2] == w.ids["d"] for e in g1.inferred_edges([w.ids["a"]]))
    # ... and its derivation is recorded with the scenario id
    sup = {(r[1], r[2]): r[4] for r in w.support(ctx.id)}
    assert (w.ids["a"], w.ids["d"]) in sup
    assert sup[(w.ids["a"], w.ids["d"])] == [w.ids[x] for x in "abcd"]


def test_scenario_inference_never_in_main(w):
    p = SyncPipeline(schema=w.schema, min_reason_interval_s=0)
    p.sync(); p.maybe_reason(force=True)
    main_inf = snapshot(p.store, G_INFERRED)
    main_asr = snapshot(p.store, G_ASSERTED)
    main_sup = w.support(0)
    assert main_sup                                         # a->c exists in main
    ctx = w.fork()
    w.put_link(ctx, "c", "d")
    g = SG.get_store(w.c, ctx, COMP)
    assert (str(w.ids["a"]), str(w.ids["d"])) in edges(g, G_INFERRED)
    # main store, main pipeline results and main support rows are byte-identical
    assert snapshot(p.store, G_INFERRED) == main_inf and snapshot(p.store, G_ASSERTED) == main_asr
    assert (str(w.ids["a"]), str(w.ids["d"])) not in {(s[len(NS + "obj/"):], o[len(NS + "obj/"):])
                                                      for s, _, o in snapshot(p.store, G_INFERRED)}
    assert w.support(0) == main_sup
    assert not any(r[1] == w.ids["a"] and r[2] == w.ids["d"] for r in w.support(0))
    assert all(e[2] != w.ids["d"] for e in p.inferred_edges([w.ids["a"]]))
    # the main pipeline refuses scenario reads
    with pytest.raises(ValueError):
        p.inferred_edges([w.ids["a"]], scenario=ctx)
    # re-running main reasoning does not wipe scenario support rows (and vice versa)
    p.maybe_reason(force=True)
    assert w.support(ctx.id)
    SG.invalidate(ctx.id)
    w.c.execute("SELECT 1"); w.c.commit()


def test_main_reasoning_keeps_scenario_rows(w):
    ctx = w.fork()
    w.put_link(ctx, "c", "d")
    SG.get_store(w.c, ctx, COMP)
    before = w.support(ctx.id)
    assert before
    p = SyncPipeline(schema=w.schema, min_reason_interval_s=0)
    p.sync(); p.maybe_reason(force=True)
    assert w.support(ctx.id) == before
    # a second scenario's rows are independent too
    ctx2 = w.fork("t2")
    SG.get_store(w.c, ctx2, COMP)
    assert w.support(ctx.id) == before


def test_scenario_without_changes_equals_main(w):
    ctx = w.fork()
    g = SG.get_store(w.c, ctx, COMP)
    p = SyncPipeline(schema=w.schema, min_reason_interval_s=0)
    p.sync(); p.maybe_reason(force=True)
    assert snapshot(g.store, G_ASSERTED) == snapshot(p.store, G_ASSERTED)
    assert snapshot(g.store, G_INFERRED) == snapshot(p.store, G_INFERRED)


def test_snapshot_semantics_main_changes_after_fork_invisible(w):
    ctx = w.fork()
    new = uuid.uuid4()
    bt.write_object(w.c, "Supplier", new, {"name": "Late", "country": "DE"}, T0, "seed")
    bt.write_link(w.c, "supplies", w.ids["c"], new, T0, "seed")
    w.c.commit()
    g = SG.get_store(w.c, ctx, COMP)
    assert (str(w.ids["c"]), str(new)) not in edges(g)
    assert not list(g.store.quads_for_pattern(obj_iri(NS, new), None, None, G_ASSERTED))


def test_removed_edge_in_scenario(w):
    ctx = w.fork()
    with scoped(w.c, OWNER, scenario=ctx):
        bt.end_link(w.c, "supplies", w.ids["b"], w.ids["c"], datetime.now(UTC) - timedelta(minutes=1), "u",
                    scenario_id=ctx.id)
    w.c.commit()
    g = SG.get_store(w.c, ctx, COMP)
    assert edges(g) == {(str(w.ids["a"]), str(w.ids["b"]))}
    assert edges(g, G_INFERRED) == set()
    assert not [r for r in w.support(ctx.id) if r[0] == 'supplies']   # no transitive a->c any more


def test_cache_hit_miss_invalidate(w):
    ctx = w.fork()
    b0 = SG.cache_info()["builds"]
    g1 = SG.get_store(w.c, ctx, COMP)
    g2 = SG.get_store(w.c, ctx, COMP)
    assert g1 is g2 and SG.cache_info()["builds"] == b0 + 1
    w.put_link(ctx, "c", "d")                              # data_version bumps
    g3 = SG.get_store(w.c, ctx, COMP)
    assert g3 is not g1 and g3.data_version > g1.data_version
    keys = [k for k in SG.cache_info()["keys"] if k[1] == ctx.id]
    assert len(keys) == 1                                  # superseded version evicted
    assert SG.invalidate(ctx.id) == 1
    g4 = SG.get_store(w.c, ctx, COMP)
    assert g4 is not g3


def test_lru_size_three(w):
    ctxs = [w.fork(f"l{i}") for i in range(4)]
    gs = [SG.get_store(w.c, c, COMP) for c in ctxs]
    info = SG.cache_info()
    assert info["size"] == 3
    assert {k[1] for k in info["keys"]} == {c.id for c in ctxs[1:]}      # oldest evicted
    SG.get_store(w.c, ctxs[1], COMP)                                     # touch -> most recent
    SG.get_store(w.c, ctxs[0], COMP)                                     # rebuild evicts ctxs[2]
    assert {k[1] for k in SG.cache_info()["keys"]} == {ctxs[1].id, ctxs[3].id, ctxs[0].id}


def test_graph_too_large(w, monkeypatch):
    ctx = w.fork()
    monkeypatch.setenv("ISNAD_SCN_GRAPH_MAX_OBJECTS", "3")
    with pytest.raises(SG.GraphTooLarge) as e:
        SG.get_store(w.c, ctx, COMP)
    assert "4" not in str(e.value) and "3" not in str(e.value)      # no counts leak
    assert SG.cache_info()["size"] == 0
    monkeypatch.setenv("ISNAD_SCN_GRAPH_MAX_OBJECTS", "1000")
    g = SG.get_store(w.c, ctx, COMP)
    assert g.objects == 4
    # review M4c: links count against the same cap as objects
    SG.clear_cache()
    monkeypatch.setenv("ISNAD_SCN_GRAPH_MAX_OBJECTS", str(g.objects + g.links - 1))
    with pytest.raises(SG.GraphTooLarge):
        SG.get_store(w.c, ctx, COMP)
    monkeypatch.setenv("ISNAD_SCN_GRAPH_MAX_OBJECTS", str(g.objects + g.links))
    assert SG.get_store(w.c, ctx, COMP).objects == 4


def test_invisible_or_unknown_scenario_refused(w):
    from engine.db.principal import Principal, ScenarioCtx
    ctx = w.fork()
    other = Principal("planner", "u-other", "")
    with scoped(w.c, other, ctx):
        with pytest.raises(SG.ScenarioNotVisible):
            SG.get_store(w.c, ctx, COMP)
    w.c.rollback()
    with scoped(w.c, Principal("planner", "u-plan", ""), ctx):
        assert SG.get_store(w.c, ctx, COMP).scenario_id == ctx.id
    w.c.rollback()
    with scoped(w.c, Principal("admin", "u-admin", ""), ctx):
        assert SG.get_store(w.c, ctx, COMP).scenario_id == ctx.id
    w.c.rollback()
    with pytest.raises(SG.ScenarioNotVisible):
        SG.get_store(w.c, ScenarioCtx(999999, datetime.now(UTC)), COMP)
    # a closed (discarded) scenario is refused even for the owner
    w.c.execute("INSERT INTO scenario_event (scenario_id, kind, actor) VALUES (%s,'discarded','u-plan')", [ctx.id])
    w.c.commit()
    with scoped(w.c, Principal("planner", "u-plan", ""), ctx):
        with pytest.raises(SG.ScenarioNotVisible):
            SG.get_store(w.c, ctx, COMP)
    w.c.rollback()


def test_concurrent_get_store_builds_once(w):
    ctx = w.fork()
    out, errs = [], []

    def run():
        c = connect(schema=w.schema)
        try:
            out.append(SG.get_store(c, ctx, COMP))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
        finally:
            c.close()

    b0 = SG.cache_info()["builds"]
    ts = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and len({id(g) for g in out}) == 1
    assert SG.cache_info()["builds"] == b0 + 1


def test_scenario_arg_mismatch_fails_closed(w):
    ctx = w.fork()
    g = SG.get_store(w.c, ctx, COMP)
    ctx2 = w.fork("other")
    from engine.sync import sparql
    with scoped(w.c, OWNER, scenario=ctx):
        with pytest.raises(PermissionError):                 # connection scoped to another scenario
            V.visible_ids(w.c, [w.ids["a"]], scenario=ctx2)
        with pytest.raises(ValueError):
            V.visible_ids(w.c, [w.ids["a"]], scenario=ctx, as_of=datetime.now(UTC))
        with pytest.raises(ValueError):                      # a graph of a different scenario
            sparql.run_query(g, w.c, True, "ASK { ?s ?p ?o }", scenario=ctx2)
    p = SyncPipeline(schema=w.schema)
    with scoped(w.c, OWNER, scenario=ctx):
        with pytest.raises(ValueError):                      # main pipeline in a scenario query
            sparql.run_query(p, w.c, True, "ASK { ?s ?p ?o }", scenario=ctx)
        with pytest.raises(ValueError):                      # scenario graph without its ctx
            sparql.run_query(g, w.c, True, "ASK { ?s ?p ?o }")
        # bfs ignores a main pipeline inside a scenario (no main inferred edges)
        res = V.bfs(w.c, w.ids["a"], 2, pipeline=p, scenario=ctx)
        assert res.inferred == [] and not res.inferred_available


def test_shacl_on_scenario_subgraph(w):
    """A scenario adds an invalid Shipment (no required props); SHACL flags it in the scenario graph only."""
    from engine.reason.shacl import pg_props_loader, validate_changed
    ctx = w.fork()
    sh = uuid.uuid4()
    w.put_obj(ctx, "Shipment", sh, {"ref": "BAD"})          # status etc. missing -> property shape violation
    g = SG.get_store(w.c, ctx, COMP)
    with scoped(w.c, OWNER, scenario=ctx):
        loader = pg_props_loader(w.c, g.cfg.object_types, NS, scenario=ctx)
        res = validate_changed(g, [sh], NS, props_loader=loader, scenario=ctx)
        ok = validate_changed(g, [w.ids["a"]], NS, props_loader=loader, scenario=ctx)
    assert not res.conforms and str(obj_iri(NS, sh).value) in res.focus_nodes
    assert any(v["path"].endswith("operated_by") for v in res.violations)
    assert ok.nodes >= 1 and str(obj_iri(NS, w.ids["a"]).value) not in res.focus_nodes
    # the scenario-only object is absent from main's store/views, so main SHACL has nothing to say about it
    p = SyncPipeline(schema=w.schema, min_reason_interval_s=0); p.sync()
    assert not list(p.store.quads_for_pattern(obj_iri(NS, sh), None, None, G_ASSERTED))
    with pytest.raises(ValueError):
        validate_changed(p.store if False else g, [sh], NS)                      # graph without scenario=
    with pytest.raises(ValueError):
        validate_changed(g, [sh], NS, scenario=scenario_admin.ctx_of(w.c, w.fork("z").id))


def test_concurrent_builds_are_limited_and_busy_is_reported(w, monkeypatch):
    """Review M4c: a global semaphore bounds simultaneous scenario graph builds (GraphBusy -> HTTP 503)."""
    ctx = w.fork()
    monkeypatch.setenv("ISNAD_SCN_MAX_GRAPH_BUILDS", "1")
    monkeypatch.setenv("ISNAD_SCN_GRAPH_TIMEOUT_S", "4")           # wait for a slot: max(1 s, timeout / 4)
    slot = SG._slots()
    assert slot.acquire(timeout=1)
    try:
        t0 = __import__("time").monotonic()
        with pytest.raises(SG.GraphBusy):
            SG.get_store(w.c, ctx, COMP)
        assert __import__("time").monotonic() - t0 < 5
    finally:
        slot.release()
    SG.clear_cache()
    assert SG.get_store(w.c, ctx, COMP).scenario_id == ctx.id      # the slot is free again
    assert SG.build_timeout_s() == 4.0
