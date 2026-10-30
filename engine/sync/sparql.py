"""Read-only, permission-filtered SPARQL (Phase 3, D5).

Model
-----
* Only SELECT and ASK are accepted. UPDATE, CONSTRUCT, DESCRIBE, SERVICE, FROM / FROM NAMED
  and GRAPH are rejected for everyone. Non-admin additionally: no aggregates / GROUP BY (D5).
  Validation parses with rdflib's SPARQL 1.1 grammar (a parse failure rejects the query, fail
  closed) AND scans a comment/string-stripped copy lexically, so a parser discrepancy between
  rdflib and Oxigraph cannot smuggle SERVICE/FROM/GRAPH through.
* Queries run against the *default graph* = ontology + asserted + inferred (provenance is a
  property of the quad's graph, which is not addressable by queries; GRAPH is rejected, and the
  meta graph is never included).
* ADMIN: evaluated directly on the live store (everything visible).
* NON-ADMIN: evaluated on a throw-away in-memory store holding ONLY the principal's view:
  type triples of visible objects, asserted edges returned by link RLS (so link type readable
  and both endpoints visible), inferred edges passing `inferred_visible` (D6), inferred
  rdf:type triples of visible subjects, and the ontology. Hidden nodes/edges therefore do not
  exist for the query: property paths, OPTIONAL, UNION, sub-queries, FILTER NOT EXISTS, ASK
  etc. cannot reach, count or probe them. A final pass still drops any row containing an
  object IRI outside the visible set (defence in depth).
  Cost: the view is rebuilt per query (O(visible nodes + edges)); no cache, so there is no
  staleness or cross-role bleed.
* SERVICE: pyoxigraph 0.5 has no switch to disable federation (verified: a SERVICE query hits the
  remote endpoint), so SERVICE is blocked only by validation (rdflib algebra names + lexical scan
  after a real lexer). Network egress filtering is the recommended second layer.
* SCENARIOS (Phase 5): `run_query(graph, conn, is_admin, q, scenario=ctx)` where `graph` is the scenario's
  `ScenarioGraph` (NOT the main pipeline; a mismatch raises ValueError, so a scenario query can never
  read main's live store). Admin queries run on the scenario store; non-admin gets the usual per-query
  visible store built from `eff_*` relations, `inferred_support WHERE scenario_id = ctx.id` and the
  scenario's inferred graph. Aggregate rejection, timeout and row caps are unchanged.
* Limits: wall-clock `timeout_s` (the query runs in a worker thread; on timeout the caller gets
  `SparqlError('timeout')`; Oxigraph offers no cancellation, so a runaway query keeps burning a
  CPU in the abandoned daemon thread on its throw-away store until it ends. Known limitation)
  and `max_rows`.
"""
from __future__ import annotations

import re
import threading
import time
from itertools import islice
from uuid import UUID

from pyoxigraph import (BlankNode, Literal, NamedNode, Quad, QueryBoolean, QuerySolutions, Store)

from . import iri
from engine.db.principal import ScenarioCtx
from .visibility import all_visible_ids, edges_exist, inferred_visible, visible_edges  # noqa: F401
from .visibility import _check_scn


class SparqlError(ValueError):
    """Rejected or failed query. `.code` in {rejected, syntax, timeout, error}; API: 400/408."""

    def __init__(self, msg: str, code: str = "rejected"):
        super().__init__(msg)
        self.code = code


_AGG_WORDS = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|SAMPLE|GROUP_CONCAT|GROUP\s+BY|HAVING)\b", re.I)
_ALWAYS_BAD = re.compile(r"\b(SERVICE|FROM|GRAPH|LOAD|CLEAR|DROP|CREATE|ADD|MOVE|COPY|INSERT|DELETE|WITH|USING|"
                         r"CONSTRUCT|DESCRIBE)\b", re.I)


_LEX = re.compile(
    r'''(?P<ls>"""(?:(?:"|"")?(?:[^"\\]|\\.))*"""|\'\'\'(?:(?:\'|\'\')?(?:[^\'\\]|\\.))*\'\'\')'''
    r'''|(?P<ss>"(?:[^"\\\n\r]|\\.)*"|\'(?:[^\'\\\n\r]|\\.)*\')'''
    r"|(?P<iri><[^<>\"{}|^`\\\x00-\x20]*>)|(?P<cm>#)", re.S)


def _strip(q: str) -> str:
    """Single-pass SPARQL lexer: blank out string literals (short and long, with escapes) and IRIREFs
    left to right, so a quote inside a comment can never open a string. A `#` outside a string/IRI
    is a comment start: comments are rejected outright (no parser-discrepancy surface)."""
    out, i = [], 0
    while i < len(q):
        m = _LEX.search(q, i)
        if m is None:
            out.append(q[i:])
            break
        out.append(q[i:m.start()])
        if m.lastgroup == "cm":
            raise SparqlError("comments are not allowed in queries")
        out.append(" ")
        i = m.end()
    q = "".join(out)
    q = re.sub(r"[?$]\w+", " ", q)
    q = re.sub(r"[\w.-]*:[\w.%:-]*", " ", q)
    return q


