"""Declarative KPI aggregates (Phase 6): ``count(Type[, where <filter>])``, ``sum|avg|max|min(Type.prop[, where <filter>])``.

No raw SQL is accepted. The ``where`` part uses the row-filter language of ``exprs.parse_filter``; identifiers
are validated against the ontology and rendered by ``exprs.filter_to_sql`` (literals quoted). ``kpi_sql`` returns
one scalar SELECT over a relation chosen by the caller (a ``rd_cur_obj_<type>`` / ``rd_eff_obj_<type>`` view, so
row-level security and masking still apply); the aggregate is never NULL (``coalesce(.., 0)``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .exprs import ExprError, Node, filter_to_sql, parse_filter, validate_filter

KPI_AGGS = ("count", "sum", "avg", "max", "min")
KPI_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEAD = re.compile(r"^\s*(count|sum|avg|max|min)\(\s*([A-Z][A-Za-z0-9]*)(?:\.([a-z][a-z0-9_]*))?\s*"
                   r"(?:,\s*where\s+(.+?))?\s*\)\s*$", re.DOTALL)
_NUMERIC = ("int", "float")


@dataclass(frozen=True)
class KpiDef:
    key: str
    description: str
    expr: str
    agg: str
    type: str
    prop: str | None = None
    where: str | None = None
    where_ast: Node | None = field(default=None, compare=False, repr=False)


def parse_kpi(key: str, description: str, expr: str, onto) -> KpiDef:
    """Parse and validate one KPI; raises ExprError with a clear message."""
    if not isinstance(expr, str) or len(expr) > 2000:
        raise ExprError("expr must be a string of at most 2000 characters")
    m = _HEAD.match(expr)
    if not m:
        raise ExprError("expr must look like count(Type[, where <filter>]) or sum|avg|max|min(Type.prop[, where <filter>])")
    agg, tname, prop, where = m.groups()
    t = onto.object_types.get(tname)
    if t is None:
        raise ExprError(f"unknown object type '{tname}'")
    if agg == "count":
        if prop is not None:
            raise ExprError("count() takes a type, not a property")
    else:
        if prop is None:
            raise ExprError(f"{agg}() needs Type.prop")
        p = next((x for x in t.properties if x.name == prop), None)
        if p is None:
            raise ExprError(f"unknown property '{prop}' on {tname}")
        ok = _NUMERIC + (("timestamp",) if agg in ("max", "min") else ())
        if p.type not in ok:
            raise ExprError(f"{agg}({tname}.{prop}): property type {p.type} is not allowed (need {'/'.join(ok)})")
    ast = None
    if where is not None:
        ast = parse_filter(where)
        errs = validate_filter(ast, onto, tname)
        if errs:
            raise ExprError("; ".join(errs))
        if any(getattr(n, "name", None) == "region" for n in _params(ast)):
            raise ExprError("$region is not allowed in a KPI filter")
    return KpiDef(key, description, expr.strip(), agg, tname, prop, where, ast)


def _params(ast):
    from .exprs import Param, walk
    return [n for n in walk(ast) if isinstance(n, Param)]


def kpi_sql(onto, kpi: KpiDef, relation: str | None = None, alias: str = "x") -> str:
    """``SELECT <aggregate> FROM <relation> x WHERE <filter>``; `relation` defaults to ``rd_cur_obj_<snake>``."""
    from .model import snake_case
    rel = relation or f"rd_cur_obj_{snake_case(kpi.type)}"
    if kpi.agg == "count":
        sel = "count(*)"
    else:
        col = f'{alias}."{kpi.prop}"'
        ptype = next(p.type for p in onto.object_types[kpi.type].properties if p.name == kpi.prop)
        # numeric aggregates are never NULL (0 when no rows); max/min of a timestamp stay NULL on no rows
        sel = f"coalesce({kpi.agg}({col}), 0)::float8" if ptype in _NUMERIC else f"{kpi.agg}({col})"
    sql = f"SELECT {sel} FROM {rel} {alias}"
    if kpi.where_ast is not None:
        sql += " WHERE " + filter_to_sql(kpi.where_ast, onto, kpi.type, alias)
    return sql
