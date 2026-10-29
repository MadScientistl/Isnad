from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from engine.db import bitemporal as bt

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ONTOLOGY = ROOT / "ontology" / "supply_chain.yaml"

# Optional params accepted on top of the YAML registry (see package docstring).
INTERNAL_PARAMS: dict[str, dict[str, str]] = {
    "close_port": {"event_id": "uuid"},
    "issue_invoice": {"invoice_id": "uuid", "ref": "string"},
}

_STRIP = {"id", "valid_during", "recorded_during", "changed_by", "action_id", "scenario_id"}
_ontology_cache: dict[str, Any] = {}


@dataclass
class ActionResult:
    action_id: UUID
    changed: bool
    name: str = ""
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    audit_seq: int | None = None
    created: dict = field(default_factory=dict)  # e.g. {"Event": uuid, "Invoice": uuid}


def ensure_audit_schema(conn) -> None:
    """Idempotently create audit_event in the connection's current schema."""
    conn.execute((Path(__file__).parent / "schema.sql").read_text())
    conn.commit()


# ------------------------------------------------------------ ontology + params
def _load(ontology):
    from engine.compiler import compile_ontology
    if ontology is None:
        ontology = DEFAULT_ONTOLOGY
    if isinstance(ontology, (str, Path)):
        k = str(ontology)
        if k not in _ontology_cache:
            _ontology_cache[k] = compile_ontology(ontology).model
        return _ontology_cache[k]
    return getattr(ontology, "model", ontology)


def _coerce(name: str, typ: str, v: Any) -> Any:
    try:
        if typ == "string":
            if not isinstance(v, str):
                raise TypeError
            return v
        if typ == "int":
            if isinstance(v, bool) or int(v) != v:
                raise TypeError
            return int(v)
        if typ == "float":
            if isinstance(v, bool):
                raise TypeError
            return float(v)
        if typ == "bool":
            if not isinstance(v, bool):
                raise TypeError
            return v
        if typ == "uuid":
            return v if isinstance(v, UUID) else UUID(str(v))
        if typ == "timestamp":
            d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        if typ in ("point", "linestring"):
            g = json.loads(v) if isinstance(v, str) else v
            want = "Point" if typ == "point" else "LineString"
            if not isinstance(g, dict) or g.get("type") != want or "coordinates" not in g:
                raise TypeError
            return g
    except (TypeError, ValueError, OverflowError):
        pass
    raise ValueError(f"param {name!r} must be a valid {typ}, got {v!r}")


def _validate(model, name: str, params: dict) -> dict:
    a = model.actions.get(name)
    if a is None:
        raise ValueError(f"unknown action {name!r}")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    decl = {**a.params, **{k: t for k, t in INTERNAL_PARAMS.get(name, {}).items()}}
    unknown = set(params) - set(decl)
    if unknown:
        raise ValueError(f"unknown params for {name}: {sorted(unknown)}")
    out = {}
    for k, t in decl.items():
        if params.get(k) is None:
            if t.endswith("!"):
                raise ValueError(f"missing required param {k!r} for {name}")
            continue
        if k == "props" and isinstance(params[k], dict):  # declared string; dict accepted too
            out[k] = params[k]
            continue
        out[k] = _coerce(k, t.rstrip("!"), params[k])
    return out


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, datetime):
        return o.astimezone(UTC).isoformat()
    return o


