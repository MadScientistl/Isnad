"""Read-only function registry (Phase 4).

Each function takes ``(conn, **params)`` where ``conn`` is already inside a scoped transaction
(``engine.db.principal.scoped``), so every read goes through RLS and the masking views. Functions
only read (bitemporal ``get_object`` / ``list_objects`` / ``neighbours`` and
``engine.sync.visibility``); they never write. Role gating is declared in the YAML ``functions:``
section and enforced by the caller via ``Ontology.role_can_run_function``; ``run`` is the single
entry point.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID

from engine.db import bitemporal as bt
from engine.sync import visibility as vis


class FunctionError(ValueError):
    """Unknown function, bad parameters, or an object the caller cannot see."""


def _uuid(v, name: str) -> UUID:
    try:
        return v if isinstance(v, UUID) else UUID(str(v))
    except (ValueError, TypeError):
        raise FunctionError(f"parameter {name!r} must be a uuid") from None


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _need(conn, type_: str, id_: UUID, sid: int = 0) -> dict:
    """The object as the (scoped) caller sees it; `sid` > 0 reads scenario `sid` (the connection must be scoped
    to it). Hidden == missing."""
    o = bt.get_object(conn, type_, id_, scenario_id=sid)
    # inside a scenario the object policy of rd_* already evaluates the scenario-effective visibility (D4)
    if o is None or (not sid and id_ not in vis.visible_ids(conn, [id_])):
        raise FunctionError(f"{type_} {id_} not found")
    return o


def _visible_neighbours(conn, link: str, id_: UUID, direction: str, sid: int = 0,
                        other_type: str | None = None) -> list[UUID]:
    ids = bt.neighbours(conn, link, id_, direction, scenario_id=sid)
    if sid:  # scenario: keep the ids whose object (of the expected type) the caller can read
        return [i for i in ids if other_type is None or bt.get_object(conn, other_type, i, scenario_id=sid)]
    ok = vis.visible_ids(conn, ids) if ids else set()
    return [i for i in ids if i in ok]


def shipments_at_risk(conn, port, scenario_id: int = 0) -> dict:
    """Non-arrived shipments with a ships_via link to the port."""
    pid = _uuid(port, "port")
    p = _need(conn, "Port", pid, scenario_id)
    out = []
    for sid in _visible_neighbours(conn, "ships_via", pid, "in", scenario_id, "Shipment"):
        s = bt.get_object(conn, "Shipment", sid, scenario_id=scenario_id)
        if s is None or s.get("status") == "arrived":
            continue
        out.append({"id": str(sid), "ref": s.get("ref"), "status": s.get("status"), "eta": s.get("eta"),
                    "delay_hours": s.get("delay_hours"), "cargo_value": s.get("cargo_value")})
    out.sort(key=lambda r: (r["ref"] or "", r["id"]))
    return _jsonable({"port": {"id": str(pid), "name": p.get("name"), "status": p.get("status")},
                      "count": len(out), "shipments": out})


def invoice_anomaly_score(conn, invoice, scenario_id: int = 0) -> dict:
    """Deterministic score in [0, 1]: amount vs the supplier's other invoices, plus status/due hints."""
    iid = _uuid(invoice, "invoice")
    inv = _need(conn, "Invoice", iid, scenario_id)
    amount = float(inv.get("amount") or 0.0)
    score, reasons = 0.0, []
    peers: list[float] = []
    for sup in _visible_neighbours(conn, "billed_by", iid, "out", scenario_id, "Supplier"):
        for oid in _visible_neighbours(conn, "billed_by", sup, "in", scenario_id, "Invoice"):
            if oid == iid:
                continue
            o = bt.get_object(conn, "Invoice", oid, scenario_id=scenario_id)
            if o is not None and o.get("amount") is not None:
                peers.append(float(o["amount"]))
    if len(peers) >= 2:
        mean, sd = statistics.fmean(peers), statistics.pstdev(peers)
        if mean > 0:
            ratio = amount / mean
            if ratio >= 1.5:
                score += min(0.7, 0.3 + 0.2 * (ratio - 1.5))
                reasons.append(f"amount is {ratio:.1f}x the supplier's average ({mean:.2f}) over {len(peers)} other invoices")
            elif sd > 0 and (amount - mean) / sd >= 2:
                score += 0.3
                reasons.append(f"amount is {(amount - mean) / sd:.1f} standard deviations above the supplier's average")
    elif peers:
        reasons.append("only one other invoice from this supplier, no baseline")
    else:
        score += 0.1
        reasons.append("no other invoices from this supplier to compare against")
    if amount > 0 and amount == round(amount, -3) and amount >= 10000:
        score += 0.1
        reasons.append("round-number amount")
    if inv.get("status") == "flagged":
        score += 0.2
        reasons.append("already flagged" + (f": {inv['anomaly']}" if inv.get("anomaly") else ""))
    score = round(min(1.0, score), 3)
    return _jsonable({"invoice": str(iid), "ref": inv.get("ref"), "amount": amount, "score": score,
                      "peer_count": len(peers), "reasons": reasons})


def risk_factors(conn, shipment, scenario_id: int = 0) -> dict:
    """Delay, closed ports on the route, and open Events affecting those ports."""
    sid = _uuid(shipment, "shipment")
    s = _need(conn, "Shipment", sid, scenario_id)
    ports, events = [], []
    for pid in _visible_neighbours(conn, "ships_via", sid, "out", scenario_id, "Port"):
        p = bt.get_object(conn, "Port", pid, scenario_id=scenario_id)
        if p is None:
            continue
        ports.append({"id": str(pid), "name": p.get("name"), "status": p.get("status")})
        for eid in _visible_neighbours(conn, "affects", pid, "in", scenario_id, "Event"):
            e = bt.get_object(conn, "Event", eid, scenario_id=scenario_id)
            if e is not None and e.get("ended") is None:
                events.append({"id": str(eid), "kind": e.get("kind"), "severity": e.get("severity"),
                               "port": str(pid), "note": e.get("note")})
    closed = [p for p in ports if p["status"] == "closed"]
    factors = []
    if (s.get("delay_hours") or 0) > 0:
        factors.append(f"shipment delayed by {s['delay_hours']} h")
    if closed:
        factors.append("closed ports on route: " + ", ".join(str(p["name"]) for p in closed))
    if events:
        factors.append(f"{len(events)} open event(s) affecting route ports")
    return _jsonable({"shipment": str(sid), "ref": s.get("ref"), "status": s.get("status"),
                      "delay_hours": s.get("delay_hours") or 0, "ports": ports, "closed_ports": closed,
                      "open_events": events, "factors": factors})


FUNCTIONS: dict[str, Callable] = {
    "shipments_at_risk": shipments_at_risk,
    "invoice_anomaly_score": invoice_anomaly_score,
    "risk_factors": risk_factors,
}


def run(conn, name: str, params: dict | None = None, *, scenario_id: int = 0) -> dict:
    """Run a registered function. Raises FunctionError for unknown functions or bad parameters.
    Role gating is the caller's job (`Ontology.role_can_run_function`). `scenario_id` > 0 reads that scenario
    (the connection must be scoped to it, `scoped(conn, principal, ctx)`); the scenario id is never a caller
    parameter of the function itself."""
    fn = FUNCTIONS.get(name)
    if fn is None:
        raise FunctionError(f"unknown function {name!r}")
    try:
        return fn(conn, **{**(params or {}), "scenario_id": scenario_id})
    except TypeError as e:
        raise FunctionError(f"bad parameters for {name!r}: {e}") from None
