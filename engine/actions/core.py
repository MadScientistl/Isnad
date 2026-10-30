"""Action engine: permissioned, audited, atomic execution of ontology actions (Phase 3).

Pipeline of ``execute`` (contract, docs/contracts.md "Phase 3 contracts"):
  role may execute -> params valid (typed) -> approval gate -> binds loaded under the principal's
  RLS (invisible == not found) -> criteria -> rules interpreter | registered function -> writes via
  ``engine.db.bitemporal`` only -> audit_event + execution_diff + webhook outbox, ONE transaction.

Transaction boundary (decision): every public entry point (execute, propose, decide, rollback,
dry_run) opens ``conn.transaction()`` itself, wraps the work in ``scoped(conn, principal)`` (so the
identity is set per call and reset on exit) and commits the connection on success, exactly like the
old ``apply_action``. The caller must NOT be inside ``scoped`` already. On any exception nothing
is written, except the separate committed ``status='denied'`` audit row for permission / criteria
failures when source is 'api' or 'agent'.
"""
from __future__ import annotations

import logging
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import errors as pgerr

from engine.compiler import exprs
from engine.db import bitemporal as bt
from engine.db.principal import OWNER, Principal, ScenarioCtx, scoped

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ONTOLOGY = ROOT / "ontology" / "supply_chain.yaml"
SIM = Principal("sim", "system:sim", "")
DENY_AUDIT_SOURCES = ("api", "agent")

_STRIP = {"id", "valid_during", "recorded_during", "changed_by", "action_id", "scenario_id"}
_ontology_cache: dict[str, Any] = {}


# ------------------------------------------------------------------- errors
log = logging.getLogger("amarh.actions")