# ------------------------------------------------------------------- context
class _Ctx:
    def __init__(self, conn, model, actor, valid_at, action_id):
        self.conn, self.model, self.actor = conn, model, actor
        self.vt, self.aid = valid_at, action_id
        self.changed = False
        self.before: dict = {}
        self.after: dict = {}
        self.created: dict = {}

    # objects
    def _check_type(self, type_):
        if type_ not in self.model.object_types:
            raise ValueError(f"unknown object type {type_!r}")

    def get(self, type_, id_):
        self._check_type(type_)
        r = bt.get_object(self.conn, type_, id_, as_of_valid=self.vt)
        return None if r is None else {k: v for k, v in r.items() if k not in _STRIP}

    def need(self, type_, id_):
        r = self.get(type_, id_)
        if r is None:
            raise ValueError(f"{type_} {id_} does not exist at {self.vt.isoformat()}")
        return r

    def _snap(self, bucket, key, val):
        bucket[key] = _jsonable(val)

    def put(self, type_, id_, props):
        self._check_type(type_)
        key = f"{type_}:{id_}"
        if key not in self.before:
            self._snap(self.before, key, self.get(type_, id_))
        ch = bt.write_object(self.conn, type_, id_, props, self.vt, self.actor, action_id=self.aid)
        self.changed |= ch
        self._snap(self.after, key, self.get(type_, id_))

    def merge(self, type_, id_, **changes):
        cur = self.need(type_, id_)
        cur.update(changes)
        self.put(type_, id_, cur)

    def remove(self, type_, id_):
        key = f"{type_}:{id_}"
        self.need(type_, id_)
        self._snap(self.before, key, self.get(type_, id_))
        self.changed |= bt.delete_object(self.conn, type_, id_, self.vt, self.actor, action_id=self.aid)
        self._snap(self.after, key, None)

    # links
    def _concrete(self, name):
        if name in self.model.object_types:
            return [name]
        out = []
        for t in self.model.object_types:
            c = t
            while c:
                if c == name:
                    out.append(t)
                    break
                c = self.model.parent_of(c)
        return out

    def _link_def(self, link):
        lt = self.model.link_types.get(link)
        if lt is None:
            raise ValueError(f"unknown link type {link!r}")
        return lt

    def _check_end(self, tname, id_):
        if not any(bt.get_object(self.conn, t, id_, as_of_valid=self.vt) for t in self._concrete(tname)):
            raise ValueError(f"{tname} {id_} does not exist at {self.vt.isoformat()}")

    def _link_exists(self, link, src, dst):
        return dst in bt.neighbours(self.conn, link, src, "out", as_of_valid=self.vt)

    def link(self, link, src, dst):
        lt = self._link_def(link)
        self._check_end(lt.src, src)
        self._check_end(lt.dst, dst)
        key = f"{link}:{src}>{dst}"
        self._snap(self.before, key, {"exists": self._link_exists(link, src, dst)})
        self.changed |= bt.write_link(self.conn, link, src, dst, self.vt, self.actor, action_id=self.aid)
        self._snap(self.after, key, {"exists": self._link_exists(link, src, dst)})

    def unlink(self, link, src, dst):
        self._link_def(link)
        key = f"{link}:{src}>{dst}"
        self._snap(self.before, key, {"exists": self._link_exists(link, src, dst)})
        self.changed |= bt.end_link(self.conn, link, src, dst, self.vt, self.actor, action_id=self.aid)
        self._snap(self.after, key, {"exists": self._link_exists(link, src, dst)})


