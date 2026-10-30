"""OWL 2 RL materialization over (ontology graph + asserted instance graph).

Inferred triples go to the SEPARATE named graph G_INFERRED, never to the asserted graph.
The graph is flagged in G_META (isInferredGraph, derivedBy, dataVersion=sync seq the
inference was computed from). Only triples about instances (subject is an `<ns>obj/`
IRI) are kept; the RL rules also re-derive ontology-level closure and axiomatic
owl:Thing/rdfs:Resource noise, which is dropped (counted in `dropped_non_instance`).
Per-triple provenance is graph-level for now: RDF-star is unverified in Oxigraph, so
rule-level provenance per edge is NOT implemented (documented limitation).
"""
from __future__ import annotations

import os
import tempfile
from collections import deque
import time
from dataclasses import dataclass, field

from pyoxigraph import Literal, NamedNode, Quad, RdfFormat, Store, BlankNode

from engine.sync import iri
from engine.sync.iri import G_ASSERTED, G_INFERRED, G_META, G_ONTOLOGY
from engine.sync.store import get_last_seq, _chunks


@dataclass
class MaterializeResult:
    backend: str
    seconds: float
    base_triples: int
    inferred_triples: int
    dropped_non_instance: int
    data_version: int
    phases: dict = field(default_factory=dict)


def _dump_base(st: Store, path: str) -> int:
    n = 0
    with open(path, "wb") as f:
        for g in (G_ONTOLOGY, G_ASSERTED):
            st.dump(f, RdfFormat.N_TRIPLES, from_graph=g)
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return n


def _term(t):
    """rdflib term -> pyoxigraph term."""
    from rdflib import URIRef, BNode, Literal as RL
    if isinstance(t, URIRef):
        return NamedNode(str(t))
    if isinstance(t, BNode):
        return BlankNode(str(t))
    if isinstance(t, RL):
        if t.language:
            return Literal(str(t), language=t.language)
        return Literal(str(t), datatype=NamedNode(str(t.datatype))) if t.datatype else Literal(str(t))
    raise TypeError(t)


def _write_inferred(st: Store, triples, ns: str, base_keys: set, backend: str, version: int, t0) -> tuple[int, int]:
    """triples: iterable of (s,p,o) rdflib terms (full closure). Returns (kept, dropped)."""
    obj_prefix = f"{ns}obj/"
    kept = dropped = 0
    new: set = set()
    for s, p, o in triples:
        key = (str(s), str(p), str(o))
        if key in base_keys:
            continue
        if not key[0].startswith(obj_prefix) or o is None or str(o).startswith("http://www.w3.org/2002/07/owl#Thing") \
                or key[2] == "http://www.w3.org/2000/01/rdf-schema#Resource":
            dropped += 1
            continue
        new.add(Quad(_term(s), _term(p), _term(o), G_INFERRED))
    kept = len(new)
    # Apply only the DELTA to the inferred graph (clear_graph on ~1e5+ quads is slow in RocksDB).
    old = set(st.quads_for_pattern(None, None, None, G_INFERRED))
    for q in old - new:
        st.remove(q)
    add = list(new - old)
    for i in range(0, len(add), 200_000):
        st.bulk_extend(add[i:i + 200_000])
    for q in list(st.quads_for_pattern(None, None, None, G_META)):
        if q.subject == G_INFERRED:
            st.remove(q)
    st.add(Quad(G_INFERRED, iri.INFERRED_FLAG, Literal("true", datatype=NamedNode("http://www.w3.org/2001/XMLSchema#boolean")), G_META))
    st.add(Quad(G_INFERRED, iri.DERIVED_BY, Literal(f"{backend} OWL2-RL"), G_META))
    st.add(Quad(G_INFERRED, iri.DATA_VERSION, Literal(str(version), datatype=NamedNode("http://www.w3.org/2001/XMLSchema#integer")), G_META))
    return kept, dropped