class PermissionDenied(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class CriteriaFailed(Exception):
    """`failures`: list of {"message", "expr"} (messages of the criteria that evaluated false)."""

    def __init__(self, failures: list[dict]):
        super().__init__("; ".join(f["message"] for f in failures))
        self.failures = failures


class NotFound(ValueError):
    """Object (or proposal / execution) missing OR invisible to the caller: identical on purpose."""


class ScenarioNotFound(NotFound):
    """Scenario unknown, invisible to the caller, or not open: identical on purpose (hidden == missing)."""


class MergeConflict(Exception):
    """Main changed on objects/links a scenario touched. `conflicts`: [{kind, type, key, reason}]."""

    def __init__(self, conflicts: list[dict]):
        super().__init__(f"merge conflict on {len(conflicts)} key(s)")
        self.conflicts = conflicts


class ApprovalRequired(Exception):
    """The action needs approval for this role: use `propose` (the API turns this into a 202)."""

    def __init__(self, action: str, approvers: tuple[str, ...] = ()):
        super().__init__(f"action {action!r} requires approval; propose it instead")
        self.action, self.approvers = action, approvers


class StaleProposal(Exception):
    """The data a proposal was based on changed before approval."""
    def __init__(self, changed):
        self.changed = list(changed)
        super().__init__(f"proposal is stale: {', '.join(self.changed)} changed since it was proposed")


class ProposalExpired(Exception):
    pass


class RollbackConflict(Exception):
    def __init__(self, keys: list[str]):
        super().__init__(f"cannot roll back: current state differs from the recorded result for {keys}")
        self.keys = keys


class RollbackRefused(ValueError):
    """Rollback not possible at all (irreversible action, already rolled back, not an action...)."""


@dataclass
class ActionResult:
    action_id: UUID
    changed: bool
    name: str = ""
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    audit_seq: int | None = None
    created: dict = field(default_factory=dict)  # e.g. {"Event": uuid, "Invoice": uuid}
    status: str = "executed"
    diff: list = field(default_factory=list)  # [{kind,type,key,op,before,after}]
    webhooks: list = field(default_factory=list)  # hook names enqueued
    proposal_id: UUID | None = None
    impact: dict | None = None  # dry_run inside a scenario: {before:{kpis}, after:{kpis}, delta:{kpis}}


def _sid(scenario_id) -> int:
    if type(scenario_id) is not int or scenario_id < 0:
        raise ValueError("scenario_id must be a non-negative int")
    return scenario_id


def _who(principal, sid: int):
    """Identity to scope the connection to. The internal system principal (`SIM`) is not an owner/shared role of any
    scenario, so inside a scenario its writes (what-if simulation, source='sim') are made through the loader
    connection (OWNER scope + scenario GUCs) while permissions and audit still use the `sim` role identity. On
    main nothing changes. Only the exact SIM identity qualifies (the API refuses `sim` as x-role)."""
    if sid and principal is not OWNER and getattr(principal, "role", None) == SIM.role \
            and getattr(principal, "user", None) == SIM.user:
        return OWNER
    return principal


def _scn_ctx(conn, sid: int) -> ScenarioCtx | None:
    """Trusted ScenarioCtx for `sid` (owner-side read). Visibility for the caller is checked inside the
    scoped block by `_need_scn` (a ctx alone grants nothing: RLS and the eff_* views fail closed)."""
    if not sid:
        return None
    from engine.db import scenario_admin
    try:
        return scenario_admin.ctx_of(conn, sid)
    except LookupError:
        raise ScenarioNotFound(f"scenario {sid} not found") from None


def _need_scn(conn, sid: int, *, write: bool) -> None:
    """Inside scoped(): the scenario must be open and visible (else NotFound); writing needs owner/admin."""
    if not sid:
        return
    vis, wr = conn.execute("SELECT amarh_scenario_visible(%s), amarh_scenario_writable(%s)", [sid, sid]).fetchone()
    if not vis:
        raise ScenarioNotFound(f"scenario {sid} not found")
    if write and not wr:
        raise PermissionDenied("this scenario is read-only for your role (only the owner or an admin may change it)")


def ensure_audit_schema(conn) -> None:
    """Compat: idempotently apply the governance tables (engine/db/governance.sql) and commit."""
    from engine.db.setup import GOVERNANCE_SQL
    conn.execute(GOVERNANCE_SQL.read_text())
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


def make_principal(role: str, user: str, ontology=None) -> Principal:
    """Principal with region and `approves` filled from the ontology's role definition."""
    r = _load(ontology).roles.get(role)
    if r is None:
        return Principal(role, user, "")
    return Principal(role, user, r.region or "", tuple(r.approves))


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


def _validate(a, params: dict) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    specs = {s.name: s for s in a.param_specs()}
    unknown = set(params) - set(specs)
    if unknown:
        raise ValueError(f"unknown params for {a.name}: {sorted(unknown)}")
    out = {}
    for k, s in specs.items():
        if params.get(k) is None:
            if s.required:
                raise ValueError(f"missing required param {k!r} for {a.name}")
            continue
        if k == "props" and isinstance(params[k], dict):  # declared string; a dict is accepted too
            out[k] = params[k]
            continue
        out[k] = _coerce(k, s.base, params[k])
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


def _jdump(o) -> str:
    return json.dumps(_jsonable(o), default=str)


def _vt(valid_at) -> datetime:
    if not isinstance(valid_at, datetime):
        raise ValueError("valid_at must be a datetime")
    return valid_at if valid_at.tzinfo else valid_at.replace(tzinfo=UTC)


class _P:
    """Normalised acting identity."""

    def __init__(self, principal):
        self.owner = principal is OWNER
        self.p = Principal("admin", "system:owner", "") if self.owner else principal
        self.role, self.user = self.p.role, self.p.user
        if not self.user:
            raise ValueError("principal.user is required")


# ------------------------------------------------------------------- context
class _Ctx:
    def __init__(self, conn, model, pr: _P, valid_at, action_id, sid: int = 0):
        self.conn, self.model, self.pr = conn, model, pr
        self.sid = sid
        self.actor = pr.user
        self.vt, self.aid = valid_at, action_id
        self.changed = False
        self.before: dict = {}
        self.after: dict = {}
        self.created: dict = {}

    # objects
    def _check_type(self, type_):
        if type_ not in self.model.object_types:
            raise ValueError(f"unknown object type {type_!r}")

    def _check_write(self, type_):
        if self.pr.owner:
            return
        r = self.model.roles.get(self.pr.role)
        if r is None or not r.can_write(type_):
            raise PermissionDenied(f"role {self.pr.role!r} may not write {type_}")

    def get(self, type_, id_, masked=False):
        self._check_type(type_)
        r = bt.get_object(self.conn, type_, id_, as_of_valid=self.vt, masked=masked, scenario_id=self.sid)
        return None if r is None else {k: v for k, v in r.items() if k not in _STRIP}

    def need(self, type_, id_):
        r = self.get(type_, id_)
        if r is None:
            raise NotFound(f"{type_} {id_} not found at {self.vt.isoformat()}")
        return r

    def _snap(self, bucket, key, val):
        bucket[key] = _jsonable(val)

    def put(self, type_, id_, props):
        self._check_type(type_)
        self._check_write(type_)
        key = f"{type_}:{id_}"
        if key not in self.before:
            self._snap(self.before, key, self.get(type_, id_))
        ch = bt.write_object(self.conn, type_, id_, props, self.vt, self.actor, action_id=self.aid,
                             scenario_id=self.sid)
        self.changed |= ch
        self._snap(self.after, key, self.get(type_, id_))

    def merge(self, type_, id_, **changes):
        cur = self.need(type_, id_)
        cur.update(changes)
        self.put(type_, id_, cur)

    def remove(self, type_, id_):
        self._check_write(type_)
        key = f"{type_}:{id_}"
        self.need(type_, id_)
        if key not in self.before:
            self._snap(self.before, key, self.get(type_, id_))
        self.changed |= bt.delete_object(self.conn, type_, id_, self.vt, self.actor, action_id=self.aid,
                                          scenario_id=self.sid)
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

    def visible_any(self, tname, id_) -> bool:
        return any(bt.get_object(self.conn, t, id_, as_of_valid=self.vt, scenario_id=self.sid)
                   for t in self._concrete(tname))

    def _link_def(self, link):
        lt = self.model.link_types.get(link)
        if lt is None:
            raise ValueError(f"unknown link type {link!r}")
        return lt

    def _check_end(self, tname, id_):
        if not self.visible_any(tname, id_):
            raise NotFound(f"{tname} {id_} not found at {self.vt.isoformat()}")

    def _link_exists(self, link, src, dst):
        return dst in bt.neighbours(self.conn, link, src, "out", as_of_valid=self.vt, scenario_id=self.sid)

    def link(self, link, src, dst):
        lt = self._link_def(link)
        self._check_end(lt.src, src)
        self._check_end(lt.dst, dst)
        key = f"{link}:{src}>{dst}"
        if key not in self.before:
            self._snap(self.before, key, {"exists": self._link_exists(link, src, dst)})
        self.changed |= bt.write_link(self.conn, link, src, dst, self.vt, self.actor, action_id=self.aid,
                                       scenario_id=self.sid)
        self._snap(self.after, key, {"exists": self._link_exists(link, src, dst)})

    def unlink(self, link, src, dst):
        self._link_def(link)
        key = f"{link}:{src}>{dst}"
        if key not in self.before:
            self._snap(self.before, key, {"exists": self._link_exists(link, src, dst)})
        self.changed |= bt.end_link(self.conn, link, src, dst, self.vt, self.actor, action_id=self.aid,
                                     scenario_id=self.sid)
        self._snap(self.after, key, {"exists": self._link_exists(link, src, dst)})

    def diff(self) -> list[dict]:
        out = []
        for key, b in self.before.items():
            a = self.after.get(key)
            if a == b:
                continue
            head, _, rest = key.partition(":")
            if ">" in rest:  # link
                be, ae = bool((b or {}).get("exists")), bool((a or {}).get("exists"))
                if be == ae:
                    continue
                out.append({"kind": "link", "type": head, "key": rest,
                            "op": "create" if ae else "delete", "before": b, "after": a})
            else:
                op = "create" if b is None else "delete" if a is None else "modify"
                out.append({"kind": "object", "type": head, "key": rest, "op": op, "before": b, "after": a})
        return out


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
        raise ValueError(f"{p['type']} already exists")
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
    from datetime import timedelta
    cur = c.need("Shipment", p["shipment"])
    h = p["delay_hours"]
    ch = {"status": "delayed", "delay_hours": h}
    if cur.get("eta") is not None:
        ch["eta"] = cur["eta"] + timedelta(hours=h)
    c.merge("Shipment", p["shipment"], **ch)


REROUTE_COST_RATE = 0.02  # extra_cost added per unit of cargo_value on a reroute


def _reroute_shipment(c, p):
    """End every current ships_via link of the shipment, then route it via the new port."""
    ship, new = p["shipment"], p["new_port"]
    for old in bt.neighbours(c.conn, "ships_via", ship, "out", as_of_valid=c.vt, scenario_id=c.sid):
        if old != new:
            c.unlink("ships_via", ship, old)
    c.link("ships_via", ship, new)
    if c.changed:  # route actually changed: deterministic reroute cost (Phase 5 contract addendum)
        cur = c.need("Shipment", ship)
        cost = float(cur.get("extra_cost") or 0.0) + REROUTE_COST_RATE * float(cur.get("cargo_value") or 0.0)
        c.merge("Shipment", ship, extra_cost=cost)


def _open_closure(c, port):
    """Open port_closure Event (ended unset) affecting `port`, as of valid_at."""
    for ev in bt.neighbours(c.conn, "affects", port, "in", as_of_valid=c.vt, scenario_id=c.sid):
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
    "reroute_shipment": _reroute_shipment,
    "create_object": _create_object, "update_object": _update_object,
    "delete_object": _delete_object, "create_link": _create_link, "end_link": _end_link,
    "ship_depart": _ship_depart, "ship_position": _ship_position, "ship_arrive": _ship_arrive,
    "delay_shipment": _delay_shipment, "close_port": _close_port, "reopen_port": _reopen_port,
    "issue_invoice": _issue_invoice, "flag_invoice": _flag_invoice, "pay_invoice": _pay_invoice,
}


