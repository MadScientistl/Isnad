"""Permission-aware read layer over the graph (Phase 3, D4/D6).

Design rules
------------
* NO second policy implementation. Every function takes a psycopg connection that is already
  inside `engine.db.principal.scoped(conn, principal)`; Postgres RLS decides what a row is.
  "Object id X is visible" == "some row for X is returned by `cur_obj_<type>` (current
  version) under the caller's role" (D4: current visibility of the id decides access to all of
  its versions, so for `as_of` queries the same RLS-filtered base tables are used).
* "Link (s,d) is visible" == a row of `link_<l>` is returned under RLS, which already requires
  the link type to be readable and both endpoints visible. Endpoints are nevertheless verified
  again with `visible_ids` (defence in depth; also drops links to ids that have no object row).
* Traversals expand ONLY through visible nodes. A hidden node cuts the path. Nothing in the
  results (including `truncated`, fanout caps, edge lists) is computed from hidden data, so no
  hidden existence or count can leak.
* Inferred edges (D6) are shown iff >= 1 recorded derivation has every support node visible and
  every asserted edge between consecutive support nodes visible (`inferred_visible`). An
  inferred edge without any recorded derivation is hidden for non-admin (fail closed).

Edge tuples everywhere are `(src: UUID, link: str, dst: UUID)` (same order as
`SyncPipeline.inferred_edges`).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Sequence
from uuid import UUID

from psycopg import sql

from engine.db import bitemporal as bt
from engine.reason.materialize import link_semantics
from .store import DEFAULT_YAML, SyncConfig, _yaml_defaults

Edge = tuple  # (UUID src, str link, UUID dst)


class NotVisible(LookupError):
    """The requested object is hidden or does not exist (API: 404, never 403)."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("graph query exceeded its deadline")


def _cfg(cfg: SyncConfig | None) -> SyncConfig:
    return cfg or SyncConfig()


def _uuids(ids: Iterable) -> list[UUID]:
    return [i if isinstance(i, UUID) else UUID(str(i)) for i in ids]


def _rel(table: str, as_of, kind: str) -> tuple[sql.Composable, list]:
    """Relation + params to read `table` (obj_*/link_*) now (cur_ view) or as of valid time."""
    if as_of is None:
        return sql.Identifier("cur_" + table), []
    c, p = bt._asof_clause(as_of, None)
    return sql.SQL("(SELECT * FROM {t} WHERE {c} AND scenario_id = 0)").format(
        t=sql.Identifier(table), c=sql.SQL(c)), p


# --------------------------------------------------------------------------- objects
def visible_ids(conn, ids: Iterable, *, as_of=None, cfg: SyncConfig | None = None) -> set[UUID]:
    """Subset of `ids` the current principal can see (one query over all object types)."""
    ids = list(dict.fromkeys(_uuids(ids)))
    if not ids:
        return set()
    parts, params = [], []
    for t in _cfg(cfg).object_types:
        rel, p = _rel(bt.obj_table(t), as_of, "obj")
        parts.append(sql.SQL("SELECT id FROM {} x WHERE id = ANY(%s::uuid[])").format(rel))
        params += p + [ids]
    q = sql.SQL(" UNION ").join(parts)
    return {r[0] for r in conn.execute(q, params).fetchall()}


def all_visible_ids(conn, *, cfg: SyncConfig | None = None) -> dict[str, list[UUID]]:
    """type -> every currently visible id of that type (used to build the SPARQL view)."""
    out: dict[str, list[UUID]] = {}
    for t in _cfg(cfg).object_types:
        out[t] = [r[0] for r in conn.execute(
            sql.SQL("SELECT DISTINCT id FROM {}").format(sql.Identifier("cur_" + bt.obj_table(t)))).fetchall()]
    return out