def validate(query: str, *, is_admin: bool) -> str:
    """Return 'select' or 'ask' or raise SparqlError. Pure (no store access)."""
    if not isinstance(query, str) or not query.strip():
        raise SparqlError("empty query", "syntax")
    if len(query) > 20_000:
        raise SparqlError("query too long", "rejected")
    from rdflib.plugins.sparql import parser, algebra
    try:
        parsed = parser.parseQuery(query)
        alg = algebra.translateQuery(parsed)
    except Exception as e:  # noqa: BLE001 - includes updates (not a query), syntax errors
        raise SparqlError(f"not a valid SPARQL 1.1 SELECT/ASK query", "syntax")
    kind = alg.algebra.name
    if kind not in ("SelectQuery", "AskQuery"):
        raise SparqlError(f"only SELECT and ASK are allowed (got {kind})")
    names: set[str] = set()

    def visit(node):
        n = getattr(node, "name", None)
        if isinstance(n, str):
            names.add(n)

    algebra.traverse(alg.algebra, visitPost=visit)
    for bad in ("ServiceGraphPattern", "Service", "Graph", "GraphGraphPattern"):
        if bad in names:
            raise SparqlError("SERVICE / GRAPH are not allowed")
    # dataset clauses (FROM / FROM NAMED) live in the parse tree
    if _has_key(parsed, "datasetClause"):
        raise SparqlError("FROM / FROM NAMED are not allowed")
    flat = _strip(query)
    m = _ALWAYS_BAD.search(flat)
    if m:
        raise SparqlError(f"{m.group(0).upper()} is not allowed")
    if not is_admin:
        if any(n.startswith("Aggregate_") or n in ("Group", "AggregateJoin") for n in names) or _AGG_WORDS.search(flat):
            raise SparqlError("aggregates / GROUP BY are not allowed for this role")
    return "select" if kind == "SelectQuery" else "ask"


def _has_key(node, key: str) -> bool:
    from pyparsing import ParseResults
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, ParseResults):
            if key in x:
                return True
            stack.extend(list(x))
            try:
                stack.extend(x.asDict().values())
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(x, dict):
            if key in x:
                return True
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x)
    return False


# --------------------------------------------------------------------------- the principal's view
def _require_graph(pipeline, scenario) -> None:
    """In a scenario `pipeline` must be that scenario's ScenarioGraph; outside, never a ScenarioGraph."""
    sid = getattr(pipeline, "scenario_id", 0)
    if scenario is None:
        if sid:
            raise ValueError("a ScenarioGraph needs scenario=<its ScenarioCtx>")
    elif sid != scenario.id:
        raise ValueError("scenario queries need the ScenarioGraph of that scenario (engine.sync.scenario_graph.get_store)")


def build_visible_store(pipeline, conn, *, is_admin: bool = False,
                        scenario: ScenarioCtx | None = None) -> tuple[Store, set[str]]:
    """In-memory store with only what `conn`'s principal may see. Returns (store, visible object IRIs).

    With `scenario`, `pipeline` is the scenario's ScenarioGraph and every relation is scenario-effective."""
    _require_graph(pipeline, scenario)
    _check_scn(conn, scenario)
    cfg = pipeline.cfg
    ns = cfg.namespace
    pre = f"{ns}obj/"
    out = Store()
    quads: list[Quad] = []
    vis_ids: set[UUID] = set()
    for typ, ids in all_visible_ids(conn, cfg=cfg, scenario=scenario).items():
        cls = iri.class_iri(ns, typ)
        for i in ids:
            vis_ids.add(i)
            quads.append(Quad(iri.obj_iri(ns, i), iri.RDF_TYPE, cls))
    from engine.db import bitemporal as bt
    from psycopg import sql
    for l in cfg.link_types:
        p = iri.link_iri(ns, l)
        for s, d in conn.execute(sql.SQL("SELECT DISTINCT src, dst FROM {}").format(
                sql.Identifier(("eff_" if scenario is not None else "cur_") + bt.link_table(l)))).fetchall():
            if s in vis_ids and d in vis_ids:
                quads.append(Quad(iri.obj_iri(ns, s), p, iri.obj_iri(ns, d)))
    # inferred graph: instance edges via D6, instance rdf:type of visible subjects as-is
    cand, type_quads = [], []
    nsl = len(ns)
    with pipeline._lock:
        inferred = list(pipeline.store.quads_for_pattern(None, None, None, iri.G_INFERRED))
        onto = list(pipeline.store.quads_for_pattern(None, None, None, iri.G_ONTOLOGY))
    for q in inferred:
        if not (isinstance(q.subject, NamedNode) and q.subject.value.startswith(pre)):
            continue
        sid = UUID(q.subject.value[len(pre):])
        if sid not in vis_ids:
            continue
        if q.predicate == iri.RDF_TYPE:
            type_quads.append(Quad(q.subject, q.predicate, q.object))
        elif isinstance(q.object, NamedNode) and q.object.value.startswith(pre) \
                and q.predicate.value.startswith(ns):
            cand.append((sid, q.predicate.value[nsl:], UUID(q.object.value[len(pre):])))
    quads += type_quads
    ok = inferred_visible(conn, [c for c in cand if c[2] in vis_ids], is_admin=is_admin, cfg=cfg, scenario=scenario)
    for s, l, d in ok:
        quads.append(Quad(iri.obj_iri(ns, s), iri.link_iri(ns, l), iri.obj_iri(ns, d)))
    quads += [Quad(q.subject, q.predicate, q.object) for q in onto]
    out.bulk_extend(quads)
    return out, {iri.obj_iri(ns, i).value for i in vis_ids}