# ------------------------------------------------------------ rules interpreter
def _run_rules(c: _Ctx, a, clean: dict, aliases: dict) -> None:
    new: dict[str, UUID] = {}

    def res(v):
        if isinstance(v, str) and v.startswith("$"):
            n = v[1:]
            head, _, rest = n.partition(".")
            if n == "now":
                return c.vt
            if n == "actor":
                return c.actor
            if head == "new" and rest:
                return new[rest]
            if rest:
                obj = aliases.get(head)
                return None if obj is None else obj.get(rest)
            return clean.get(n)
        return v

    for r in a.rules:
        if r.kind == "modify":
            tid = res(r.id)
            cur = c.need(r.type, tid)
            cur.update({k: res(v) for k, v in r.set.items()})
            c.put(r.type, tid, cur)
        elif r.kind == "create":
            tid = res(r.id) if r.id is not None else uuid4()
            if c.get(r.type, tid) is not None:
                raise ValueError(f"{r.type} already exists")
            c.put(r.type, tid, {k: res(v) for k, v in r.set.items()})
            c.created[r.type] = tid
            if r.as_name:
                new[r.as_name] = tid
        elif r.kind == "delete":
            c.remove(r.type, res(r.id))
        elif r.kind == "link":
            c.link(r.link, res(r.src), res(r.dst))
        elif r.kind == "unlink":
            c.unlink(r.link, res(r.src), res(r.dst))
        else:  # pragma: no cover - compiler rejects
            raise ValueError(f"unknown rule kind {r.kind!r}")


