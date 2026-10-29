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


def _need(conn, type_: str, id_: UUID) -> dict:
    o = bt.get_object(conn, type_, id_)
    if o is None or id_ not in vis.visible_ids(conn, [id_]):
        raise FunctionError(f"{type_} {id_} not found")
    return o


def _visible_neighbours(conn, link: str, id_: UUID, direction: str) -> list[UUID]:
    ids = bt.neighbours(conn, link, id_, direction)
    ok = vis.visible_ids(conn, ids) if ids else set()
    return [i for i in ids if i in ok]


def shipments_at_risk(conn, port) -> dict:
    """Non-arrived shipments with a ships_via link to the port."""
    pid = _uuid(port, "port")
    p = _need(conn, "Port", pid)
    out = []
    for sid in _visible_neighbours(conn, "ships_via", pid, "in"):
        s = bt.get_object(conn, "Shipment", sid)
        if s is None or s.get("status") == "arrived":
            continue
        out.append({"id": str(sid), "ref": s.get("ref"), "status": s.get("status"), "eta": s.get("eta"),
                    "delay_hours": s.get("delay_hours"), "cargo_value": s.get("cargo_value")})
    out.sort(key=lambda r: (r["ref"] or "", r["id"]))
    return _jsonable({"port": {"id": str(pid), "name": p.get("name"), "status": p.get("status")},
                      "count": len(out), "shipments": out})


def invoice_anomaly_score(conn, invoice) -> dict:
    """Deterministic score in [0, 1]: amount vs the supplier's other invoices, plus status/due hints."""
    iid = _uuid(invoice, "invoice")
    inv = _need(conn, "Invoice", iid)
    amount = float(inv.get("amount") or 0.0)
    score, reasons = 0.0, []
    peers: list[float] = []
    for sup in _visible_neighbours(conn, "billed_by", iid, "out"):
        for oid in _visible_neighbours(conn, "billed_by", sup, "in"):
            if oid == iid:
                continue
            o = bt.get_object(conn, "Invoice", oid)
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


def risk_factors(conn, shipment) -> dict:
    """Delay, closed ports on the route, and open Events affecting those ports."""
    sid = _uuid(shipment, "shipment")
    s = _need(conn, "Shipment", sid)
    ports, events = [], []
    for pid in _visible_neighbours(conn, "ships_via", sid, "out"):
        p = bt.get_object(conn, "Port", pid)
        if p is None:
            continue
        ports.append({"id": str(pid), "name": p.get("name"), "status": p.get("status")})
        for eid in _visible_neighbours(conn, "affects", pid, "in"):
            e = bt.get_object(conn, "Event", eid)
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


def run(conn, name: str, params: dict | None = None) -> dict:
    """Run a registered function. Raises FunctionError for unknown functions or bad parameters.
    Role gating is the caller's job (`Ontology.role_can_run_function`)."""
    fn = FUNCTIONS.get(name)
    if fn is None:
        raise FunctionError(f"unknown function {name!r}")
    try:
        return fn(conn, **(params or {}))
    except TypeError as e:
        raise FunctionError(f"bad parameters for {name!r}: {e}") from None
