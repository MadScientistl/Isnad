"""KPIs of the supply-chain world, computed under the caller's row-level security (Phase 5).

``compute(conn, principal, scenario_id=0, as_of_valid=None) -> {value_at_risk, delay_hours_total,
shipments_delayed, shipments_in_transit, ports_closed, invoices_flagged, extra_cost, exposure_shipments,
exposure_suppliers}`` and ``series(conn, principal, scenario_id, metric, from_, to, step_hours)``.

Every number is aggregated over the masked, RLS-filtered ``rd_*`` views (objects) and the RLS-filtered link
tables, so a role only counts what it can see: a hidden shipment is not in ``value_at_risk``, a masked column
sums to zero. Main reads ``rd_cur_obj_*`` / ``cur_link_*`` (the fast path); a scenario reads the overlay views
``rd_eff_obj_*`` / ``eff_link_*``. With ``as_of_valid`` the same overlay rule is applied explicitly on the
all-versions relations (``bitemporal._scn_where`` for a scenario).

Definitions (documented, deterministic):
  * ``value_at_risk``: sum(cargo_value) of non-arrived shipments that are delayed or ship via a closed port.
  * ``delay_hours_total``: sum(delay_hours) over shipments.
  * ``extra_cost``: sum(Shipment.extra_cost) + sum over suppliers of (price_index - 1) * 1000 (null = 1.0).
  * ``exposure_shipments``: non-arrived shipments with an asserted ``ships_via`` link to a closed port.
  * ``exposure_suppliers``: suppliers billing (``invoices_for`` + ``billed_by``) an exposed shipment, plus every
    supplier reachable from them through asserted ``supplies`` links (transitive closure over asserted edges,
    which equals what the reasoner would infer; inferred edges are not read). 0 for roles that cannot see invoices.

``compute`` / ``series`` run inline when the connection already carries the caller's identity (inside
``scoped(...)``, e.g. in the API or in the action engine's preview) and otherwise open their own scoped block.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from engine.db import bitemporal as bt
from engine.db.principal import OWNER, current_principal, current_scenario, scoped

KPI_KEYS = ("value_at_risk", "delay_hours_total", "shipments_delayed", "shipments_in_transit", "ports_closed",
            "invoices_flagged", "extra_cost", "exposure_shipments", "exposure_suppliers")
MAX_SERIES_POINTS = 500
_OPEN = "(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity')"


# ------------------------------------------------------------------ scope
@contextmanager
def _scope(conn, principal, sid: int):
    """Yield with the connection scoped to (principal, scenario sid). Inline when already scoped."""
    cur = current_principal(conn)
    if cur is not None or (principal is OWNER and sid and current_scenario(conn) == sid):
        if current_scenario(conn) != sid:
            raise ValueError(f"connection is scoped to scenario {current_scenario(conn) or 'main'}, not {sid}")
        if cur is not None and principal is not OWNER and (cur.role, cur.user) != (principal.role, principal.user):
            raise ValueError("principal does not match the connection's identity")
        yield
        return
    ctx = None
    if sid:
        from engine.db import scenario_admin
        ctx = scenario_admin.ctx_of(conn, sid)
    with conn.transaction():
        with scoped(conn, principal, ctx):
            if sid and not conn.execute("SELECT isnad_scenario_visible(%s)", [sid]).fetchone()[0]:
                raise LookupError(f"scenario {sid} not found")
            yield


# -------------------------------------------------------------- relations
class _Q:
    """Builds one SQL text; relations append their params in textual order (use inside one f-string)."""

    def __init__(self, sid: int, as_of, conn):
        self.sid, self.as_of, self.conn, self.params = sid, bt._utc(as_of) if as_of is not None else None, conn, []

    def obj(self, type_: str) -> str:
        t = bt.snake(type_)
        if self.as_of is None:
            return f"rd_cur_obj_{t}" if not self.sid else f"rd_eff_obj_{t}"
        return self._all(f"rd_obj_{t}", "obj", type_)

    def link(self, name: str) -> str:
        t = bt.snake(name)
        if self.as_of is None:
            return f"cur_link_{t}" if not self.sid else f"eff_link_{t}"
        return self._all(f"link_{t}", "link", name)

    def _all(self, table: str, kind: str, name: str) -> str:
        if self.sid:
            w, p = bt._scn_where(self.conn, kind, name, self.sid, self.as_of, None)
        else:
            w, p = f"scenario_id = 0 AND valid_during @> %s::timestamptz AND {_OPEN}", [self.as_of]
        self.params.extend(p)
        return f"(SELECT * FROM {table} WHERE {w})"


def _scalar(conn, q: _Q, text: str):
    v = conn.execute(text, q.params).fetchone()[0]
    return 0 if v is None else v


def counts(conn, type_: str, col: str, scenario_id: int = 0, as_of=None) -> dict[str, int]:
    """{value: count} of `col` over the visible objects of `type_` (caller must be scoped)."""
    q = _Q(scenario_id, as_of, conn)
    text = f"SELECT x.{col}, count(*) FROM {q.obj(type_)} x GROUP BY x.{col}"
    return {k: int(c) for k, c in conn.execute(text, q.params).fetchall()}


def open_events(conn, scenario_id: int = 0, as_of=None) -> int:
    q = _Q(scenario_id, as_of, conn)
    return int(_scalar(conn, q, f"SELECT count(*) FROM {q.obj('Event')} x WHERE x.ended IS NULL"))


# ---------------------------------------------------------------- metrics
def _status_count(type_, status):
    def fn(conn, sid, as_of):
        q = _Q(sid, as_of, conn)
        text = f"SELECT count(*) FROM {q.obj(type_)} x WHERE x.status = %s"
        q.params.append(status)
        return int(_scalar(conn, q, text))
    return fn


def _value_at_risk(conn, sid, as_of):
    q = _Q(sid, as_of, conn)
    text = (f"SELECT coalesce(sum(s.cargo_value), 0)::float8 FROM {q.obj('Shipment')} s "
            f"WHERE s.status IS DISTINCT FROM 'arrived' AND (s.status = 'delayed' OR EXISTS ("
            f"SELECT 1 FROM {q.link('ships_via')} l JOIN {q.obj('Port')} p ON p.id = l.dst "
            f"WHERE l.src = s.id AND p.status = 'closed'))")
    return float(_scalar(conn, q, text))


def _delay_hours(conn, sid, as_of):
    q = _Q(sid, as_of, conn)
    return float(_scalar(conn, q, f"SELECT coalesce(sum(delay_hours), 0)::float8 FROM {q.obj('Shipment')} x"))


def _extra_cost(conn, sid, as_of):
    q = _Q(sid, as_of, conn)
    text = (f"SELECT (SELECT coalesce(sum(extra_cost), 0) FROM {q.obj('Shipment')} s) + "
            f"(SELECT coalesce(sum((coalesce(price_index, 1.0) - 1.0) * 1000), 0) FROM {q.obj('Supplier')} u)")
    return float(_scalar(conn, q, text))


def _exposed(q: _Q) -> str:
    return (f"SELECT s.id FROM {q.obj('Shipment')} s WHERE s.status IS DISTINCT FROM 'arrived' AND EXISTS ("
            f"SELECT 1 FROM {q.link('ships_via')} l JOIN {q.obj('Port')} p ON p.id = l.dst "
            f"WHERE l.src = s.id AND p.status = 'closed')")


def _exposure_shipments(conn, sid, as_of):
    q = _Q(sid, as_of, conn)
    return int(_scalar(conn, q, f"SELECT count(*) FROM ({_exposed(q)}) e"))


def _exposure_suppliers(conn, sid, as_of):
    q = _Q(sid, as_of, conn)
    text = (f"WITH RECURSIVE exposed AS ({_exposed(q)}), "
            f"billed AS (SELECT DISTINCT b.dst AS sup FROM {q.link('invoices_for')} i "
            f"JOIN {q.link('billed_by')} b ON b.src = i.src WHERE i.dst IN (SELECT id FROM exposed)), "
            f"chain(sup) AS (SELECT sup FROM billed UNION SELECT l.dst FROM chain c "
            f"JOIN {q.link('supplies')} l ON l.src = c.sup) "
            f"SELECT count(*) FROM chain")
    return int(_scalar(conn, q, text))


METRICS = {
    "value_at_risk": _value_at_risk,
    "delay_hours_total": _delay_hours,
    "shipments_delayed": _status_count("Shipment", "delayed"),
    "shipments_in_transit": _status_count("Shipment", "in_transit"),
    "ports_closed": _status_count("Port", "closed"),
    "invoices_flagged": _status_count("Invoice", "flagged"),
    "extra_cost": _extra_cost,
    "exposure_shipments": _exposure_shipments,
    "exposure_suppliers": _exposure_suppliers,
}

# Built-in (hand-written SQL) metrics and what each one reads: (object types, link types, (Type, prop) pairs).
# A built-in is offered for an ontology only when all of it exists there; anything else comes from the
# ontology's `kpis:` block.
_NEEDS = {
    "value_at_risk": (("Shipment", "Port"), ("ships_via",), (("Shipment", "cargo_value"), ("Shipment", "status"),
                                                              ("Port", "status"))),
    "delay_hours_total": (("Shipment",), (), (("Shipment", "delay_hours"),)),
    "shipments_delayed": (("Shipment",), (), (("Shipment", "status"),)),
    "shipments_in_transit": (("Shipment",), (), (("Shipment", "status"),)),
    "ports_closed": (("Port",), (), (("Port", "status"),)),
    "invoices_flagged": (("Invoice",), (), (("Invoice", "status"),)),
    "extra_cost": (("Shipment", "Supplier"), (), (("Shipment", "extra_cost"), ("Supplier", "price_index"))),
    "exposure_shipments": (("Shipment", "Port"), ("ships_via",), (("Shipment", "status"), ("Port", "status"))),
    "exposure_suppliers": (("Shipment", "Port", "Invoice", "Supplier"),
                           ("ships_via", "invoices_for", "billed_by", "supplies"),
                           (("Shipment", "status"), ("Port", "status"))),
}


def _model(ontology):
    """The Ontology to compute for. None = the process default (legacy callers and tests)."""
    if ontology is None:
        from engine.actions.core import _load
        return _load(None)
    return getattr(ontology, "model", ontology)


def _builtin_ok(m, key) -> bool:
    types, links, props = _NEEDS[key]
    return (all(t in m.object_types for t in types) and all(l in m.link_types for l in links)
            and all(p in {x.name for x in m.object_types[t].properties} for t, p in props))


def metric_keys(ontology=None) -> tuple[str, ...]:
    """KPI keys available for the ontology: applicable built-ins (fixed order) then declared KPIs."""
    m = _model(ontology)
    keys = [k for k in KPI_KEYS if _builtin_ok(m, k)]
    keys += [k for k in getattr(m, "kpis", {}) if k not in keys]
    return tuple(keys)


def _declared(m, key):
    def fn(conn, sid, as_of):
        from engine.compiler import kpi_sql
        k = m.kpis[key]
        q = _Q(sid, as_of, conn)
        rel = q.obj(k.type)
        v = _scalar(conn, q, kpi_sql(m, k, rel))
        return int(v) if k.agg == "count" else float(v)
    return fn


def _metric_fn(m, key):
    # a built-in wins over a declared KPI of the same key (same definition, tested SQL)
    if key in METRICS and _builtin_ok(m, key):
        return METRICS[key]
    if key in getattr(m, "kpis", {}):
        return _declared(m, key)
    raise KeyError(key)


def compute(conn, principal, scenario_id: int = 0, as_of_valid: datetime | None = None, ontology=None) -> dict:
    """All KPIs for main (scenario_id 0) or a scenario, as the caller sees the world.
    `ontology` (Ontology or CompiledOntology) selects the KPI set; an ontology without KPIs gives ``{}``."""
    if type(scenario_id) is not int or scenario_id < 0:
        raise ValueError("scenario_id must be a non-negative int")
    m = _model(ontology)
    keys = metric_keys(m)
    if not keys:
        return {}
    with _scope(conn, principal, scenario_id):
        return {k: _metric_fn(m, k)(conn, scenario_id, as_of_valid) for k in keys}


def series(conn, principal, scenario_id: int, metric: str, from_: datetime, to: datetime,
           step_hours: float = 24.0, ontology=None) -> dict:
    """``{metric, points: [{t, v}]}``: one KPI evaluated at each step from `from_` to `to` (inclusive)."""
    m = _model(ontology)
    if metric not in metric_keys(m):
        raise ValueError(f"metric must be one of {list(metric_keys(m))}")
    if step_hours <= 0:
        raise ValueError("step_hours must be positive")
    from_, to = bt._utc(from_), bt._utc(to)
    if to < from_:
        raise ValueError("'from' must not be after 'to'")
    n = int((to - from_) / timedelta(hours=step_hours)) + 1
    if n > MAX_SERIES_POINTS:
        raise ValueError(f"too many points ({n} > {MAX_SERIES_POINTS}); raise step_hours")
    times = [from_ + timedelta(hours=step_hours * i) for i in range(n)]
    if times[-1] < to:
        times.append(to)
    fn = _metric_fn(m, metric)
    with _scope(conn, principal, scenario_id):
        pts = [{"t": t.astimezone(UTC).isoformat(), "v": fn(conn, scenario_id, t)} for t in times]
    return {"metric": metric, "points": pts}


def declared(conn, principal, scenario_id: int = 0, as_of_valid: datetime | None = None, ontology=None) -> dict:
    """Only the KPIs declared in the ontology's ``kpis:`` block (cheap aggregates; no hand-written metrics)."""
    m = _model(ontology)
    keys = tuple(getattr(m, "kpis", {}))
    if not keys:
        return {}
    with _scope(conn, principal, scenario_id):
        return {k: _declared(m, k)(conn, scenario_id, as_of_valid) for k in keys}


def object_counts(conn, ontology, types, scenario_id: int = 0, as_of=None) -> dict[str, int]:
    """{Type: visible object count} (caller must be scoped; masked/RLS views)."""
    out = {}
    for t in types:
        q = _Q(scenario_id, as_of, conn)
        out[t] = int(_scalar(conn, q, f"SELECT count(*) FROM {q.obj(t)} x"))
    return out


def enum_counts(conn, ontology, spec: dict[str, list[str]], scenario_id: int = 0, as_of=None) -> dict:
    """{Type: {prop: {value: n}}} for the enum properties listed in `spec` ({Type: [prop,...]}); every declared
    enum value is present (zero filled)."""
    m = _model(ontology)
    out: dict = {}
    for t, props in spec.items():
        ot = m.object_types[t]
        for pn in props:
            pr = next(x for x in ot.properties if x.name == pn)
            q = _Q(scenario_id, as_of, conn)
            got = {k: int(c) for k, c in conn.execute(
                f'SELECT x."{pn}", count(*) FROM {q.obj(t)} x GROUP BY x."{pn}"', q.params).fetchall()}
            out.setdefault(t, {})[pn] = {v: got.get(v, 0) for v in pr.enum}
    return out