# ------------------------------------------------------------------ pipeline
def _pipeline(conn, model, a, raw_params, pr: _P, valid_at, aid, *, gate: bool, sid: int = 0):
    name = a.name
    if not pr.owner and not model.role_can_execute(pr.role, name):
        raise PermissionDenied(f"role {pr.role!r} may not execute {name!r}")
    clean = _validate(a, raw_params)
    if gate and a.requires_approval and pr.role not in a.approval_exempt:
        raise ApprovalRequired(name, a.approvers)
    c = _Ctx(conn, model, pr, valid_at, aid, sid)
    aliases: dict[str, dict | None] = {}
    for al, b in a.bind.items():
        oid = clean.get(b.id[1:]) if b.id.startswith("$") else b.id
        if oid is None:
            raise ValueError(f"bind {al!r}: missing id {b.id}")
        obj = bt.get_object(conn, b.type, oid, as_of_valid=valid_at, scenario_id=sid)
        if obj is None:
            raise NotFound(f"{b.type} {oid} not found at {valid_at.isoformat()}")
        d = {k: v for k, v in obj.items() if k not in _STRIP}
        d["id"] = oid
        aliases[al] = d
    for s in a.param_specs():
        if s.ref and clean.get(s.name) is not None and not c.visible_any(s.ref, clean[s.name]):
            raise NotFound(f"{s.ref} {clean[s.name]} not found at {valid_at.isoformat()}")
    failures = []
    for cr in a.criteria:
        ok = exprs.evaluate(cr.ast if cr.ast is not None else exprs.parse_expr(cr.expr),
                            params=clean, aliases=aliases, now=valid_at, actor=pr.user)
        if not ok:
            failures.append({"message": cr.message, "expr": cr.expr})
    if failures:
        raise CriteriaFailed(failures)
    if a.function:
        fn = HANDLERS.get(a.function)
        if fn is None:
            raise ValueError(f"action {name!r}: function {a.function!r} is not registered")
        fn(c, clean)
    else:
        _run_rules(c, a, clean, aliases)
    return c, clean


