"""The nine agent tools: pydantic input model + implementation + result envelope.

Rules (see docs/contracts.md, Phase 4):
* every tool runs in its own `with conn.transaction(): with scoped(conn, human_principal):` (propose_action
  runs the engine's own transactions) and reads ONLY through permission-filtered engine code (bitemporal masked
  reads, engine.sync.visibility, engine.functions, engine.actions.propose);
* no parameter names a role, user, schema or table; ids/values are bound parameters only;
* hidden objects behave exactly like missing ones ("not found");
* results are masked, string-capped, size-bounded and wrapped in the untrusted envelope with provenance.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine import functions as fx
from engine.actions import CriteriaFailed, NotFound, PermissionDenied, propose
from engine.api.masking import mask_out
from engine.db import bitemporal as bt
from engine.db.principal import scoped
from engine.reason.materialize import link_semantics
from engine.sync import visibility as V

from .injection import scan
from .policy import PolicyDenied, allowed_actions, check_action, check_tool

log = logging.getLogger("amarh.agent")

MAX_STR = 300            # database strings are capped to this many characters
MAX_DATA_CHARS = 16_000  # serialised `data` budget per tool result (~4k tokens)
GEO = ("point", "linestring")
COMMON_COLS = {"valid_during", "recorded_during", "changed_by", "action_id", "scenario_id"}
# direction in which an impact flows along each link name (fwd = src -> dst)
FLOW = {"affects": "fwd", "ships_via": "rev", "supplies": "fwd", "supplied_by": "rev"}


class ToolError(Exception):
    """A problem the model should see (bad input, not found, ...)."""


@dataclass
class Result:
    data: Any
    truncated: bool = False
    proposal: dict | None = None


@dataclass
class ToolOutcome:
    ok: bool
    envelope: dict
    flags: list = field(default_factory=list)
    denied: dict | None = None      # {"kind": "tool"|"action"|"engine", "name", "reason"}
    proposal: dict | None = None
    error: str | None = None


# ------------------------------------------------------------------ input models
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(_In):
    property: str = Field(description="declared property name, or 'id'")
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "is_null"]
    value: Any = Field(default=None, description="literal (list for 'in'); ignored for is_null")


class QueryObjectsIn(_In):
    type: str = Field(description="object type name, e.g. Shipment")
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    order_by: str | None = None
    desc: bool = False
    limit: int = Field(20, ge=1, le=50)


class TraverseIn(_In):
    start_id: str = Field(description="uuid of the start object")
    depth: int = Field(1, ge=1, le=3)
    link_types: list[str] | None = None
    include_inferred: bool = False


class FindPathsIn(_In):
    from_id: str
    to_id: str
    max_hops: int = Field(4, ge=1, le=4)
    max_paths: int = Field(5, ge=1, le=10)
    link_types: list[str] | None = None
    include_inferred: bool = False


class ImpactIn(_In):
    start_id: str
    direction: Literal["downstream", "upstream"] = "downstream"
    depth: int = Field(2, ge=1, le=3)
    include_inferred: bool = True


class HistoryIn(_In):
    type: str
    id: str
    limit: int = Field(20, ge=1, le=50)


class AsOfIn(_In):
    type: str
    id: str
    valid_at: str = Field(description="ISO-8601 timestamp (valid time)")
    include_neighbours: bool = False


class DiffIn(_In):
    type: str
    id: str
    valid_at_a: str
    valid_at_b: str
    include_neighbours: bool = False


class RunFunctionIn(_In):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class Evidence(_In):
    ref: str = Field(description="ref of a fact returned by an earlier tool call of THIS run, e.g. Port:<uuid>")
    claim: str = Field(max_length=500)
    kind: Literal["asserted", "inferred"]
    tool_call_id: str = Field(description="the tool_call_id of the result that contained the ref")


class ProposeIn(_In):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence: list[Evidence] = Field(min_length=1, max_length=20)


# ------------------------------------------------------------------ small helpers
def _j(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_j(x) for x in v]
    if isinstance(v, (bytes, memoryview)):
        return None
    return v


def _uuid(v, what="id") -> UUID:
    try:
        return v if isinstance(v, UUID) else UUID(str(v))
    except (ValueError, TypeError, AttributeError):
        raise ToolError(f"{what} must be a uuid") from None


def _ts(s: str) -> datetime:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"not an ISO-8601 timestamp: {str(s)[:40]!r}") from None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _oref(type_: str, id_) -> str:
    return f"{type_}:{id_}"


def _eref(link: str, src, dst) -> str:
    return f"{link}:{src}>{dst}"


def _readable_type(ctx, name: str):
    ot = ctx.model.object_types.get(name)
    if ot is None or not ctx.role_def.can_read(name):
        raise ToolError(f"unknown object type {str(name)[:60]!r}")  # same text for unknown and unreadable
    return ot


def _masked(ctx, type_: str) -> set:
    a = ctx.role_def.types.get(type_)
    return set(a.mask) if a else set()


def _links(ctx, names):
    ok = [l for l in ctx.model.link_types if l in ctx.role_def.links]
    if names is None:
        return None
    bad = [n for n in names if n not in ok]
    if bad:
        raise ToolError(f"unknown link type {str(bad[0])[:60]!r}")
    return list(names)


def _props(row: dict) -> dict:
    return {k: _j(v) for k, v in row.items() if k not in COMMON_COLS and k != "id"}


def _obj_dict(type_: str, row: dict) -> dict:
    return {"ref": _oref(type_, row["id"]), "id": str(row["id"]), "type": type_, "props": _props(row)}


def _deadline(ctx) -> float:
    return time.monotonic() + max(1.0, min(10.0, ctx.remaining_s()))


def _is_admin(ctx) -> bool:
    return ctx.principal.role == "admin"


def _require_visible(conn, id_: UUID):
    if id_ not in V.visible_ids(conn, [id_]):
        raise ToolError("not found")


def _inferred_info(conn, edges) -> dict:
    """edge -> (rule, support) for inferred edges. Reads inferred_support under the caller's RLS, which only
    exposes derivations whose every node the caller can see (D6)."""
    edges = list(dict.fromkeys(edges))
    if not edges:
        return {}
    rows = conn.execute(
        "SELECT s.link, s.src, s.dst, s.derivation_no, s.support FROM inferred_support s "
        "JOIN unnest(%s::text[], %s::uuid[], %s::uuid[]) e(l, a, b) ON s.link = e.l AND s.src = e.a AND s.dst = e.b "
        "ORDER BY s.derivation_no", [[e[1] for e in edges], [e[0] for e in edges], [e[2] for e in edges]]).fetchall()
    sup = {}
    for l, s, d, _n, support in rows:
        sup.setdefault((s, l, d), [str(x) for x in support])
    sem = link_semantics()
    out = {}
    for e in edges:
        base, rev = sem.get(e[1], (e[1], False))
        rule = f"owl2rl:inverse({base})" if rev else f"owl2rl:transitive({base})"
        out[e] = (rule, sup.get(e, []))
    return out


def _edge_dicts(conn, asserted, inferred):
    info = _inferred_info(conn, inferred)
    out = [{"ref": _eref(l, s, d), "src": str(s), "link": l, "dst": str(d)} for s, l, d in asserted]
    for s, l, d in inferred:
        rule, support = info[(s, l, d)]
        out.append({"ref": _eref(l, s, d), "src": str(s), "link": l, "dst": str(d), "inferred": True,
                    "rule": rule, "support": support})
    return out


def _nodes(ctx, conn, ids, as_of=None):
    infos = V.node_infos(conn, list(ids), as_of=as_of, model=ctx.model)
    return [{"ref": _oref(i["type"], i["id"]), **i} for i in (infos.get(str(x)) for x in ids) if i]


# ------------------------------------------------------------------ tools
def query_objects(ctx, conn, a: QueryObjectsIn, call_id) -> Result:
    ot = _readable_type(ctx, a.type)
    masked = _masked(ctx, a.type)
    cols = {p.name: p for p in ot.properties if p.type not in GEO and p.name not in masked}
    ops = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
    where, params = [], []
    for f in a.filters:
        if f.property == "id":
            if f.op not in ("eq", "ne", "in"):
                raise ToolError("id supports eq, ne, in")
            vals = [_uuid(x) for x in f.value] if f.op == "in" and isinstance(f.value, list) else [_uuid(f.value)]
            if f.op == "in":
                where.append('"id" = ANY(%s)'); params.append(vals)
            else:
                where.append(f'"id" {ops[f.op]} %s'); params.append(vals[0])
            continue
        p = cols.get(f.property)
        if p is None:
            raise ToolError(f"unknown property {str(f.property)[:60]!r} for {a.type}")
        col = f'"{p.name}"'
        if f.op == "is_null":
            where.append(f"{col} IS NULL")
        elif f.op == "in":
            if not isinstance(f.value, list) or not f.value or len(f.value) > 50:
                raise ToolError("'in' needs a list of 1..50 values")
            where.append(f"{col} = ANY(%s)"); params.append([_coerce(p, x) for x in f.value])
        else:
            where.append(f"{col} {ops[f.op]} %s"); params.append(_coerce(p, f.value))
    ob = a.order_by
    if ob not in (None, "id") and ob not in cols:
        raise ToolError(f"unknown order_by property {str(ob)[:60]!r}")
    rows = bt.list_objects(conn, a.type, where=" AND ".join(where) or None, params=params, limit=a.limit + 1,
                           order_by=ob, desc=a.desc)
    more = len(rows) > a.limit
    return Result({"type": a.type, "objects": [_obj_dict(a.type, r) for r in rows[:a.limit]]}, truncated=more)


def _coerce(p, v):
    t = p.type
    bad = ToolError(f"value for {p.name!r} must be of type {t}")
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
            raise bad
        return int(v)
    if t == "float":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise bad
        return float(v)
    if t == "bool":
        if not isinstance(v, bool):
            raise bad
        return v
    if t == "timestamp":
        return _ts(v)
    if t == "enum":
        if v not in p.enum:
            raise ToolError(f"{p.name!r} must be one of {list(p.enum)}")
        return v
    if not isinstance(v, str):
        raise bad
    return v


def _bfs(ctx, conn, center, depth, include_inferred, link_types, as_of=None):
    try:
        return V.bfs(conn, center, depth, pipeline=ctx.pipeline, as_of=as_of, include_inferred=include_inferred,
                     link_types=link_types, max_nodes=100, max_fanout=25, is_admin=_is_admin(ctx),
                     deadline=_deadline(ctx))
    except V.NotVisible:
        raise ToolError("not found") from None


def traverse(ctx, conn, a: TraverseIn, call_id) -> Result:
    start = _uuid(a.start_id, "start_id")
    res = _bfs(ctx, conn, start, a.depth, a.include_inferred, _links(ctx, a.link_types))
    data = {"start": str(start), "nodes": _nodes(ctx, conn, res.order),
            "edges": _edge_dicts(conn, [(s, l, d) for s, l, d in res.edges], res.inferred),
            "inferred_available": res.inferred_available}
    return Result(data, truncated=res.truncated)


def find_paths(ctx, conn, a: FindPathsIn, call_id) -> Result:
    src, dst = _uuid(a.from_id, "from_id"), _uuid(a.to_id, "to_id")
    try:
        paths = V.find_paths(conn, src, dst, max_hops=a.max_hops, pipeline=ctx.pipeline,
                             include_inferred=a.include_inferred, link_types=_links(ctx, a.link_types),
                             max_paths=a.max_paths, is_admin=_is_admin(ctx), deadline=_deadline(ctx))
    except V.NotVisible:
        raise ToolError("not found") from None
    ids = list(dict.fromkeys(n for p in paths for n, _l, _d in p))
    asserted = set(V.visible_edges(conn, ids)) if ids else set()
    edges, out = {}, []
    for p in paths:
        hops = []
        for (prev, _l0, _d0), (node, link, dr) in zip(p, p[1:]):
            e = (prev, link, node) if dr == "out" else (node, link, prev)
            inf = e not in asserted
            edges[e] = inf
            hops.append({"edge": _eref(*(e[1], e[0], e[2])), "from": str(prev), "to": str(node),
                         "link": link, "inferred": inf})
        out.append({"nodes": [str(n) for n, _l, _d in p], "hops": hops})
    data = {"paths": out, "nodes": _nodes(ctx, conn, ids),
            "edges": _edge_dicts(conn, [e for e, i in edges.items() if not i], [e for e, i in edges.items() if i])}
    return Result(data, truncated=len(paths) >= a.max_paths)


def impact_analysis(ctx, conn, a: ImpactIn, call_id) -> Result:
    start = _uuid(a.start_id, "start_id")
    allowed = [l for l in ("supplies", "ships_via", "affects") if l in ctx.role_def.links]
    if not allowed:
        raise ToolError("no impact links are available to this role")
    res = _bfs(ctx, conn, start, a.depth, a.include_inferred, allowed)
    edges = [(s, l, d, False) for s, l, d in res.edges] + [(s, l, d, True) for s, l, d in res.inferred]
    edges.sort(key=lambda e: e[3])  # asserted edges win ties
    adj: dict = {}
    for s, l, d, inf in edges:
        flow = FLOW.get(l)
        if flow is None:
            continue
        fr, to = (s, d) if flow == "fwd" else (d, s)
        if a.direction == "upstream":
            fr, to = to, fr
        adj.setdefault(fr, []).append((to, (s, l, d), inf))
    hops, via, frontier = {start: 0}, {}, [start]
    for h in range(1, a.depth + 1):
        nxt = []
        for x in frontier:
            for to, e, inf in adj.get(x, []):
                if to not in hops:
                    hops[to], via[to] = h, (e, inf)
                    nxt.append(to)
        frontier = nxt
    impacted_ids = [i for i in hops if i != start]
    infos = {n["id"]: n for n in _nodes(ctx, conn, impacted_ids)}
    impacted = [{**infos[str(i)], "hops": hops[i], "via": _eref(via[i][0][1], via[i][0][0], via[i][0][2]),
                 "basis": "inferred" if via[i][1] else "asserted"} for i in impacted_ids if str(i) in infos]
    used = list(dict.fromkeys(via[i] for i in impacted_ids))
    data = {"start": str(start), "direction": a.direction, "impacted": impacted,
            "edges": _edge_dicts(conn, [e for e, inf in used if not inf], [e for e, inf in used if inf]),
            "note": "impact flows: affects src->dst, ships_via dst->src, supplies src->dst (inferred supplies is transitive)"}
    return Result(data, truncated=res.truncated)


def get_history(ctx, conn, a: HistoryIn, call_id) -> Result:
    _readable_type(ctx, a.type)
    id_ = _uuid(a.id)
    rows = bt.history(conn, a.type, id_)
    if not rows:
        raise ToolError("not found")
    vs = [{"valid_from": _j(r["valid_from"]), "valid_to": _j(r["valid_to"]),
           "recorded_from": _j(r["recorded_from"]), "recorded_to": _j(r["recorded_to"]),
           "changed_by": r.get("changed_by"), "props": _props({k: v for k, v in r.items() if k not in (
               "valid_from", "valid_to", "recorded_from", "recorded_to")})} for r in rows]
    return Result({"ref": _oref(a.type, id_), "versions": vs[-a.limit:]}, truncated=len(vs) > a.limit)


def _snapshot(ctx, conn, type_, id_, at, neighbours):
    row = bt.get_object(conn, type_, id_, as_of_valid=at)
    if row is None:
        return None, [], [], False
    if not neighbours:
        return row, [], [], False
    try:
        res = V.bfs(conn, id_, 1, as_of=at, include_inferred=False, max_nodes=100, max_fanout=25,
                    is_admin=_is_admin(ctx), deadline=_deadline(ctx))
    except V.NotVisible:
        return None, [], [], False
    return row, res.order, res.edges, res.truncated


def as_of(ctx, conn, a: AsOfIn, call_id) -> Result:
    _readable_type(ctx, a.type)
    id_, at = _uuid(a.id), _ts(a.valid_at)
    row, order, edges, trunc = _snapshot(ctx, conn, a.type, id_, at, a.include_neighbours)
    if row is None:
        raise ToolError("not found")
    data = {"valid_at": at.isoformat(), **_obj_dict(a.type, row)}
    if a.include_neighbours:
        data["neighbours"] = [n for n in _nodes(ctx, conn, order, as_of=at) if n["id"] != str(id_)]
        data["edges"] = _edge_dicts(conn, edges, [])
    return Result(data, truncated=trunc)


def diff(ctx, conn, a: DiffIn, call_id) -> Result:
    _readable_type(ctx, a.type)
    id_, ta, tb = _uuid(a.id), _ts(a.valid_at_a), _ts(a.valid_at_b)
    ra, oa, ea, xa = _snapshot(ctx, conn, a.type, id_, ta, a.include_neighbours)
    rb, ob, eb, xb = _snapshot(ctx, conn, a.type, id_, tb, a.include_neighbours)
    if ra is None and rb is None:
        raise ToolError("not found")
    pa, pb = _props(ra) if ra else {}, _props(rb) if rb else {}
    changed = {k: {"a": pa.get(k), "b": pb.get(k)} for k in sorted(set(pa) | set(pb)) if pa.get(k) != pb.get(k)}
    data = {"ref": _oref(a.type, id_), "a": ta.isoformat(), "b": tb.isoformat(),
            "exists_at_a": ra is not None, "exists_at_b": rb is not None, "changed": changed}
    if a.include_neighbours:
        sa, sb = set(ea), set(eb)
        data["links_added"] = _edge_dicts(conn, sorted(sb - sa, key=str), [])
        data["links_removed"] = _edge_dicts(conn, sorted(sa - sb, key=str), [])
        na, nb = set(oa), set(ob)
        data["neighbours_added"] = [n for n in _nodes(ctx, conn, [x for x in ob if x not in na], as_of=tb)]
        data["neighbours_removed"] = [n for n in _nodes(ctx, conn, [x for x in oa if x not in nb], as_of=ta)]
    return Result(data, truncated=xa or xb)


def run_function(ctx, conn, a: RunFunctionIn, call_id) -> Result:
    f = ctx.model.functions.get(a.name)
    if f is None or not ctx.model.role_can_run_function(ctx.principal.role, a.name):
        raise PolicyDenied("function", str(a.name)[:80], "function is not available to this role")
    specs = {s.name: s for s in f.param_specs()}
    extra = set(a.params) - set(specs)
    if extra:
        raise ToolError(f"unknown params for {a.name}: {sorted(map(str, extra))[:5]}")
    for k, s in specs.items():
        if s.required and a.params.get(k) is None:
            raise ToolError(f"missing required param {k!r}")
        if s.base == "uuid" and a.params.get(k) is not None:
            _uuid(a.params[k], k)
    try:
        out = _j(fx.run(conn, a.name, a.params))
    except fx.FunctionError as e:
        raise ToolError(str(e)) from None
    ids = sorted(_collect_uuids(out))
    objs = _nodes(ctx, conn, [UUID(i) for i in ids][:100]) if ids else []
    return Result({"function": a.name, "result": out, "objects": objs})


def _collect_uuids(x, acc=None):
    acc = set() if acc is None else acc
    if isinstance(x, str):
        try:
            acc.add(str(UUID(x)))
        except ValueError:
            pass
    elif isinstance(x, dict):
        for v in x.values():
            _collect_uuids(v, acc)
    elif isinstance(x, list):
        for v in x:
            _collect_uuids(v, acc)
    return acc


def _canon(v):
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, list):
        return [_canon(x) for x in v]
    if isinstance(v, str):
        try:
            return str(UUID(v))
        except ValueError:
            return v
    return v


def _check_evidence(ctx, items):
    for e in items:
        calls = ctx.seen.get(e.ref)
        if not calls or e.tool_call_id not in calls:
            raise ToolError(f"evidence ref {e.ref[:80]!r} was not returned by tool call {e.tool_call_id[:60]!r} "
                            "of this run; cite only facts you retrieved")
        if e.kind == "asserted" and e.ref in ctx.inferred_refs:
            raise ToolError(f"evidence ref {e.ref[:80]!r} is an inferred fact; mark it kind='inferred'")


def propose_action(ctx, conn, a: ProposeIn, call_id) -> Result:
    check_action(ctx, a.action)
    _check_evidence(ctx, a.evidence)
    action = ctx.model.actions[a.action]
    specs = {s.name: s for s in action.param_specs()}
    extra = set(a.params) - set(specs)
    if extra:
        raise ToolError(f"unknown params for {a.action}: {sorted(map(str, extra))[:5]}")
    for k, s in specs.items():
        v = a.params.get(k)
        if s.base == "uuid" and v is not None:
            try:
                canon = str(UUID(str(v)))
            except ValueError:
                raise ToolError(f"param {k!r}: not a valid uuid") from None
            if canon not in ctx.seen_ids:
                raise ToolError(f"param {k!r}: this id was not retrieved by this run; look the object up first "
                                "(ids cannot be invented, and optional id params must be omitted)")
    key = hashlib.sha256(json.dumps([ctx.run_id, a.action, _canon(a.params)], sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()
    ev = [e.model_dump() for e in a.evidence]
    pid = propose(conn, a.action, dict(a.params), ctx.agent_principal, a.rationale, valid_at=ctx.now(),
                  source="agent", run_id=ctx.run_id, tool_trace=list(ctx.trace), ontology=ctx.comp,
                  on_behalf_of=ctx.principal.user, evidence=ev, idempotency_key=key)
    with conn.transaction():
        with scoped(conn, ctx.principal):
            row = conn.execute("SELECT dry_run, expires_at FROM proposal WHERE id=%s", [pid]).fetchone()
    dry, exp = (row or (None, None))
    info = {"proposal_id": str(pid), "action": a.action, "params": _j(a.params)}
    data = {**info, "status": "pending_approval", "dry_run": _j(dry), "expires_at": _j(exp),
            "note": "Nothing was executed. A human with approval rights must approve this proposal."}
    return Result(data, proposal={**info, "expires_at": _j(exp)})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type
    impl: Callable
    own_tx: bool = False


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("query_objects", "List objects of one type with optional filters (equality/range on declared "
             "properties), ordering and a limit (max 50). Returns only what your role may see.",
             QueryObjectsIn, query_objects),
    ToolSpec("traverse", "Breadth-first expansion from an object over visible links (depth<=3, <=25 links per "
             "node, <=100 nodes). Optionally include inferred (reasoned) edges, which are flagged.",
             TraverseIn, traverse),
    ToolSpec("find_paths", "Shortest-first simple paths between two objects (hops<=4, paths<=10). Each hop is "
             "labelled asserted or inferred.", FindPathsIn, find_paths),
    ToolSpec("impact_analysis", "Follow downstream or upstream impact over supplies, ships_via and affects "
             "(depth<=3). Each impacted object says whether it was reached via an asserted or inferred edge.",
             ImpactIn, impact_analysis),
    ToolSpec("get_history", "All stored versions of an object (valid and recorded time), newest last.",
             HistoryIn, get_history),
    ToolSpec("as_of", "Read an object (optionally with its direct neighbours) as it was at a past valid time.",
             AsOfIn, as_of),
    ToolSpec("diff", "Compare an object (optionally its direct neighbourhood) between two valid times: changed "
             "properties and added/removed links.", DiffIn, diff),
    ToolSpec("run_function", "Run a registered read-only analysis function (see the system prompt for the list).",
             RunFunctionIn, run_function),
    ToolSpec("propose_action", "Propose an action for human approval. Needs a rationale and evidence items that "
             "cite refs returned by your earlier tool calls. Executes nothing.", ProposeIn, propose_action,
             own_tx=True),
]}


def _clean_schema(s):
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if k != "title"}
    if isinstance(s, list):
        return [_clean_schema(v) for v in s]
    return s


def tool_schemas(names) -> list[dict]:
    return [{"name": n, "description": TOOLS[n].description,
             "input_schema": _clean_schema(TOOLS[n].input_model.model_json_schema())} for n in names]


# ------------------------------------------------------------------ result finalisation
def _cap_strings(x):
    if isinstance(x, str):
        return x if len(x) <= MAX_STR else x[:MAX_STR] + "…"
    if isinstance(x, dict):
        return {k: _cap_strings(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_cap_strings(v) for v in x]
    return x


def _trim(x, n):
    if isinstance(x, list):
        return [_trim(v, n) for v in x[:n]]
    if isinstance(x, dict):
        return {k: _trim(v, n) for k, v in x.items()}
    return x


def _shrink(data):
    size = lambda d: len(json.dumps(d, default=str))
    if size(data) <= MAX_DATA_CHARS:
        return data, False
    for n in (50, 25, 12, 6, 3, 1):
        d = _trim(data, n)
        if size(d) <= MAX_DATA_CHARS:
            return d, True
    return {"note": "result too large; narrow the query"}, True


_U = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_REF_RE = re.compile(rf"[A-Za-z_]+:{_U}(>{_U})?")


def _walk_refs(x, out):
    """Facts = dicts whose "ref" has the canonical form (property values named "ref" are not facts)."""
    if isinstance(x, dict):
        if isinstance(x.get("ref"), str) and _REF_RE.fullmatch(x["ref"]):
            out.append(x)
        for k, v in x.items():
            if k != "props":   # property values are data, never facts (a property may be named "ref")
                _walk_refs(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk_refs(v, out)


def _finalise(ctx, data, truncated: bool, call_id: str):
    """Mask, cap, shrink, register the refs the model actually sees, and build the provenance."""
    data = _j(data)
    flags = scan(data)
    data = mask_out(ctx.model, ctx.principal, data)
    data, cut = _shrink(_cap_strings(data))
    facts: list = []
    _walk_refs(data, facts)
    asserted, inferred = [], []
    for f in facts:
        ref, inf = f["ref"], bool(f.get("inferred"))
        head, _, tail = ref.partition(":")
        if ">" in tail:
            a_, _, b_ = tail.partition(">")
            ctx.see_id(a_), ctx.see_id(b_)
            ctx.see(ref, call_id, inferred=inf)
        else:
            ctx.see(ref, call_id, inferred=inf, type_=head if len(tail) == 36 else None)
            if len(tail) == 36:
                ctx.see_id(tail, head)
        if inf:
            inferred.append({"edge": ref, "rule": f.get("rule"), "support": f.get("support", [])})
        elif ref not in asserted:
            asserted.append(ref)
    return data, {"asserted": asserted, "inferred": inferred}, truncated or cut, flags, facts


def _envelope(ok, data, prov, truncated, call_id, error=None):
    env = {"ok": ok, "data": data, "untrusted": True, "provenance": prov, "truncated": truncated,
           "tool_call_id": call_id}
    if error:
        env["error"] = error
    return env


def _err(call_id, msg, **kw) -> ToolOutcome:
    return ToolOutcome(False, _envelope(False, None, {"asserted": [], "inferred": []}, False, call_id, msg),
                       error=msg, **kw)


def safe_input(ctx, name, raw):
    """Tool input as stored in traces/audit: JSON-safe and masked (propose_action params)."""
    raw = _j(raw if isinstance(raw, dict) else {})
    return mask_out(ctx.model, ctx.principal, raw) if name == "propose_action" else raw


def run_tool(ctx, name: str, raw_input, call_id: str) -> ToolOutcome:
    """Execute one tool call as the HUMAN principal. Never raises."""
    entry = {"tool_call_id": call_id, "name": str(name)[:60], "input": None, "ok": None}
    ctx.trace.append(entry)
    try:
        check_tool(ctx, name)
        spec = TOOLS[name]
        raw = raw_input if isinstance(raw_input, dict) else {}
        entry["input"] = safe_input(ctx, name, raw)
        try:
            args = spec.input_model.model_validate(raw)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4])
            raise ToolError("invalid input: " + msg[:400]) from None
        conn = ctx.conn()
        if spec.own_tx:
            res = spec.impl(ctx, conn, args, call_id)
        else:
            with conn.transaction():
                with scoped(conn, ctx.principal):
                    res = spec.impl(ctx, conn, args, call_id)
        data, prov, trunc, flags, facts = _finalise(ctx, res.data, res.truncated, call_id)
        entry.update(ok=True, truncated=trunc, flags=flags, refs=[f["ref"] for f in facts][:30])
        prop = None
        if res.proposal:
            prop = mask_out(ctx.model, ctx.principal, res.proposal)
        return ToolOutcome(True, _envelope(True, data, prov, trunc, call_id), flags=flags, proposal=prop)
    except PolicyDenied as e:
        out = _err(call_id, e.reason, denied={"kind": e.kind, "name": e.name, "reason": e.reason})
    except ToolError as e:
        out = _err(call_id, str(e))
    except (V.NotVisible, NotFound):
        out = _err(call_id, "not found")
    except PermissionDenied as e:
        out = _err(call_id, f"permission denied: {e.detail}"[:300],
                   denied={"kind": "engine", "name": str(name), "reason": e.detail[:300]})
    except CriteriaFailed as e:
        out = _err(call_id, "criteria failed: " + "; ".join(f["message"] for f in e.failures)[:300])
    except TimeoutError:
        out = _err(call_id, "query exceeded its time limit; narrow it")
    except ValueError as e:
        out = _err(call_id, str(e)[:300])
    except psycopg.Error as e:
        log.warning("agent tool %s database error: %s", name, str(e).splitlines()[0] if str(e) else type(e))
        out = _err(call_id, "database error")
    except Exception:  # noqa: BLE001
        log.exception("agent tool %s failed", name)
        out = _err(call_id, "internal error")
    try:
        ctx.conn().rollback()
    except Exception:  # noqa: BLE001
        pass
    entry.update(ok=False, error=out.error)
    return out