# ------------------------------------------------------------------- handlers
def _props_arg(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            raise ValueError("props must be a JSON object")
    if not isinstance(v, dict):
        raise ValueError("props must be a JSON object")
    return v


def _create_object(c, p):
    id_ = p.get("id") or uuid4()
    if c.get(p["type"], id_) is not None:
        raise ValueError(f"{p['type']} {id_} already exists")
    c.put(p["type"], id_, _props_arg(p["props"]))
    c.created[p["type"]] = id_


def _update_object(c, p):
    c.need(p["type"], p["id"])
    c.put(p["type"], p["id"], _props_arg(p["props"]))


def _delete_object(c, p):
    c.remove(p["type"], p["id"])


def _ship_depart(c, p):
    c.merge("Shipment", p["shipment"], status="in_transit", progress=0.0)


def _ship_position(c, p):
    # A position update means the shipment is moving again: a delayed shipment resumes in_transit.
    cur = c.need("Shipment", p["shipment"])
    ch = {"position": p["position"], "progress": p["progress"]}
    if cur.get("status") == "delayed":
        ch["status"] = "in_transit"
    c.merge("Shipment", p["shipment"], **ch)


def _ship_arrive(c, p):
    c.merge("Shipment", p["shipment"], status="arrived", progress=1.0)


def _delay_shipment(c, p):
    cur = c.need("Shipment", p["shipment"])
    h = p["delay_hours"]
    ch = {"status": "delayed", "delay_hours": h}
    if cur.get("eta") is not None:
        ch["eta"] = cur["eta"] + timedelta(hours=h)
    c.merge("Shipment", p["shipment"], **ch)


def _open_closure(c, port):
    """Open port_closure Event (ended unset) affecting `port`, as of valid_at."""
    for ev in bt.neighbours(c.conn, "affects", port, "in", as_of_valid=c.vt):
        e = c.get("Event", ev)
        if e and e["kind"] == "port_closure" and e.get("ended") is None:
            return ev
    return None


def _close_port(c, p):
    port = p["port"]
    c.merge("Port", port, status="closed")
    note = p.get("reason") or ""
    if p.get("duration_h") is not None:
        note = f"{note} (expected duration {p['duration_h']:g} h)".strip()
    ev = p.get("event_id") or uuid4()
    c.put("Event", ev, {"kind": "port_closure", "severity": "high", "started": c.vt,
                        "ended": None, "note": note or None})
    c.link("affects", ev, port)
    c.created["Event"] = ev


def _reopen_port(c, p):
    port = p["port"]
    c.merge("Port", port, status="open")
    ev = _open_closure(c, port)
    if ev is not None:
        c.merge("Event", ev, ended=c.vt)


def _issue_invoice(c, p):
    c.need("Shipment", p["shipment"])
    c.need("Supplier", p["supplier"])
    inv = p.get("invoice_id") or uuid4()
    props = {"ref": p.get("ref") or f"INV-{str(inv)[:8].upper()}", "amount": p["amount"],
             "currency": p.get("currency") or "USD", "status": "open", "due": p.get("due"),
             "anomaly": None}
    c.put("Invoice", inv, props)
    c.link("invoices_for", inv, p["shipment"])
    c.link("billed_by", inv, p["supplier"])
    c.created["Invoice"] = inv


def _flag_invoice(c, p):
    c.merge("Invoice", p["invoice"], status="flagged", anomaly=p["anomaly"])


def _pay_invoice(c, p):
    c.merge("Invoice", p["invoice"], status="paid")


def _create_link(c, p):
    c.link(p["link"], p["src"], p["dst"])


def _end_link(c, p):
    c.unlink(p["link"], p["src"], p["dst"])


HANDLERS = {
    "create_object": _create_object, "update_object": _update_object,
    "delete_object": _delete_object, "create_link": _create_link, "end_link": _end_link,
    "ship_depart": _ship_depart, "ship_position": _ship_position, "ship_arrive": _ship_arrive,
    "delay_shipment": _delay_shipment, "close_port": _close_port, "reopen_port": _reopen_port,
    "issue_invoice": _issue_invoice, "flag_invoice": _flag_invoice, "pay_invoice": _pay_invoice,
}


def apply_action(conn, name: str, params: dict, actor: str, *, valid_at: datetime,
                 source: str = "api", run_id: str | None = None, ontology=None) -> ActionResult:
    """Validate, write (via bitemporal), audit and commit in ONE transaction.

    Raises ValueError on validation problems; any failure rolls back writes and audit row.
    `ontology`: None (default supply_chain.yaml), a path, or a compiled ontology/model.
    """
    model = _load(ontology)
    if not isinstance(valid_at, datetime):
        raise ValueError("valid_at must be a datetime")
    valid_at = valid_at if valid_at.tzinfo else valid_at.replace(tzinfo=UTC)
    if not actor:
        raise ValueError("actor is required")
    clean = _validate(model, name, params)
    if name not in HANDLERS:
        raise ValueError(f"action {name!r} has no implementation")
    aid = uuid4()
    ctx = _Ctx(conn, model, actor, valid_at, aid)
    with conn.transaction():
        HANDLERS[name](ctx, clean)
        seq = conn.execute(
            "INSERT INTO audit_event (action_id, name, actor, source, run_id, params, sim_time, before, after) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb) RETURNING seq",
            [aid, name, actor, source, run_id, json.dumps(_jsonable(clean)), valid_at,
             json.dumps(ctx.before), json.dumps(ctx.after)],
        ).fetchone()[0]
    conn.commit()
    return ActionResult(aid, ctx.changed, name, ctx.before, ctx.after, seq, ctx.created)
