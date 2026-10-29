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