def node_infos(conn, ids: Iterable, *, as_of=None, cfg: SyncConfig | None = None,
               model=None) -> dict[str, dict]:
    """{id: {id, type, label, status}} for visible ids, read through the masked `rd_*` views.

    Same shape as the existing /graph node (`label` = name|ref|kind else short id)."""
    ids = list(dict.fromkeys(_uuids(ids)))
    if not ids:
        return {}
    if model is None:
        from engine.compiler import compile_ontology
        model = compile_ontology(DEFAULT_YAML).model
    parts, params = [], []
    for t in _cfg(cfg).object_types:
        ot = model.object_types.get(t)
        if ot is None:
            continue
        pn = {p.name for p in ot.properties}
        lab = next((c for c in ("name", "ref", "kind") if c in pn), None)
        table = bt.obj_table(t)
        if as_of is None:
            src = sql.Identifier(_rd_cur(conn, table))
            where = sql.SQL("id = ANY(%s::uuid[])")
            p = [ids]
        else:
            c, p0 = bt._asof_clause(as_of, None)
            src = sql.Identifier(bt.read_source(conn, table))
            where = sql.SQL("{} AND scenario_id = 0 AND id = ANY(%s::uuid[])").format(sql.SQL(c))
            p = p0 + [ids]
        parts.append(sql.SQL("SELECT id, {t}::text AS type, {lab} AS label, {st} AS status FROM {src} WHERE {w}").format(
            t=sql.Literal(t),
            lab=sql.SQL(f'"{lab}"::text') if lab else sql.SQL("NULL::text"),
            st=sql.SQL('"status"::text') if "status" in pn else sql.SQL("NULL::text"),
            src=src, w=where))
        params += p
    out: dict[str, dict] = {}
    for id_, typ, label, status in conn.execute(sql.SQL(" UNION ALL ").join(parts), params).fetchall():
        out[str(id_)] = {"id": str(id_), "type": typ, "label": label or str(id_)[:8], "status": status}
    return out


def _rd_cur(conn, table: str) -> str:
    rd = f"rd_cur_{table}"
    ok = conn.execute("SELECT to_regclass(%s) IS NOT NULL", [rd]).fetchone()[0]
    return rd if ok else "cur_" + table


# --------------------------------------------------------------------------- edges
def visible_edges(conn, ids: Iterable, *, as_of=None, link_types: Sequence[str] | None = None,
                  direction: str = "both", cfg: SyncConfig | None = None) -> list[Edge]:
    """Asserted edges touching `ids` that survive link RLS AND whose endpoints are both visible."""
    ids = list(dict.fromkeys(_uuids(ids)))
    if not ids:
        return []
    cfg = _cfg(cfg)
    parts, params = [], []
    cond = {"out": "src = ANY(%s::uuid[])", "in": "dst = ANY(%s::uuid[])",
            "both": "(src = ANY(%s::uuid[]) OR dst = ANY(%s::uuid[]))"}[direction]
    for l in cfg.link_types:
        if link_types is not None and l not in link_types:
            continue
        rel, p = _rel(bt.link_table(l), as_of, "link")
        parts.append(sql.SQL("SELECT DISTINCT src, {l}::text AS link, dst FROM {r} x WHERE {c}").format(
            l=sql.Literal(l), r=rel, c=sql.SQL(cond)))
        params += p + [ids] * cond.count("%s")
    if not parts:
        return []
    rows = conn.execute(sql.SQL(" UNION ALL ").join(parts), params).fetchall()
    nodes = {r[0] for r in rows} | {r[2] for r in rows}
    ok = visible_ids(conn, nodes, as_of=as_of, cfg=cfg)
    return sorted({(s, l, d) for s, l, d in rows if s in ok and d in ok}, key=lambda e: (str(e[0]), e[1], str(e[2])))


def edges_exist(conn, link: str, pairs: Iterable[tuple[UUID, UUID]]) -> set[tuple[UUID, UUID]]:
    """Current asserted (src,dst) pairs of `link` visible under link RLS."""
    pairs = list(set(pairs))
    if not pairs:
        return set()
    srcs, dsts = [p[0] for p in pairs], [p[1] for p in pairs]
    rows = conn.execute(
        sql.SQL("SELECT k.src, k.dst FROM {} k JOIN unnest(%s::uuid[], %s::uuid[]) u(s, d) "
                "ON k.src = u.s AND k.dst = u.d").format(sql.Identifier("cur_" + bt.link_table(link))),
        [srcs, dsts]).fetchall()
    return {(r[0], r[1]) for r in rows}