def _base_keys(path: str) -> set:
    # N-Triples lines parsed by pyoxigraph for consistent term strings
    from pyoxigraph import parse
    return {(q.subject.value, q.predicate.value, q.object.value)
            for q in parse(path=path, format=RdfFormat.N_TRIPLES)}


def materialize(st: Store, ns: str = iri.DEFAULT_NS, backend: str = "reasonable") -> MaterializeResult:
    """Run OWL 2 RL with `reasonable` (backend='reasonable') or owlrl; write the inferred graph."""
    if backend == "owlrl":
        return materialize_owlrl(st, ns)
    import reasonable
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as d:
        nt = os.path.join(d, "base.nt")
        nbase = _dump_base(st, nt)
        ttl = os.path.join(d, "base.ttl"); os.link(nt, ttl)
        t_dump = time.perf_counter()
        r = reasonable.PyReasoner()
        r.load_file(ttl)
        t_load = time.perf_counter()
        out = r.reason()
        t_reason = time.perf_counter()
        keys = _base_keys(nt)
        kept, dropped = _write_inferred(st, out, ns, keys, "reasonable", get_last_seq(st), t0)
    t1 = time.perf_counter()
    return MaterializeResult("reasonable", t1 - t0, nbase, kept, dropped, get_last_seq(st),
                             {"dump": t_dump - t0, "load": t_load - t_dump, "reason": t_reason - t_load,
                              "diff_write": t1 - t_reason})


def materialize_owlrl(st: Store, ns: str = iri.DEFAULT_NS) -> MaterializeResult:
    """Fallback / reference: owlrl DeductiveClosure(OWLRL_Semantics). Slow; use on small graphs."""
    import owlrl
    from rdflib import Graph
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as d:
        nt = os.path.join(d, "base.nt")
        nbase = _dump_base(st, nt)
        g = Graph()
        g.parse(nt, format="nt")
        base_keys = {(str(s), str(p), str(o)) for s, p, o in g}
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False,
                               datatype_axioms=False).expand(g)
        kept, dropped = _write_inferred(st, g, ns, base_keys, "owlrl", get_last_seq(st), t0)
    return MaterializeResult("owlrl", time.perf_counter() - t0, nbase, kept, dropped, get_last_seq(st))


# ----------------------------------------------------------------------------------------
# Derivation support for inferred instance edges (Phase 3, D6)
# ----------------------------------------------------------------------------------------
MAX_DERIVATIONS = 50          # derivations stored per inferred edge (shortest first)
_MAX_PARTIAL_PATHS = 50_000   # search budget per inferred edge

_sem_cache: dict = {}


def link_semantics(yaml_path=None) -> dict[str, tuple[str, bool]]:
    """{inferred link name -> (base link, reversed)} for every link name the reasoner can infer.

    A transitive link L maps to (L, False); an inverse name I of L maps to (I -> (L, True)).
    `support` paths in inferred_support are ordered along the BASE link (src of L first), so
    for a reversed name the path runs dst -> src of the inferred edge.
    """
    from engine.sync.store import DEFAULT_YAML
    from engine.compiler import compile_ontology
    p = str(yaml_path or DEFAULT_YAML)
    if p not in _sem_cache:
        m = compile_ontology(p).model
        sem: dict[str, tuple[str, bool]] = {}
        for l in m.link_types.values():
            if l.transitive:
                sem[l.name] = (l.name, False)
            if l.inverse:
                sem[l.inverse] = (l.name, True)
        _sem_cache[p] = sem
    return _sem_cache[p]


def enumerate_paths(adj: dict, radj_reach: set, s, d, cap: int = MAX_DERIVATIONS,
                    min_len: int = 1, budget: int = _MAX_PARTIAL_PATHS) -> list[tuple]:
    """Simple paths s -> d over `adj` (node -> sorted successors), shortest first, at most `cap`.
    `radj_reach` = nodes from which d is reachable (prunes dead ends)."""
    out: list[tuple] = []
    q = deque([(s,)])
    steps = 0
    while q and len(out) < cap and steps < budget:
        path = q.popleft()
        steps += 1
        for n in adj.get(path[-1], ()):
            if n == d:
                if len(path) >= min_len:
                    out.append(path + (n,))
                    if len(out) >= cap:
                        break
            elif n not in path and n in radj_reach:
                q.append(path + (n,))
    return out