# --------------------------------------------------------------------------- execution
def _term_json(t) -> dict | None:
    if t is None:
        return None
    if isinstance(t, NamedNode):
        return {"type": "uri", "value": t.value}
    if isinstance(t, BlankNode):
        return {"type": "bnode", "value": t.value}
    if isinstance(t, Literal):
        d = {"type": "literal", "value": t.value}
        if t.language:
            d["lang"] = t.language
        else:
            d["datatype"] = t.datatype.value
        return d
    return {"type": "triple", "value": str(t)}


def run_query(pipeline, conn, principal_is_admin: bool, query: str, *, timeout_s: float = 5.0,
              max_rows: int = 1000, scenario: ScenarioCtx | None = None) -> dict:
    """Run a validated SELECT/ASK. `conn` must be inside `scoped(...)` for the acting principal.
    With `scenario`, `pipeline` is the scenario's ScenarioGraph and `conn` is scoped with that scenario.

    Returns `{"vars": [...], "rows": [{var: {type, value[, datatype|lang]}}], "truncated": bool}`
    (unbound variables are omitted from a row, as in SPARQL JSON results); ASK returns
    `{"vars": [], "rows": [], "boolean": bool, "truncated": False}`. Raises SparqlError.
    """
    kind = validate(query, is_admin=principal_is_admin)
    _require_graph(pipeline, scenario)
    _check_scn(conn, scenario)
    deadline = time.monotonic() + timeout_s
    if principal_is_admin:
        store, pre = pipeline.store, None
        graphs = [iri.G_ONTOLOGY, iri.G_ASSERTED, iri.G_INFERRED]
        kw = {"default_graph": graphs, "named_graphs": []}
    else:
        store, vis = build_visible_store(pipeline, conn, is_admin=False, scenario=scenario)
        kw = {}
        pre = (pipeline.cfg.namespace + "obj/", vis)
    remaining = max(0.05, deadline - time.monotonic())
    box: dict = {}

    def work():
        try:
            res = store.query(query, **kw)
            if isinstance(res, QueryBoolean):
                box["ask"] = bool(res)
            elif isinstance(res, QuerySolutions):
                box["vars"] = [v.value for v in res.variables]
                rows, hidden_drop = [], 0
                for sol in islice(res, max_rows * 4 + 1 if pre else max_rows + 1):
                    row = {v: sol[v] for v in box["vars"] if sol[v] is not None}
                    if pre and any(isinstance(t, NamedNode) and t.value.startswith(pre[0]) and t.value not in pre[1]
                                   for t in row.values()):
                        continue  # defence in depth: a hidden object IRI must never be returned
                    rows.append(row)
                    if len(rows) > max_rows:
                        break
                box["rows"] = rows
            else:  # CONSTRUCT/DESCRIBE slipped through validation
                box["error"] = SparqlError("only SELECT and ASK are allowed")
        except SyntaxError as e:
            box["error"] = SparqlError("syntax error", "syntax")
        except BaseException as e:  # noqa: BLE001
            box["error"] = SparqlError("query failed", "error")

    th = threading.Thread(target=work, daemon=True, name="sparql")
    th.start()
    th.join(remaining)
    if th.is_alive():
        raise SparqlError(f"query exceeded {timeout_s:g}s", "timeout")
    if "error" in box:
        raise box["error"]
    if "ask" in box:
        return {"vars": [], "rows": [], "boolean": box["ask"], "truncated": False}
    rows = box["rows"]
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {"vars": box["vars"],
            "rows": [{v: _term_json(t) for v, t in r.items()} for r in rows],
            "truncated": truncated}