def _persist(conn, model, a, pr: _P, c: _Ctx, clean, valid_at, aid, *, source, run_id, proposal_id,
             tool_trace, on_behalf_of, name=None, parent=None, params=None) -> tuple[int, list, list]:
    diff = c.diff()
    sid = c.sid
    seq = conn.execute(
        "INSERT INTO audit_event (action_id, name, actor, source, run_id, params, sim_time, before, after, "
        "role, on_behalf_of, proposal_id, status, tool_trace, parent_execution_id, scenario_id) "
        "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s,%s,%s,'executed',%s::jsonb,%s,%s) RETURNING seq",
        [aid, name or a.name, pr.user, source, run_id, _jdump(params if params is not None else clean),
         valid_at, json.dumps(c.before), json.dumps(c.after), pr.role, on_behalf_of, proposal_id,
         None if tool_trace is None else _jdump(tool_trace), parent, sid],
    ).fetchone()[0]
    if diff:
        conn.cursor().executemany(
            "INSERT INTO execution_diff (action_id, kind, type, key, op, before, after, scenario_id) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
            [(aid, d["kind"], d["type"], d["key"], d["op"],
              None if d["before"] is None else json.dumps(d["before"]),
              None if d["after"] is None else json.dumps(d["after"]), sid) for d in diff])
    hooks: list[str] = []
    # a scenario is a sandbox: it never enqueues webhooks (nothing leaves the platform)
    if a is not None and source != "sim" and c.changed and not sid:
        for hook in a.webhooks:
            wd = model.webhooks.get(hook)
            if wd is None:
                continue
            oid = uuid4()
            payload = {"hook": hook, "action": a.name, "action_id": str(aid), "actor": pr.user,
                       "role": pr.role, "valid_at": valid_at.isoformat(),
                       "params": _jsonable(clean), "created": _jsonable(c.created)}
            conn.execute(
                "INSERT INTO webhook_outbox (id, action_id, hook, url, payload, idempotency_key) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                [oid, aid, hook, os.environ.get(wd.url_env) or None, json.dumps(payload), f"{aid}:{hook}"])
            conn.execute("INSERT INTO webhook_delivery (outbox_id, attempt, status, next_attempt_at) "
                         "VALUES (%s,0,'pending',NULL)", [oid])
            hooks.append(hook)
    return seq, diff, hooks


def _do_execute(conn, model, a, raw_params, pr: _P, valid_at, aid, *, source, run_id, proposal_id,
                tool_trace, on_behalf_of, gate=True, sid: int = 0) -> ActionResult:
    """Everything inside the (already open) scoped transaction. Inside a scenario there is no approval gate
    (sandbox: the approval gate protects main; merging re-applies the gate)."""
    c, clean = _pipeline(conn, model, a, raw_params, pr, valid_at, aid, gate=gate and not sid, sid=sid)
    seq, diff, hooks = _persist(conn, model, a, pr, c, clean, valid_at, aid, source=source, run_id=run_id,
                                proposal_id=proposal_id, tool_trace=tool_trace, on_behalf_of=on_behalf_of)
    return ActionResult(aid, c.changed, a.name, c.before, c.after, seq, c.created, "executed", diff, hooks,
                        proposal_id)


def _action(model, name):
    a = model.actions.get(name)
    if a is None:
        raise ValueError(f"unknown action {name!r}")
    return a


def _audit_denied(conn, model, pr: _P, name, params, valid_at, source, run_id, proposal_id, tool_trace,
                  error: str, failures=None, sid: int = 0) -> None:
    """Own committed transaction: a denial leaves a status='denied' audit row, no data writes. Inside a
    scenario the row carries the scenario id (and is only visible under that scenario)."""
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        sctx = _scn_ctx(conn, sid) if sid else None
        with conn.transaction():
            with scoped(conn, OWNER if pr.owner else pr.p, sctx):
                conn.execute(
                    "INSERT INTO audit_event (action_id, name, actor, source, run_id, params, sim_time, before, "
                    "after, role, proposal_id, status, tool_trace, scenario_id) VALUES (%s,%s,%s,%s,%s,%s::jsonb,"
                    "%s,NULL,%s::jsonb,%s,%s,'denied',%s::jsonb,%s)",
                    [uuid4(), name, pr.user, source, run_id, _jdump(params), valid_at,
                     _jdump({"error": error, "failures": failures or []}), pr.role, proposal_id,
                     None if tool_trace is None else _jdump(tool_trace), sid])
        conn.commit()
    except (psycopg.Error, ScenarioNotFound) as e:
        # e.g. the caller may not write that scenario (RLS) or it vanished: the denial is not auditable there
        log.warning("denial in scenario %s could not be audited: %s", sid, str(e).splitlines()[0])
        _safe_rollback(conn)


def _guarded(conn, model, pr, name, params, valid_at, source, run_id, proposal_id, tool_trace, fn, sid: int = 0):
    try:
        try:
            return fn()
        except pgerr.InsufficientPrivilege as e:
            log.warning("RLS denied a write: %s", str(e).splitlines()[0])
            raise PermissionDenied("row-level security denied the write") from e
    except PermissionDenied as e:
        if source in DENY_AUDIT_SOURCES:
            _audit_denied(conn, model, pr, name, params, valid_at, source, run_id, proposal_id, tool_trace,
                          e.detail, sid=sid)
        else:
            _safe_rollback(conn)
        raise
    except CriteriaFailed as e:
        if source in DENY_AUDIT_SOURCES:
            _audit_denied(conn, model, pr, name, params, valid_at, source, run_id, proposal_id, tool_trace,
                          str(e), e.failures, sid=sid)
        else:
            _safe_rollback(conn)
        raise


def _safe_rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


# ----------------------------------------------------------------- public API
def execute(conn, name: str, params: dict, principal, *, valid_at: datetime, source: str = "api",
            run_id: str | None = None, proposal_id: UUID | None = None, tool_trace=None, ontology=None,
            on_behalf_of: str | None = None, scenario_id: int = 0) -> ActionResult:
    """Validate, authorise, write, audit and commit in ONE transaction (see module docstring).

    `scenario_id` > 0 runs the action inside that what-if scenario (Phase 5): same permission, criteria and RLS
    checks as main, writes only the scenario overlay, audit rows carry the scenario id, no webhooks, no graph
    outbox, no approval gate (sandbox). The caller must own the scenario (or be admin).

    Raises ValueError/NotFound (validation, missing or invisible object), ScenarioNotFound, PermissionDenied,
    CriteriaFailed, ApprovalRequired (use `propose`).
    """
    model = _load(ontology)
    a = _action(model, name)
    valid_at = _vt(valid_at)
    pr = _P(principal)
    aid = uuid4()
    sid = _sid(scenario_id)

    def go():
        sctx = _scn_ctx(conn, sid)
        with conn.transaction():
            with scoped(conn, _who(principal, sid), sctx):
                _need_scn(conn, sid, write=True)
                r = _do_execute(conn, model, a, params, pr, valid_at, aid, source=source, run_id=run_id,
                                proposal_id=proposal_id, tool_trace=tool_trace, on_behalf_of=on_behalf_of,
                                sid=sid)
        conn.commit()
        return r

    return _guarded(conn, model, pr, name, params, valid_at, source, run_id, proposal_id, tool_trace, go, sid)


def _impact(before: dict, after: dict) -> dict:
    """Contract shape: {before:{kpis}, after:{kpis}, delta:{kpis}} with delta = after - before."""
    keys = sorted(set(before) | set(after))
    return {"before": {"kpis": before}, "after": {"kpis": after},
            "delta": {"kpis": {k: (after.get(k) or 0) - (before.get(k) or 0) for k in keys}}}


def dry_run(conn, name: str, params: dict, principal, *, valid_at: datetime, ontology=None,
            scenario_id: int = 0) -> ActionResult:
    """Run the whole pipeline (permissions, criteria, rules) and return the diff; commits nothing.
    The approval gate is not applied (a dry run is how a proposal previews its effect). With `scenario_id`
    the run happens inside the scenario and `impact` holds the KPIs (main before, scenario after, delta)."""
    from engine import kpis
    model = _load(ontology)
    a = _action(model, name)
    valid_at = _vt(valid_at)
    pr = _P(principal)
    aid = uuid4()
    sid = _sid(scenario_id)
    sctx = _scn_ctx(conn, sid)
    with conn.transaction(force_rollback=True):
        before = None
        if sid:
            with scoped(conn, principal):
                before = kpis.compute(conn, principal, 0, valid_at)
        with scoped(conn, _who(principal, sid), sctx):
            _need_scn(conn, sid, write=True)
            c, clean = _pipeline(conn, model, a, params, pr, valid_at, aid, gate=False, sid=sid)
            # evaluated AT the action's valid time (SQL now() is the transaction start, before the write)
            impact = _impact(before, kpis.compute(conn, principal, sid, valid_at)) if sid else None
            return ActionResult(aid, c.changed, name, c.before, c.after, None, c.created, "dry_run", c.diff(),
                                impact=impact)


PROPOSAL_TTL_H = float(os.environ.get("AMARH_PROPOSAL_TTL_H", "24"))


def propose(conn, name: str, params: dict, principal, rationale: str | None = None, *, valid_at: datetime,
            source: str = "api", run_id: str | None = None, tool_trace=None, ontology=None,
            on_behalf_of: str | None = None, evidence=None, idempotency_key: str | None = None,
            ttl_h: float | None = None, scenario_id: int = 0) -> UUID:
    """Store a proposal (with a dry-run preview). Writes nothing else; the role must be allowed to
    execute the action. Criteria are checked now so that doomed proposals are refused early.

    With `scenario_id` the preview is the real run INSIDE that scenario plus `impact` (KPIs: main before,
    scenario after, delta) and `scenario_id`; the proposal row keeps it as evidence scenario but still
    executes ONLY on main after a human approves (staleness is evaluated against main: the stored base
    versions come from a main dry run, empty when the action cannot run on main yet)."""
    model = _load(ontology)
    a = _action(model, name)
    valid_at = _vt(valid_at)
    pr = _P(principal)
    pid = uuid4()
    sid = _sid(scenario_id)

    def go():
        sctx = _scn_ctx(conn, sid)
        with conn.transaction():
            r = _propose_open(conn, model, a, params, principal, pr, sctx, sid, pid, name, rationale, valid_at,
                              run_id, tool_trace, on_behalf_of, evidence, idempotency_key, ttl_h)
        conn.commit()
        return r

    return _guarded(conn, model, pr, name, params, valid_at, source, run_id, None, tool_trace, go, sid)


def _preview_run(conn, model, a, params, pr, valid_at, sid, with_kpis=None):
    """One pipeline run inside a savepoint that is always rolled back. Returns (clean, preview, extra)."""
    try:
        with conn.transaction():
            c, clean = _pipeline(conn, model, a, params, pr, valid_at, uuid4(), gate=False, sid=sid)
            extra = with_kpis() if with_kpis else None
            raise _Preview(clean, {"valid_at": valid_at.isoformat(), "changed": c.changed, "diff": c.diff()}, extra)
    except _Preview as pv:
        return pv.clean, pv.preview, pv.extra


def _propose_open(conn, model, a, params, principal, pr, sctx, sid, pid, name, rationale, valid_at, run_id,
                  tool_trace, on_behalf_of, evidence, idempotency_key, ttl_h):
    """Body of `propose` inside the open transaction (shared with the scenario merge, which must not commit)."""
    from engine import kpis
    base_diff = None
    if not sid:
        with scoped(conn, principal):
            clean, preview, _ = _preview_run(conn, model, a, params, pr, valid_at, 0)
        base_diff = preview["diff"]
    else:
        # main side first: baseline KPIs and what approval would do on main (staleness base)
        with scoped(conn, principal):
            before = kpis.compute(conn, principal, 0, valid_at)
            try:
                _c0, main_preview, _ = _preview_run(conn, model, a, params, pr, valid_at, 0)
                base_diff = main_preview["diff"]
            except (NotFound, CriteriaFailed, ValueError):
                main_preview = None  # e.g. the target only exists inside the scenario: nothing to compare yet
        with scoped(conn, principal, sctx):
            _need_scn(conn, sid, write=True)
            clean, preview, after = _preview_run(conn, model, a, params, pr, valid_at, sid,
                                                 with_kpis=lambda: kpis.compute(conn, principal, sid, valid_at))
            preview["scenario_id"] = sid
            preview["impact"] = _impact(before, after)
            if not preview["diff"] and main_preview is not None:  # already applied inside the scenario
                preview["scenario_diff"], preview["diff"] = preview["diff"], main_preview["diff"]
    with scoped(conn, principal, sctx):
        if idempotency_key:
            dup = conn.execute("SELECT id FROM proposal WHERE idempotency_key=%s", [idempotency_key]).fetchone()
            if dup:
                return dup[0]
        ttl = PROPOSAL_TTL_H if ttl_h is None else ttl_h
        conn.execute(
            "INSERT INTO proposal (id, action, params, proposer, proposer_role, rationale, dry_run, "
            "run_id, tool_trace, on_behalf_of, idempotency_key, evidence, base_versions, expires_at, scenario_id) "
            "VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,"
            "clock_timestamp() + make_interval(secs => %s),%s)",
            [pid, name, _jdump(clean), pr.user, pr.role, rationale, _jdump(preview), run_id,
             None if tool_trace is None else _jdump(tool_trace), on_behalf_of, idempotency_key,
             None if evidence is None else _jdump(evidence),
             _jdump(_base_versions(base_diff if base_diff is not None else [])),
             ttl * 3600, sid])
        if sid:
            conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES (%s,'proposal',%s,%s::jsonb)",
                         [sid, pr.user, _jdump({"proposal_id": str(pid), "action": name})])
    return pid