def write_support(conn, st: Store, ns: str, yaml_path=None, max_derivations: int = MAX_DERIVATIONS,
                  data_version: int | None = None, scenario_id: int = 0) -> dict:
    """(Re)populate `inferred_support` from the ASSERTED graph for every inferred instance edge.

    Runs as OWNER (loader connection; `amarh_app` has SELECT only). For each inferred edge
    (s, L, d) of a transitive link or inverse name, stores up to `max_derivations` simple
    paths over the asserted base-link edges (shortest first) as separate derivations
    (`derivation_no` 0..), `support` = ordered node ids of the path in base-link direction.
    Derivations beyond the cap are dropped, which can only HIDE an edge that is visible
    through a dropped derivation (fail closed), never reveal one. The table is replaced in
    one transaction. Returns {edges, derivations, no_derivation}.

    `scenario_id` (Phase 5): 0 = main. Only rows of that scenario are deleted/inserted, so a scenario
    build never touches main's rows and vice versa.
    """
    if type(scenario_id) is not int or scenario_id < 0:
        raise ValueError("scenario_id must be a non-negative int")
    sem = link_semantics(yaml_path)
    pre = f"{ns}obj/"
    n = len(pre)
    if data_version is None:
        data_version = get_last_seq(st)
    adj_by_base: dict[str, dict] = {}
    radj_by_base: dict[str, dict] = {}

    def graph_of(base):
        if base not in adj_by_base:
            pred = NamedNode(f"{ns}{base}")
            adj: dict = {}
            radj: dict = {}
            for q in st.quads_for_pattern(None, pred, None, G_ASSERTED):
                if isinstance(q.subject, NamedNode) and isinstance(q.object, NamedNode) \
                        and q.subject.value.startswith(pre) and q.object.value.startswith(pre):
                    a, b = q.subject.value[n:], q.object.value[n:]
                    adj.setdefault(a, set()).add(b)
                    radj.setdefault(b, set()).add(a)
            adj_by_base[base] = {k: sorted(v) for k, v in adj.items()}
            radj_by_base[base] = radj
        return adj_by_base[base], radj_by_base[base]

    def reach_to(radj, d):
        seen, todo = {d}, [d]
        while todo:
            x = todo.pop()
            for y in radj.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        return seen

    rows, edges, none = [], 0, 0
    for link, (base, rev) in sem.items():
        adj, radj = graph_of(base)
        pred = NamedNode(f"{ns}{link}")
        for q in st.quads_for_pattern(None, pred, None, G_INFERRED):
            if not (isinstance(q.subject, NamedNode) and isinstance(q.object, NamedNode)
                    and q.subject.value.startswith(pre) and q.object.value.startswith(pre)):
                continue
            s, d = q.subject.value[n:], q.object.value[n:]
            edges += 1
            bs, bd = (d, s) if rev else (s, d)
            paths = enumerate_paths(adj, reach_to(radj, bd), bs, bd, max_derivations)
            if not paths:
                none += 1
            for i, p in enumerate(paths):
                rows.append((link, s, d, i, list(p), data_version))
    with conn.transaction():
        conn.execute("DELETE FROM inferred_support WHERE scenario_id = %s", [scenario_id])
        if rows:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO inferred_support (link, src, dst, derivation_no, support, data_version, "
                                "scenario_id) VALUES (%s, %s::uuid, %s::uuid, %s, %s::uuid[], %s, " + str(scenario_id) + ")",
                                rows)
    return {"edges": edges, "derivations": len(rows), "no_derivation": none}
