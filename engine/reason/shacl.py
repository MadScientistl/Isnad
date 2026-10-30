"""pySHACL runner on a changed subgraph.

The Oxigraph store only holds ids/types/edges, so property shapes (datatype, minCount on
literals) can only be checked if properties are supplied: pass `props_loader` (see
`pg_props_loader`) to hydrate the extracted subgraph from Postgres. Without it, only
edge/type-based constraints are meaningful (property minCount shapes will report
violations; filter or hydrate).

Scenarios (Phase 5): `validate_changed(graph, ids, ns, props_loader, scenario=ctx)` validates the
neighbourhood in the scenario's own store; `graph` is then the scenario's `ScenarioGraph`
(`engine.sync.scenario_graph.get_store`), never the main store, and
`pg_props_loader(conn, types, ns, scenario=ctx)` hydrates from the scenario-effective `eff_obj_*` views
(the connection must be scoped with the same scenario).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from pyoxigraph import Literal, NamedNode, Quad, RdfFormat, Store, serialize

from engine.sync import iri
from engine.db.principal import ScenarioCtx
from engine.sync.iri import G_ASSERTED, obj_iri

SHAPES = Path(__file__).resolve().parents[2] / "build" / "shapes.ttl"
XSD = "http://www.w3.org/2001/XMLSchema#"


@dataclass
class ShaclResult:
    conforms: bool
    violations: list[dict]
    seconds: float
    nodes: int
    triples: int
    phases: dict = field(default_factory=dict)

    @property
    def focus_nodes(self) -> set[str]:
        return {v["focus"] for v in self.violations}


def extract_subgraph(st: Store, ids: Iterable, ns: str = iri.DEFAULT_NS, graph=G_ASSERTED) -> list[Quad]:
    """SPARQL CONSTRUCT neighbourhood of `ids`: outgoing triples, types of their targets, incoming edges."""
    vals = " ".join(f"<{obj_iri(ns, i).value}>" for i in ids)
    g = f"<{graph.value}>"
    queries = [
        f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH {g} {{ VALUES ?s {{ {vals} }} ?s ?p ?o }} }}",
        f"CONSTRUCT {{ ?o a ?c }} WHERE {{ GRAPH {g} {{ VALUES ?s {{ {vals} }} ?s ?p ?o . ?o a ?c }} }}",
        f"CONSTRUCT {{ ?x ?q ?s }} WHERE {{ GRAPH {g} {{ VALUES ?s {{ {vals} }} ?x ?q ?s }} }}",
    ]
    out: dict = {}
    for q in queries:
        for t in st.query(q):
            out[(t.subject, t.predicate, t.object)] = Quad(t.subject, t.predicate, t.object)
    return list(out.values())


def _lit(v):
    if isinstance(v, bool):
        return Literal("true" if v else "false", datatype=NamedNode(XSD + "boolean"))
    if isinstance(v, int):
        return Literal(str(v), datatype=NamedNode(XSD + "integer"))
    if isinstance(v, float):
        return Literal(repr(v), datatype=NamedNode(XSD + "double"))
    if isinstance(v, datetime):
        return Literal(v.isoformat(), datatype=NamedNode(XSD + "dateTime"))
    return Literal(str(v))


def pg_props_loader(conn, types: Iterable[str], ns: str = iri.DEFAULT_NS,
                    scenario: ScenarioCtx | None = None) -> Callable:
    """Returns loader(ids)->[Quad]: literal properties of current rows for `ids` from cur_obj_<type>
    (or eff_obj_<type> inside a `scenario`; the connection's scenario GUC must match, and its
    transaction is left open so the scoped identity survives)."""
    from psycopg import sql
    from engine.db.bitemporal import obj_table, COMMON_COLS
    types = tuple(types)

    def load(ids):
        ids = list(ids)
        quads = []
        for t in types:
            if scenario is not None:
                got = conn.execute("SELECT current_setting('isnad.scenario', true)").fetchone()[0]
                if str(got or "") != str(scenario.id):
                    raise PermissionError("connection is not scoped to this scenario")
            cur = conn.execute(sql.SQL("SELECT * FROM {} WHERE id = ANY(%s)").format(
                sql.Identifier(("eff_" if scenario is not None else "cur_") + obj_table(t))), [ids])
            cols = [d.name for d in cur.description]
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                s = obj_iri(ns, r["id"])
                for c, v in r.items():
                    if c == "id" or c in COMMON_COLS or v is None:
                        continue
                    quads.append(Quad(s, NamedNode(ns + c), _lit(v)))
        if scenario is None:
            conn.rollback()
        return quads
    return load


@lru_cache(maxsize=4)
def _shapes(path: str):
    from rdflib import Graph
    g = Graph()
    g.parse(path, format="turtle")
    return g


def validate_changed(st: Store, changed_ids: Iterable, ns: str = iri.DEFAULT_NS,
                     props_loader: Callable | None = None, shapes_path: Path | str = SHAPES,
                     scenario: ScenarioCtx | None = None) -> ShaclResult:
    """Extract neighbourhood of changed ids, optionally hydrate, validate with pySHACL.

    With `scenario`, `st` must be the scenario's ScenarioGraph (its isolated store is validated);
    passing a plain Store/SyncPipeline store raises ValueError (never validate main by accident)."""
    from rdflib import Graph, Namespace, RDF
    import pyshacl
    sid = getattr(st, "scenario_id", 0)
    if scenario is not None:
        if sid != scenario.id:
            raise ValueError("scenario validation needs the ScenarioGraph of that scenario")
    elif sid:
        raise ValueError("a ScenarioGraph needs scenario=<its ScenarioCtx>")
    if sid:
        st = st.store
    changed_ids = list(changed_ids)
    t0 = time.perf_counter()
    quads = extract_subgraph(st, changed_ids, ns)
    if props_loader:
        nodes = {q.subject.value for q in quads if q.subject.value.startswith(f"{ns}obj/")}
        quads += props_loader([n.rsplit("/", 1)[1] for n in nodes])
    t1 = time.perf_counter()
    data = Graph()
    data.parse(data=serialize(quads, format=RdfFormat.N_TRIPLES).decode(), format="nt")
    t2 = time.perf_counter()
    conforms, rg, _ = pyshacl.validate(data, shacl_graph=_shapes(str(shapes_path)), inference="none")
    t3 = time.perf_counter()
    SH = Namespace("http://www.w3.org/ns/shacl#")
    viol = []
    for r in rg.subjects(RDF.type, SH.ValidationResult):
        viol.append({"focus": str(rg.value(r, SH.focusNode)), "path": str(rg.value(r, SH.resultPath)),
                     "component": str(rg.value(r, SH.sourceConstraintComponent)).rsplit("#", 1)[-1]})
    nodes_n = len({q.subject for q in quads})
    return ShaclResult(bool(conforms), viol, t3 - t0, nodes_n, len(data),
                       {"extract": t1 - t0, "to_rdflib": t2 - t1, "validate": t3 - t2})