class _Preview(Exception):
    def __init__(self, clean, preview, extra=None):
        self.clean, self.preview, self.extra = clean, preview, extra


def decide(conn, proposal_id: UUID, principal, approve: bool, comment: str | None = None, *,
           valid_at: datetime | None = None, source: str = "api", ontology=None,
           tool_trace=None) -> ActionResult | None:
    """Approve (executes AS the approver with the full pipeline re-run; returns the ActionResult) or
    reject (returns None). D3: proposer != approver, admin included. One decision per proposal."""
    model = _load(ontology)
    pr = _P(principal)
    ctx_name = {"n": "approval"}

    def go():
        with conn.transaction():
            with scoped(conn, principal):
                row = conn.execute(
                    "SELECT action, params, proposer, proposer_role, dry_run, run_id, on_behalf_of, "
                    "base_versions, expires_at < clock_timestamp() FROM proposal WHERE id=%s",
                    [proposal_id]).fetchone()
                if row is None:
                    raise NotFound(f"proposal {proposal_id} not found")
                action, params, proposer, _prole, dry, run_id, behalf, base, expired = row
                ctx_name["n"] = action
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [str(proposal_id)])
                if conn.execute("SELECT 1 FROM approval WHERE proposal_id=%s", [proposal_id]).fetchone():
                    raise ValueError(f"proposal {proposal_id} was already decided")
                if not pr.owner and not model.role_can_approve(pr.role, action):
                    raise PermissionDenied(f"role {pr.role!r} may not approve {action!r}")
                if proposer == pr.user or (behalf and behalf == pr.user):
                    raise PermissionDenied("the proposer (or the user the agent acted for) cannot decide "
                                           "their own proposal")
                if approve and expired:
                    raise ProposalExpired(f"proposal {proposal_id} has expired")
                aid = uuid4()
                res = None
                if approve:
                    # executes at approval time (not the proposal's valid time) so binds and criteria see
                    # current data; anything that changed since the dry run makes the proposal stale
                    vt = _vt(valid_at) if valid_at is not None else datetime.now(UTC)
                    res = _do_execute(conn, model, _action(model, action), params, pr, vt, aid, source=source,
                                      run_id=run_id, proposal_id=proposal_id, tool_trace=tool_trace,
                                      on_behalf_of=behalf or proposer, gate=False)
                    changed = _stale_keys(base, res.diff)
                    if changed:
                        raise StaleProposal(changed)
                try:
                    conn.execute(
                        "INSERT INTO approval (id, proposal_id, decision, approver, approver_role, comment, "
                        "execution_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [uuid4(), proposal_id, "approved" if approve else "rejected", pr.user, pr.role, comment,
                         aid if approve else None])
                except pgerr.UniqueViolation:
                    raise ValueError(f"proposal {proposal_id} was already decided") from None
        conn.commit()
        return res

    vt0 = _vt(valid_at) if valid_at is not None else datetime.now(UTC)
    # the denial audit needs the action name, filled in once the proposal row is read
    try:
        return _guarded_decide(conn, model, pr, proposal_id, vt0, source, tool_trace, go, ctx_name)
    finally:
        pass


