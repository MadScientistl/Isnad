"""Per-scenario semantic graph (Phase 5): an isolated in-memory Oxigraph store + reasoning for ONE what-if.

Isolation rules (all enforced by construction and covered by tests/sync/test_scenario_graph.py)
-----------------------------------------------------------------------------------------------
* The store is a fresh `pyoxigraph.Store()`; the main `SyncPipeline`, its store and main
  `inferred_support` rows (scenario_id = 0) are never read or written by anything here.
  `write_support(..., scenario_id=N)` only deletes/inserts rows of scenario N.
* The graph holds the FULL effective state of the scenario (what an admin sees), read by the
  loader/owner connection through the `eff_obj_*` / `eff_link_*` views with the scenario GUCs set.
  Non-admin reads never see this store directly: `sparql.build_visible_store` and
  `visibility.*` re-filter every node and edge through RLS + D6 (`inferred_support WHERE scenario_id = N`).
* `get_store` first checks, on the CALLER'S connection, that the scenario is visible to the caller
  (`isnad_scenario_visible` + a visible `scenario_state` row); otherwise `ScenarioNotVisible`
  (the HTTP layer maps it to 404 `scenario_not_found`). The cache is therefore safe to share
  between users: a hit still requires that check.
* Cache: key `(scenario_id, data_version, clock)`, LRU of `ISNAD_SCN_GRAPH_CACHE` (default 3) entries,
  thread-safe; a new data_version of a scenario evicts its older entries. One build at a time per
  scenario. Hard cap `ISNAD_SCN_GRAPH_MAX_OBJECTS` (default 20000 objects) -> `GraphTooLarge`
  (HTTP 413). The error carries no counts (a size is data about hidden rows).

Signatures the API layer calls (see also visibility / sparql / shacl):

    from engine.sync import scenario_graph as SG, visibility as V, sparql
    graph = SG.get_store(conn, ctx, comp)                    # conn: caller's scoped conn; raises
                                                             #   ScenarioNotVisible (404) | GraphTooLarge (413)
    V.bfs(conn, center, depth, pipeline=graph, scenario=ctx, ...)      # /graph  (as_of must be None)
    V.node_infos(conn, ids, cfg=cfg, model=om, scenario=ctx)
    V.find_paths(conn, a, b, pipeline=graph, scenario=ctx, ...)
    V.visible_ids / visible_edges / all_visible_ids / edges_exist / inferred_visible(..., scenario=ctx)
    sparql.run_query(graph, conn, is_admin, query, scenario=ctx)       # /sparql ("pipeline" arg = the graph)
    graph.inferred_edges(ids); graph.store; graph.cfg; graph.data_version
    SG.invalidate(scenario_id)                               # after discard / merge / purge
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from itertools import islice

import psycopg
from psycopg import sql
from pyoxigraph import Quad, Store

from engine.db import bitemporal as bt
from engine.db.connection import connect, get_dsn
from engine.db.principal import OWNER, ScenarioCtx, scoped
from engine.reason.materialize import materialize, write_support
from . import iri
from .pipeline import store_inferred_edges
from .store import SyncConfig, _set_last_seq, open_store


class GraphTooLarge(RuntimeError):
    """The scenario holds more objects than ISNAD_SCN_GRAPH_MAX_OBJECTS (HTTP 413). No counts in the message."""


class GraphBusy(RuntimeError):
    """Too many scenario graph builds are running (ISNAD_SCN_MAX_GRAPH_BUILDS); HTTP 503 `graph_busy`."""


class ScenarioNotVisible(LookupError):
    """The scenario does not exist, is closed, or is not visible to the caller (HTTP 404 scenario_not_found)."""


def max_objects() -> int:
    return int(os.environ.get("ISNAD_SCN_GRAPH_MAX_OBJECTS", "20000"))


_build_slots: dict[int, threading.BoundedSemaphore] = {}
_build_slots_lock = threading.Lock()


def _slots() -> threading.BoundedSemaphore:
    """Process-wide limit on concurrent scenario graph builds (review M4c): each build holds the full effective
    state of a scenario in memory and runs reasoning, so builds for DIFFERENT scenarios must not pile up."""
    n = max(1, int(os.environ.get("ISNAD_SCN_MAX_GRAPH_BUILDS", "2")))
    with _build_slots_lock:
        if n not in _build_slots:
            _build_slots[n] = threading.BoundedSemaphore(n)
        return _build_slots[n]


def build_timeout_s() -> float:
    return float(os.environ.get("ISNAD_SCN_GRAPH_TIMEOUT_S", "120"))


def cache_size() -> int:
    return max(1, int(os.environ.get("ISNAD_SCN_GRAPH_CACHE", "3")))


@dataclass(eq=False)
class ScenarioGraph:
    """Immutable-after-build semantic graph of one scenario. Duck-types the parts of `SyncPipeline` that the
    read layer uses (`cfg`, `store`, `_lock`, `inferred_edges`), so it can be passed as `pipeline=`."""
    scenario_id: int
    ctx: ScenarioCtx
    data_version: int
    cfg: SyncConfig
    store: Store
    built_at: float = field(default_factory=time.time)
    build_seconds: float = 0.0
    objects: int = 0
    links: int = 0
    inferred_triples: int = 0
    support_stats: dict | None = None
    inference_error: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def inferred_edges(self, ids) -> list[tuple]:
        """Instance-to-instance inferred edges (src, link, dst) of THIS scenario touching any id (sorted).
        Unfiltered: callers must pass them through `visibility.inferred_visible(..., scenario=ctx)`."""
        with self._lock:
            return store_inferred_edges(self.store, self.cfg.namespace, ids)


# --------------------------------------------------------------------------- cache
_cache: "OrderedDict[tuple, ScenarioGraph]" = OrderedDict()
_cache_lock = threading.Lock()
_build_locks: dict[tuple, threading.Lock] = {}
_stats = {"hits": 0, "misses": 0, "builds": 0}


def _key(ctx: ScenarioCtx, data_version: int) -> tuple:
    return (ctx.id, int(data_version), ctx.clock.isoformat() if ctx.clock is not None else "")


def invalidate(scenario_id: int, schema: str | None = None) -> int:
    """Drop every cached graph of a scenario (discard / merge / expiry / purge); `schema` None = all schemas.
    Returns entries dropped."""
    with _cache_lock:
        ks = [k for k in _cache if k[1] == scenario_id and (schema is None or k[0] == schema)]
        for k in ks:
            del _cache[k]
        return len(ks)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def cache_info() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "keys": list(_cache), **_stats}


def _cfg_of(comp) -> SyncConfig:
    m = comp.model
    return SyncConfig(m.namespace, tuple(m.object_types), tuple(m.link_types))


# --------------------------------------------------------------------------- build
def _load(oc, ctx: ScenarioCtx, cfg: SyncConfig, cap: int) -> tuple[list[Quad], int, int, int]:
    """Read the scenario-effective ids/edges as OWNER (scenario GUCs set). Returns (quads, dv, n_obj, n_link)."""
    ns = cfg.namespace
    quads: list[Quad] = []
    remaining = cap
    n_obj = n_link = 0
    dv = oc.execute("SELECT data_version FROM scenario_state WHERE scenario_id = %s", [ctx.id]).fetchone()
    dv = int(dv[0]) if dv else 0
    for typ in cfg.object_types:
        cls = iri.class_iri(ns, typ)
        rows = oc.execute(sql.SQL("SELECT DISTINCT id FROM {} LIMIT %s").format(
            sql.Identifier("eff_" + bt.obj_table(typ))), [remaining + 1]).fetchall()
        if len(rows) > remaining:
            raise GraphTooLarge("scenario graph exceeds the object limit")
        remaining -= len(rows)
        n_obj += len(rows)
        quads.extend(Quad(iri.obj_iri(ns, r[0]), iri.RDF_TYPE, cls, iri.G_ASSERTED) for r in rows)
    for typ in cfg.link_types:
        p = iri.link_iri(ns, typ)
        rows = oc.execute(sql.SQL("SELECT DISTINCT src, dst FROM {} LIMIT %s").format(
            sql.Identifier("eff_" + bt.link_table(typ))), [remaining + 1]).fetchall()
        if len(rows) > remaining:  # links count against the same cap as objects
            raise GraphTooLarge("scenario graph exceeds the size limit")
        remaining -= len(rows)
        n_link += len(rows)
        quads.extend(Quad(iri.obj_iri(ns, s), p, iri.obj_iri(ns, d), iri.G_ASSERTED) for s, d in rows)
    return quads, dv, n_obj, n_link


def _build(ctx: ScenarioCtx, comp, dsn: str | None, schema: str | None) -> ScenarioGraph:
    t0 = time.perf_counter()
    cfg = _cfg_of(comp)
    oc = connect(dsn or get_dsn(), schema=schema)
    try:
        oc.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        tmo = f"{int(build_timeout_s() * 1000)}ms"
        oc.execute("SELECT set_config('statement_timeout', %s, true)", [tmo])
        with scoped(oc, OWNER, scenario=ctx):
            quads, dv, n_obj, n_link = _load(oc, ctx, cfg, max_objects())
        oc.rollback()
        oc.execute("SELECT set_config('statement_timeout', %s, true)", [tmo])  # the next transaction (support rows)
        store = open_store(None, cfg)          # fresh in-memory store, ontology graph loaded
        for i in range(0, len(quads), 200_000):
            store.bulk_extend(quads[i:i + 200_000])
        _set_last_seq(store, dv)
        err, n_inf = None, 0
        try:
            res = materialize(store, cfg.namespace)
            n_inf = res.inferred_triples
        except Exception as e:  # noqa: BLE001 - the inferred layer is optional (mirrors the main pipeline)
            err = f"{type(e).__name__}: {e}"
        stats = None
        if err is None:
            try:
                stats = write_support(oc, store, cfg.namespace, data_version=dv, scenario_id=ctx.id)
            except psycopg.errors.UndefinedTable:
                stats = None
        oc.commit()
    finally:
        oc.close()
    g = ScenarioGraph(ctx.id, ctx, dv, cfg, store, build_seconds=time.perf_counter() - t0, objects=n_obj,
                      links=n_link, inferred_triples=n_inf, support_stats=stats, inference_error=err)
    return g


def get_store(conn, ctx: ScenarioCtx, comp, *, dsn: str | None = None, schema: str | None = None) -> ScenarioGraph:
    """The (cached) semantic graph of scenario `ctx`, rebuilt when `scenario_state.data_version` changed.

    `conn` = the CALLER's connection (inside `scoped(conn, principal, ctx)`): used only for the visibility
    check and the current data_version; the build itself runs on its own owner connection. `comp` = the
    compiled ontology (`engine.compiler.compile_ontology(...)`). `dsn`/`schema` default to the env DSN and the
    caller's `current_schema()`. Raises ScenarioNotVisible, GraphTooLarge.
    """
    if not isinstance(ctx, ScenarioCtx):
        raise TypeError("ctx must be a ScenarioCtx")
    row = conn.execute(
        "SELECT isnad_scenario_visible(%s), (SELECT data_version FROM scenario_state WHERE scenario_id = %s), "
        "current_schema()", [ctx.id, ctx.id]).fetchone()
    if not row[0] or row[1] is None:
        raise ScenarioNotVisible(str(ctx.id))
    dv, schema = int(row[1]), schema or row[2]
    k = (schema,) + _key(ctx, dv)
    with _cache_lock:
        g = _cache.get(k)
        if g is not None:
            _cache.move_to_end(k)
            _stats["hits"] += 1
            return g
        _stats["misses"] += 1
        blk = _build_locks.setdefault((schema, ctx.id), threading.Lock())
    with blk:
        with _cache_lock:                      # another thread may have built it while we waited
            g = _cache.get(k)
            if g is not None:
                _cache.move_to_end(k)
                return g
        slots = _slots()
        if not slots.acquire(timeout=max(1.0, build_timeout_s() / 4)):
            raise GraphBusy("too many scenario graph builds are running")
        try:
            g = _build(ctx, comp, dsn, schema)
        finally:
            slots.release()
        kb = (schema,) + _key(ctx, g.data_version)
        with _cache_lock:
            _stats["builds"] += 1
            for old in [x for x in _cache if x[0] == schema and x[1] == ctx.id and x != kb]:
                del _cache[old]               # superseded versions of the same scenario
            _cache[kb] = g
            _cache.move_to_end(kb)
            while len(_cache) > cache_size():
                _cache.popitem(last=False)
        return g