# --------------------------------------------------------------------------- inferred (D6)
def inferred_visible(conn, edges: Iterable[Edge], *, is_admin: bool = False,
                     cfg: SyncConfig | None = None) -> set[Edge]:
    """Subset of inferred `edges` the principal may see.

    An edge is visible iff >= 1 derivation in `inferred_support` exists such that
    (a) every node id in `support` is visible, and
    (b) every asserted edge between consecutive support nodes is visible (link RLS).
    `support` is the ordered node path of the underlying BASE link (for an inverse-named edge
    the path runs dst -> src; see engine.reason.materialize.link_semantics). Edges without any
    recorded derivation are hidden unless `is_admin` (admins see everything).
    Only current state: callers must not ask for inferred edges when `as_of` is in the past.
    """
    edges = list(dict.fromkeys(edges))
    if not edges:
        return set()
    sem = link_semantics()
    rows = conn.execute(
        "SELECT s.link, s.src, s.dst, s.derivation_no, s.support FROM inferred_support s "
        "JOIN unnest(%s::text[], %s::uuid[], %s::uuid[]) e(l, a, b) "
        "ON s.link = e.l AND s.src = e.a AND s.dst = e.b",
        [[e[1] for e in edges], [e[0] for e in edges], [e[2] for e in edges]]).fetchall()
    support_nodes: set[UUID] = set()
    for _, _, _, _, sup in rows:
        support_nodes.update(sup)
    vis = visible_ids(conn, support_nodes, cfg=cfg)
    # consecutive pairs per base link, checked once, in bulk
    need: dict[str, set] = {}
    for link, _, _, _, sup in rows:
        base = sem.get(link, (link, False))[0]
        if all(n in vis for n in sup):
            need.setdefault(base, set()).update(zip(sup, sup[1:]))
    ok_pairs = {b: edges_exist(conn, b, ps) for b, ps in need.items()}
    out: set[Edge] = set()
    for link, src, dst, _, sup in rows:
        if (src, link, dst) in out or len(sup) < 2:
            continue
        base = sem.get(link, (link, False))[0]
        if all(n in vis for n in sup) and all(p in ok_pairs.get(base, ()) for p in zip(sup, sup[1:])):
            out.add((src, link, dst))
    if is_admin:  # admin: everything, including edges without recorded support
        out |= set(edges)
    return out


# --------------------------------------------------------------------------- traversal
@dataclass
class GraphResult:
    """Result of `bfs`. Nodes are visible ids in BFS order (center first)."""
    center: UUID
    order: list[UUID]
    edges: list[Edge]                 # asserted, both endpoints in `order`
    inferred: list[Edge] = field(default_factory=list)   # visible inferred, both endpoints in `order`
    truncated: bool = False           # a cap (max_nodes / max_fanout) cut VISIBLE data only
    inferred_available: bool = False