# Properties the simulation rewrites constantly; they must not make every proposal about a moving shipment
# stale. TODO: declare as `volatile: true` in the YAML ontology instead of a constant.
VOLATILE_PROPS = frozenset({"position", "progress"})


def _stable(snap):
    return {k: v for k, v in snap.items() if k not in VOLATILE_PROPS} if isinstance(snap, dict) else snap


def _base_versions(diff) -> dict:
    """What a proposal was based on: the 'before' snapshot of every object/link the action touches."""
    return {f"{d['kind']}:{d['type']}:{d['key']}": _stable(d["before"]) for d in diff}


def _stale_keys(base, diff) -> list[str]:
    """Keys whose recorded base differs from what the approval run saw."""
    if not base:
        return []
    now = {f"{d['kind']}:{d['type']}:{d['key']}": _stable(json.loads(json.dumps(d["before"], default=str)))
           for d in diff}
    changed = {k for k, b in base.items() if now.get(k, "<missing>") != b}
    # something that already existed at approval but was not part of the proposal (e.g. a link added since)
    changed |= {k for k, v in now.items() if k not in base and v is not None}
    return sorted(changed)


def _guarded_decide(conn, model, pr, proposal_id, vt0, source, tool_trace, fn, ctx_name):
    try:
        try:
            return fn()
        except pgerr.InsufficientPrivilege as e:
            log.warning("RLS denied a write: %s", str(e).splitlines()[0])
            raise PermissionDenied("row-level security denied the write") from e
    except PermissionDenied as e:
        _deny(conn, model, pr, proposal_id, vt0, source, tool_trace, ctx_name, e.detail, None)
        raise
    except CriteriaFailed as e:
        _deny(conn, model, pr, proposal_id, vt0, source, tool_trace, ctx_name, str(e), e.failures)
        raise
    except (StaleProposal, ProposalExpired) as e:
        _deny(conn, model, pr, proposal_id, vt0, source, tool_trace, ctx_name, str(e), None)
        raise


def _deny(conn, model, pr, proposal_id, vt0, source, tool_trace, ctx_name, msg, failures):
    if source in DENY_AUDIT_SOURCES:
        _audit_denied(conn, model, pr, ctx_name["n"], {"proposal_id": str(proposal_id)}, vt0, source, None,
                      proposal_id, tool_trace, msg, failures)
    else:
        _safe_rollback(conn)


