"""Outbox consumer + full rebuild for the Oxigraph semantic layer.

Only ids, types and edges are mirrored (no properties). The store is derived:
the *asserted* graph always equals "current views of Postgres" after a sync.

Incremental strategy: outbox rows are coalesced per key, then each key is
RECONCILED against the current views (cur_obj_*/cur_link_*), so replaying rows
in any order/duplicated yields the same state as a rebuild. Known limit: a row
written with a future valid_from only appears once a later outbox row touches it
(the outbox has no time trigger); `rebuild()` always picks it up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg import sql
from pyoxigraph import Literal, NamedNode, Quad, Store

from engine.db.bitemporal import link_table, obj_table
from . import iri
from .iri import G_ASSERTED, G_META, RDF_TYPE, SYNC_SUBJ, LAST_SEQ, class_iri, link_iri, obj_iri


@dataclass
class SyncConfig:
    namespace: str = iri.DEFAULT_NS
    object_types: tuple[str, ...] = ("Supplier", "Port", "Warehouse", "Shipment", "Carrier")
    link_types: tuple[str, ...] = ("ships_via", "supplies", "operated_by")
    ontology_ttl: Path | None = Path(__file__).resolve().parents[2] / "build" / "ontology.ttl"

    @classmethod
    def from_yaml(cls, path) -> "SyncConfig":
        from engine.compiler import compile_ontology
        m = compile_ontology(path).model
        return cls(m.namespace, tuple(m.object_types), tuple(m.link_types))


def open_store(path: str | Path | None = None, cfg: SyncConfig | None = None) -> Store:
    """On-disk store at `path` (configurable) or in-memory if None. Loads the ontology graph once."""
    cfg = cfg or SyncConfig()
    st = Store(str(path)) if path else Store()
    if cfg.ontology_ttl and not any(True for _ in st.quads_for_pattern(None, None, None, iri.G_ONTOLOGY)):
        st.bulk_load(path=str(cfg.ontology_ttl), format=_TTL, base_iri=None, to_graph=iri.G_ONTOLOGY)
    return st


from pyoxigraph import RdfFormat as _RF  # noqa: E402
_TTL = _RF.TURTLE


def get_last_seq(st: Store) -> int:
    for q in st.quads_for_pattern(SYNC_SUBJ, LAST_SEQ, None, G_META):
        return int(q.object.value)
    return 0


def _set_last_seq(st: Store, seq: int) -> None:
    for q in list(st.quads_for_pattern(SYNC_SUBJ, LAST_SEQ, None, G_META)):
        st.remove(q)
    st.add(Quad(SYNC_SUBJ, LAST_SEQ, Literal(str(seq), datatype=NamedNode("http://www.w3.org/2001/XMLSchema#integer")), G_META))


def _chunks(it, n):
    it = iter(it)
    while c := list(islice(it, n)):
        yield c


def consume_outbox(conn: psycopg.Connection, st: Store, cfg: SyncConfig | None = None,
                   batch: int = 50_000) -> dict:
    """Apply outbox rows with seq > last applied. Returns stats. Idempotent."""
    cfg = cfg or SyncConfig()
    stats = {"rows": 0, "obj_keys": 0, "link_keys": 0, "added": 0, "removed": 0, "last_seq": get_last_seq(st)}
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT seq, kind, type, id, src, dst FROM graph_outbox WHERE seq > %s "
                        "ORDER BY seq LIMIT %s", [stats["last_seq"], batch])
            rows = cur.fetchall()
        conn.rollback()  # end read txn
        if not rows:
            return stats
        objs: dict[str, set] = {}
        links: dict[str, set] = {}
        for _, kind, typ, id_, src, dst in rows:
            if kind == "obj":
                objs.setdefault(typ, set()).add(id_)
            else:
                links.setdefault(typ, set()).add((src, dst))
        # Read current state for all touched keys in ONE snapshot, then apply.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        try:
            for typ, ids in objs.items():
                cur_ids = set()
                for c in _chunks(ids, 20_000):
                    cur_ids |= {r[0] for r in conn.execute(
                        sql.SQL("SELECT id FROM {} WHERE id = ANY(%s)").format(
                            sql.Identifier("cur_" + obj_table(typ))), [c]).fetchall()}
                cls = class_iri(cfg.namespace, typ)
                for id_ in ids:
                    s = obj_iri(cfg.namespace, id_)
                    q = Quad(s, RDF_TYPE, cls, G_ASSERTED)
                    if id_ in cur_ids:
                        if q not in st:
                            st.add(q); stats["added"] += 1
                    elif q in st:
                        st.remove(q); stats["removed"] += 1
            for typ, pairs in links.items():
                cur_pairs = set()
                for c in _chunks(pairs, 20_000):
                    srcs = [p[0] for p in c]; dsts = [p[1] for p in c]
                    cur_pairs |= set(conn.execute(
                        sql.SQL("SELECT l.src, l.dst FROM {} l JOIN unnest(%s::uuid[], %s::uuid[]) k(s,d) "
                                "ON l.src = k.s AND l.dst = k.d").format(
                            sql.Identifier("cur_" + link_table(typ))), [srcs, dsts]).fetchall())
                p_iri = link_iri(cfg.namespace, typ)
                for s_, d_ in pairs:
                    q = Quad(obj_iri(cfg.namespace, s_), p_iri, obj_iri(cfg.namespace, d_), G_ASSERTED)
                    if (s_, d_) in cur_pairs:
                        if q not in st:
                            st.add(q); stats["added"] += 1
                    elif q in st:
                        st.remove(q); stats["removed"] += 1
        finally:
            conn.rollback()
        stats["rows"] += len(rows)
        stats["obj_keys"] += sum(len(v) for v in objs.values())
        stats["link_keys"] += sum(len(v) for v in links.values())
        stats["last_seq"] = rows[-1][0]
        _set_last_seq(st, stats["last_seq"])


def _current_quads(conn, cfg: SyncConfig) -> Iterable[Quad]:
    for typ in cfg.object_types:
        cls = class_iri(cfg.namespace, typ)
        with conn.cursor(name=f"c_{typ}") as cur:
            cur.execute(sql.SQL("SELECT DISTINCT id FROM {}").format(sql.Identifier("cur_" + obj_table(typ))))
            for (id_,) in cur:
                yield Quad(obj_iri(cfg.namespace, id_), RDF_TYPE, cls, G_ASSERTED)
    for typ in cfg.link_types:
        p = link_iri(cfg.namespace, typ)
        with conn.cursor(name=f"l_{typ}") as cur:
            cur.execute(sql.SQL("SELECT DISTINCT src, dst FROM {}").format(sql.Identifier("cur_" + link_table(typ))))
            for s, d in cur:
                yield Quad(obj_iri(cfg.namespace, s), p, obj_iri(cfg.namespace, d), G_ASSERTED)


def rebuild(conn: psycopg.Connection, st: Store, cfg: SyncConfig | None = None) -> dict:
    """Drop asserted+inferred graphs and reload from the current views (one snapshot)."""
    cfg = cfg or SyncConfig()
    conn.rollback()
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    try:
        seq = conn.execute("SELECT coalesce(max(seq),0) FROM graph_outbox").fetchone()[0]
        st.clear_graph(G_ASSERTED)
        st.clear_graph(iri.G_INFERRED)
        n = 0
        for chunk in _chunks(_current_quads(conn, cfg), 200_000):
            st.bulk_extend(chunk); n += len(chunk)
    finally:
        conn.rollback()
    _set_last_seq(st, seq)
    return {"quads": n, "last_seq": seq}


def snapshot(st: Store, graph: NamedNode = G_ASSERTED) -> set[tuple[str, str, str]]:
    """Comparable (s,p,o) string set for a graph."""
    return {(q.subject.value, q.predicate.value, q.object.value) for q in st.quads_for_pattern(None, None, None, graph)}