def bfs(conn, center, depth: int = 1, *, pipeline=None, as_of=None, include_inferred: bool = True,
        link_types: Sequence[str] | None = None, max_nodes: int = 300, max_fanout: int | None = None,
        is_admin: bool = False, deadline: float | None = None, cfg: SyncConfig | None = None) -> GraphResult:
    """Breadth-first expansion from `center` through VISIBLE nodes and VISIBLE edges only.

    Raises `NotVisible` when the center itself is hidden/missing (API: 404). `truncated` is set
    only when a cap cut something that the caller could see anyway. Inferred edges are added
    for the live state (as_of None) between nodes already in the result; they never extend the
    frontier (same behaviour as the pre-Phase-3 /graph).
    `deadline` = time.monotonic() value; TimeoutError when exceeded.
    """
    center = _uuids([center])[0]
    cfg = cfg or (pipeline.cfg if pipeline is not None else SyncConfig())
    if center not in visible_ids(conn, [center], as_of=as_of, cfg=cfg):
        raise NotVisible(str(center))
    nodes = {center: None}
    seen_edges: dict[Edge, None] = {}
    frontier, truncated = [center], False
    for _ in range(depth):
        _check_deadline(deadline)
        if not frontier or truncated:
            break
        edges = visible_edges(conn, frontier, as_of=as_of, link_types=link_types, cfg=cfg)
        if max_fanout is not None:
            per: dict[UUID, int] = {}
            kept = []
            for e in edges:
                ends = [x for x in (e[0], e[2]) if x in frontier]
                if any(per.get(x, 0) >= max_fanout for x in ends):
                    truncated = True
                    continue
                for x in ends:
                    per[x] = per.get(x, 0) + 1
                kept.append(e)
            edges = kept
        cand: list[UUID] = []
        for e in edges:
            seen_edges[e] = None
            for x in (e[0], e[2]):
                if x not in nodes and x not in cand:
                    cand.append(x)
        frontier = []
        for x in cand:
            if len(nodes) >= max_nodes:
                truncated = True
                break
            nodes[x] = None
            frontier.append(x)
    order = list(nodes)
    res = GraphResult(center, order, [e for e in seen_edges if e[0] in nodes and e[2] in nodes], truncated=truncated)
    if as_of is None and include_inferred and pipeline is not None:
        res.inferred_available = True
        try:
            have = {(e[0], e[2], e[1]) for e in res.edges}
            cand_inf = [e for e in pipeline.inferred_edges(order)
                        if e[0] in nodes and e[2] in nodes and (e[0], e[2], e[1]) not in have]
            vis = inferred_visible(conn, cand_inf, is_admin=is_admin, cfg=cfg)
            res.inferred = sorted(vis, key=lambda e: (str(e[0]), e[1], str(e[2])))
        except Exception:  # noqa: BLE001 - mirror old behaviour: inferred layer is optional
            res.inferred_available = False
            res.inferred = []
    return res


def find_paths(conn, a, b, *, max_hops: int = 4, pipeline=None, as_of=None, include_inferred: bool = False,
               link_types: Sequence[str] | None = None, max_paths: int = 20, max_nodes: int = 2000,
               is_admin: bool = False, deadline: float | None = None,
               cfg: SyncConfig | None = None) -> list[list[tuple[UUID, str | None, str | None]]]:
    """Shortest-first simple paths a -> b, treating links as undirected, checking EVERY hop.

    Each hop is an edge returned by `visible_edges` between two visible nodes (hidden middle
    nodes cut the path). Returns paths as lists of `(node, link_used_to_reach_it, direction)`
    where direction is 'out' (edge points forward along the path) or 'in'; the first element is
    `(a, None, None)`. Inferred edges are used only if include_inferred and as_of is None, and
    only when `inferred_visible`. Raises NotVisible if either end is hidden/missing.
    """
    a, b = _uuids([a, b])
    cfg = cfg or (pipeline.cfg if pipeline is not None else SyncConfig())
    if {a, b} - visible_ids(conn, [a, b], as_of=as_of, cfg=cfg):
        raise NotVisible("endpoint not visible")
    parents: dict[UUID, list[tuple[UUID, str, str]]] = {a: []}
    level, dist = [a], {a: 0}
    found = a == b
    for hop in range(max_hops):
        if found or not level or len(dist) > max_nodes:
            break
        _check_deadline(deadline)
        edges = visible_edges(conn, level, as_of=as_of, link_types=link_types, cfg=cfg)
        if include_inferred and as_of is None and pipeline is not None:
            ie = pipeline.inferred_edges(level)
            edges = edges + sorted(inferred_visible(conn, ie, is_admin=is_admin, cfg=cfg),
                                   key=lambda e: (str(e[0]), e[1], str(e[2])))
        nxt = []
        for s, l, d in edges:
            for here, there, dr in ((s, d, "out"), (d, s, "in")):
                if here in level and dist.get(there, hop + 1) == hop + 1 and there != here:
                    if there not in dist:
                        dist[there] = hop + 1
                        nxt.append(there)
                    parents.setdefault(there, []).append((here, l, dr))
        level = nxt
        found = b in dist
    if b not in dist:
        return []
    paths: list = []

    def walk(node, tail):
        if len(paths) >= max_paths:
            return
        if node == a:
            paths.append([(a, None, None)] + tail)
            return
        for p, l, dr in parents.get(node, []):
            walk(p, [(node, l, dr)] + tail)

    walk(b, [])
    return paths