# ------------------------------------------------------------------ rollback
def _revive(model, type_, props: dict | None):
    """Undo JSON-ification of a recorded snapshot: ISO strings back to datetimes for timestamp props."""
    if props is None:
        return None
    ot = model.object_types[type_]
    ts = {p.name for p in ot.properties if p.type == "timestamp"}
    out = dict(props)
    for k in ts:
        if isinstance(out.get(k), str):
            out[k] = datetime.fromisoformat(out[k])
    return out


def rollback(conn, action_id: UUID, principal, reason: str | None = None, *, valid_at: datetime | None = None,
             source: str = "api", run_id: str | None = None, ontology=None, tool_trace=None,
             scenario_id: int = 0) -> ActionResult:
    """D1 compensating rollback: only if every affected object/link still equals the recorded `after`
    (as of `valid_at`, default now); else RollbackConflict(keys). The rollback is itself an audited
    execution (name 'rollback', parent_execution_id) plus a `rollback` row. Webhooks are not undone and
    are not re-sent. `rollback: irreversible` actions, already rolled back and denied executions refuse.

    Who may roll back: a role that may execute the original action; for approval-gated actions
    (and roles not exempt) only a role that may approve it.

    `scenario_id` > 0 rolls back an execution of that scenario INSIDE it (compensating writes in the overlay,
    no webhooks); the execution must belong to that exact scenario (main executions cannot be rolled back
    from a scenario and vice versa: NotFound).
    """
    model = _load(ontology)
    pr = _P(principal)
    vt = _vt(valid_at) if valid_at is not None else datetime.now(UTC)
    aid = uuid4()
    orig = {"name": "rollback"}
    sid = _sid(scenario_id)

    def go():
        sctx = _scn_ctx(conn, sid)
        with conn.transaction():
            with scoped(conn, _who(principal, sid), sctx):
                _need_scn(conn, sid, write=True)
                row = conn.execute(
                    "SELECT name, status, params FROM audit_event WHERE action_id=%s AND scenario_id=%s",
                    [action_id, sid]).fetchone()
                if row is None:
                    raise NotFound(f"execution {action_id} not found")
                oname, ostatus, _oparams = row
                orig["name"] = oname
                a = model.actions.get(oname)
                if ostatus != "executed" or a is None:
                    raise RollbackRefused(f"execution {action_id} ({oname}, {ostatus}) cannot be rolled back")
                if a.rollback == "irreversible":
                    raise RollbackRefused(f"action {oname!r} is irreversible")
                if not pr.owner:
                    if not model.role_can_execute(pr.role, oname):
                        raise PermissionDenied(f"role {pr.role!r} may not roll back {oname!r}")
                    if a.requires_approval and pr.role not in a.approval_exempt \
                            and not model.role_can_approve(pr.role, oname):
                        raise PermissionDenied(f"role {pr.role!r} may not roll back approval-gated {oname!r}")
                if conn.execute("SELECT 1 FROM rollback WHERE original_action_id=%s", [action_id]).fetchone():
                    raise RollbackRefused(f"execution {action_id} was already rolled back")
                diff = conn.execute(
                    "SELECT kind, type, key, op, before, after FROM execution_diff WHERE action_id=%s ORDER BY seq",
                    [action_id]).fetchall()
                c = _Ctx(conn, model, pr, vt, aid, sid)
                conflicts = []
                for kind, typ, key, op, before, after in diff:
                    if kind == "object":
                        cur = c.get(typ, UUID(key))
                        if _jsonable(cur) != after:
                            conflicts.append(f"{typ}:{key}")
                    else:
                        s, d = key.split(">")
                        ex = c._link_exists(typ, UUID(s), UUID(d))
                        if ex != bool((after or {}).get("exists")):
                            conflicts.append(f"{typ}:{key}")
                if conflicts:
                    raise RollbackConflict(conflicts)
                for kind, typ, key, op, before, after in reversed(diff):
                    if kind == "object":
                        oid = UUID(key)
                        if before is None:
                            c.remove(typ, oid)
                        else:
                            c.put(typ, oid, _revive(model, typ, before))
                    else:
                        s, d = (UUID(x) for x in key.split(">"))
                        if before and before.get("exists"):
                            c.link(typ, s, d)
                        else:
                            c.unlink(typ, s, d)
                params = {"original_action_id": str(action_id), "reason": reason}
                seq, rdiff, _ = _persist(conn, model, None, pr, c, params, vt, aid, source=source, run_id=run_id,
                                         proposal_id=None, tool_trace=tool_trace, on_behalf_of=None,
                                         name="rollback", parent=action_id, params=params)
                conn.execute(
                    "INSERT INTO rollback (id, original_action_id, rollback_action_id, actor, reason, scenario_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", [uuid4(), action_id, aid, pr.user, reason, sid])
        conn.commit()
        return ActionResult(aid, c.changed, "rollback", c.before, c.after, seq, c.created, "executed", rdiff, [])

    try:
        try:
            return go()
        except pgerr.InsufficientPrivilege as e:
            log.warning("RLS denied a write: %s", str(e).splitlines()[0])
            raise PermissionDenied("row-level security denied the write") from e
    except PermissionDenied as e:
        if source in DENY_AUDIT_SOURCES:
            _audit_denied(conn, model, pr, "rollback", {"original_action_id": str(action_id), "reason": reason},
                          vt, source, run_id, None, tool_trace, e.detail, sid=sid)
        else:
            _safe_rollback(conn)
        raise
    except Exception:
        _safe_rollback(conn)
        raise
