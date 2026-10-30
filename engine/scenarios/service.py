"""Scenario service (Phase 5): lifecycle, comparison and merge of what-if scenarios.

Transaction model: like the action engine, every public function opens its own ``conn.transaction()``,
scopes the connection to the caller (``scoped(conn, principal[, ScenarioCtx])``) and commits. The caller must
NOT be inside ``scoped``. A scenario the caller may not see is reported exactly like a missing one
(``ScenarioNotFound``); a scenario the caller can see but not change (a ``shared_roles`` viewer) raises
``PermissionDenied`` on writes (discard, merge).

Limits (env, read per call): ``AMARH_SCN_MAX_PER_USER`` (5), ``AMARH_SCN_MAX_PER_RUN`` (3),
``AMARH_SCN_MAX_GLOBAL`` (20), ``AMARH_SCN_TTL_H`` (168; fixed lifetime from creation, ``expire_due`` marks
expired scenarios and purges their rows). The scenario/event tables are append-only, so the TTL is not extended
by activity (documented deviation from "idle expiry").
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from engine import kpis
from engine.actions import core
from engine.actions.core import (ApprovalRequired, MergeConflict, NotFound, PermissionDenied, ScenarioNotFound,
                                 _jdump, _jsonable, _P, _STRIP)
from engine.db import bitemporal as bt
from engine.db import scenario_admin
from engine.db.bitemporal import ScenarioLimit
from engine.db.principal import OWNER, ScenarioCtx, scoped

log = logging.getLogger("amarh.scenarios")

TERMINAL = ("merged", "discarded", "expired")
EVENT_KINDS = ("simulated", "proposal")  # kinds an API layer may record (created/terminal ones are ours)
REPLAY_SOURCES = ("api", "agent", "ui")
VOLATILE = core.VOLATILE_PROPS
_SELECT = (
    "SELECT s.id, s.name, s.owner_user, s.owner_role, s.parent_id, s.base_recorded, s.base_sim_time, "
    "s.shared_roles, s.run_id, s.created_at, s.expires_at, st.clock, st.data_version, st.override_rows, "
    "coalesce((SELECT e.kind FROM scenario_event e WHERE e.scenario_id = s.id AND e.kind IN "
    "('merged','discarded','expired') ORDER BY e.seq DESC LIMIT 1), 'open') AS status "
    "FROM scenario s LEFT JOIN scenario_state st ON st.scenario_id = s.id")


def limits() -> dict:
    return {"per_user": int(os.environ.get("AMARH_SCN_MAX_PER_USER", "5")),
            "per_run": int(os.environ.get("AMARH_SCN_MAX_PER_RUN", "3")),
            "global": int(os.environ.get("AMARH_SCN_MAX_GLOBAL", "20")),
            "ttl_h": float(os.environ.get("AMARH_SCN_TTL_H", "168"))}


_COLS = ("id", "name", "owner_user", "owner_role", "parent_id", "base_recorded", "base_sim_time", "shared_roles",
         "run_id", "created_at", "expires_at", "clock", "data_version", "override_rows", "status")


def _dict(row) -> dict:
    d = dict(zip(_COLS, row))
    d["shared_roles"] = list(d["shared_roles"] or [])
    d["data_version"] = int(d["data_version"] or 0)
    d["override_rows"] = int(d["override_rows"] or 0)
    return d


def _fetch(conn, sid: int) -> dict | None:
    row = conn.execute(_SELECT + " WHERE s.id = %s", [sid]).fetchone()
    return None if row is None else _dict(row)


def _open_info(conn, sid: int) -> dict:
    """Inside scoped(): the scenario row if it is open and visible to the caller, else ScenarioNotFound."""
    if type(sid) is not int or sid <= 0:
        raise ScenarioNotFound(f"scenario {sid} not found")
    d = _fetch(conn, sid)
    if d is None or d["status"] != "open" or not conn.execute(
            "SELECT amarh_scenario_visible(%s)", [sid]).fetchone()[0]:
        raise ScenarioNotFound(f"scenario {sid} not found")
    return d


def _invalidate_graph(sid: int) -> None:
    try:
        from engine.sync import scenario_graph
        scenario_graph.invalidate(sid)
    except Exception:  # noqa: BLE001 - cache only
        pass


# ---------------------------------------------------------------- lifecycle
def create(conn, principal, name: str, *, run_id: str | None = None, parent: int | None = None,
           expires_in_h: float | None = None, shared_roles=(), ontology=None) -> dict:
    """Fork main now (snapshot semantics). Owner = the caller. Raises ScenarioLimit (429) beyond the quotas."""
    pr = _P(principal)
    name = (name or "").strip()
    if not name or len(name) > 200:
        raise ValueError("name is required (max 200 characters)")
    if parent not in (None, 0):
        raise ValueError("forking from another scenario is not supported; scenarios fork main")
    lim = limits()
    ttl = lim["ttl_h"] if expires_in_h is None else float(expires_in_h)
    if not 0 < ttl <= 24 * 365:
        raise ValueError("expires_in_h must be positive (max one year)")
    roles = core._load(ontology).roles
    shared = []
    for r in (shared_roles or ()):
        if r == "sim" or r not in roles:
            raise ValueError(f"unknown role {r!r} in shared_roles")
        if r not in shared:
            shared.append(r)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('scenario-create', 0))")
        # quotas are counted as the loader: a user must not need to see other users' scenarios to be limited
        openq = ("FROM scenario s WHERE NOT EXISTS (SELECT 1 FROM scenario_event e WHERE e.scenario_id = s.id "
                 "AND e.kind IN ('merged','discarded','expired'))")
        if conn.execute(f"SELECT count(*) {openq} AND s.owner_user = %s", [pr.user]).fetchone()[0] >= lim["per_user"]:
            raise ScenarioLimit(f"you already have {lim['per_user']} open scenarios (AMARH_SCN_MAX_PER_USER)")
        if run_id and conn.execute(f"SELECT count(*) {openq} AND s.run_id = %s", [run_id]).fetchone()[0] >= lim["per_run"]:
            raise ScenarioLimit(f"this agent run already opened {lim['per_run']} scenarios (AMARH_SCN_MAX_PER_RUN)")
        if conn.execute(f"SELECT count(*) {openq}").fetchone()[0] >= lim["global"]:
            raise ScenarioLimit(f"{lim['global']} scenarios are open (AMARH_SCN_MAX_GLOBAL)")
        sim_now = conn.execute("SELECT max(sim_time) FROM audit_event WHERE scenario_id = 0 AND status = 'executed' "
                               "AND sim_time IS NOT NULL").fetchone()[0]
        # inserted by the loader (the scoped INSERT ... RETURNING cannot see its own row through the definer
        # policy helpers); owner identity comes from the server-side principal, never from the request body
        row = scenario_admin.create_base(
            conn, name=name, owner_user=pr.user, owner_role=pr.role, shared_roles=shared, run_id=run_id,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl), base_sim_time=sim_now)
        out = _fetch(conn, row["id"])
    conn.commit()
    return out


def get(conn, principal, id: int) -> dict:
    """One scenario of any status the caller may access (history stays readable after merge / discard)."""
    with conn.transaction():
        with scoped(conn, principal):
            d = _fetch(conn, id) if type(id) is int and id > 0 else None
    conn.commit()
    if d is None:
        raise ScenarioNotFound(f"scenario {id} not found")
    return d


def list_(conn, principal, status: str = "open") -> list[dict]:
    """Scenarios the caller may access (owner, admin, shared role). status: open|all|merged|discarded|expired."""
    if status not in ("open", "all", *TERMINAL):
        raise ValueError("status must be open, all, merged, discarded or expired")
    with conn.transaction():
        with scoped(conn, principal):
            rows = [_dict(r) for r in conn.execute(_SELECT + " ORDER BY s.id").fetchall()]
    conn.commit()
    return [d for d in rows if status == "all" or d["status"] == status]


def resolve(conn, principal, id) -> ScenarioCtx:
    """The ONLY way a scenario id becomes a ScenarioCtx: open and visible to the caller, else ScenarioNotFound."""
    if type(id) is not int or id <= 0:
        raise ScenarioNotFound(f"scenario {id} not found")
    with conn.transaction():
        with scoped(conn, principal):
            d = _open_info(conn, id)
    conn.commit()
    return ScenarioCtx(d["id"], d["base_recorded"], d["clock"])


def record_event(conn, principal, scenario_id: int, kind: str, payload: dict | None = None) -> None:
    """Append a 'simulated' / 'proposal' event (owner or admin only; RLS enforces it)."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"event kind must be one of {EVENT_KINDS}")
    pr = _P(principal)
    with conn.transaction():
        with scoped(conn, principal):
            _open_info(conn, scenario_id)
            core._need_scn(conn, scenario_id, write=True)
            conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES (%s,%s,%s,%s::jsonb)",
                         [scenario_id, kind, pr.user, _jdump(payload or {})])
    conn.commit()


def discard(conn, principal, id: int) -> dict:
    """Owner or admin: purge the scenario's rows (audit stays) and record 'discarded'."""
    pr = _P(principal)
    with conn.transaction():
        with scoped(conn, principal):
            _open_info(conn, id)
            core._need_scn(conn, id, write=True)
            conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES (%s,'discarded',%s,"
                         "'{}'::jsonb)", [id, pr.user])
        scenario_admin.purge(conn, id)  # loader-side janitor, after the scope is reset
    conn.commit()
    _invalidate_graph(id)
    return {"id": id, "status": "discarded"}


def expire_due(conn, *, now: datetime | None = None) -> list[int]:
    """Loader-side janitor: mark open scenarios whose ``expires_at`` passed as expired and purge them."""
    with conn.transaction():
        ids = [r[0] for r in conn.execute(
            "SELECT s.id FROM scenario s WHERE s.expires_at IS NOT NULL AND s.expires_at < coalesce(%s, now()) "
            "AND NOT EXISTS (SELECT 1 FROM scenario_event e WHERE e.scenario_id = s.id AND e.kind IN "
            "('merged','discarded','expired')) ORDER BY s.id", [now]).fetchall()]
        for sid in ids:
            conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES (%s,'expired',"
                         "'system:expiry','{}'::jsonb)", [sid])
            scenario_admin.purge(conn, sid)
    conn.commit()
    for sid in ids:
        _invalidate_graph(sid)
    return ids


# ------------------------------------------------------------------ compare
def _snap(row: dict) -> dict:
    drop = set(_STRIP) | {"valid_from", "valid_to", "recorded_from", "recorded_to"}
    return _jsonable({k: v for k, v in row.items() if k not in drop})


def _read_side(conn, keys: list[tuple], sid: int) -> dict:
    """{(kind, type, key_a, key_b): snapshot | None} for the keys, as the scoped caller sees scenario `sid`.
    Objects: property dict (masked, RLS-filtered); links: {"exists": bool}. None = absent or not visible."""
    out: dict[tuple, dict | None] = {}
    by_obj: dict[str, list[UUID]] = {}
    by_link: dict[str, list[tuple[UUID, UUID]]] = {}
    for kind, typ, a, b in keys:
        (by_obj.setdefault(typ, []).append(a) if kind == "obj" else by_link.setdefault(typ, []).append((a, b)))
    for typ, ids in by_obj.items():
        rows = bt.list_objects(conn, typ, scenario_id=sid, where="id = ANY(%s)", params=[ids])
        have = {r["id"]: _snap(r) for r in rows}
        for i in ids:
            out[("obj", typ, i, bt.ZERO_UUID)] = have.get(i)
    for link, pairs in by_link.items():
        srcs = sorted({s for s, _ in pairs})
        if sid:
            w, sp = bt._scn_where(conn, "link", link, sid, None, None)
        else:
            w, sp = bt._asof_clause(None, None)
            w, sp = f"scenario_id = 0 AND {w}", sp
        rows = bt._fetch(conn, bt.link_table(link), f"src = ANY(%s) AND {w}", [srcs, *sp])
        have = {(r["src"], r["dst"]) for r in rows}
        for s, d in pairs:
            out[("link", link, s, d)] = {"exists": (s, d) in have}
    return out


def _side_keys(conn, principal, sid: int) -> set[tuple]:
    if not sid:
        return set()
    with scoped(conn, principal):
        return {(o["kind"], o["type"], o["key_a"], o["key_b"]) for o in bt.list_overrides(conn, sid)}


def compare(conn, principal, a: int, b: int = 0, *, limit: int = 500, ontology=None) -> dict:
    """Diff + KPIs of scenario `a` against `b` (0 = main), both sides read under the caller's RLS and masking.

    Orientation (frozen): each diff row has ``before`` = state on side b and ``after`` = state on side a;
    ``kpis.delta = a - b``. A key the caller cannot see on either side is omitted (no placeholder, no count).
    Candidate keys are the override keys of the scenario side(s): untouched keys are equal by construction.
    """
    if type(a) is not int or a <= 0 or type(b) is not int or b < 0:
        raise ValueError("compare needs a scenario id a > 0 and b >= 0 (0 = main)")
    limit = max(1, min(int(limit), 5000))
    with conn.transaction():
        with scoped(conn, principal):
            ia = _open_info(conn, a)
            ib = _open_info(conn, b) if b else None
        keys = _side_keys(conn, principal, a) | _side_keys(conn, principal, b)
        klist = sorted(keys, key=lambda k: (k[0], k[1], str(k[2]), str(k[3])))
        sides, kp = {}, {}
        for tag, sid in (("a", a), ("b", b)):
            ctx = scenario_admin.ctx_of(conn, sid) if sid else None
            with scoped(conn, principal, ctx):
                sides[tag] = _read_side(conn, klist, sid)
                kp[tag] = kpis.compute(conn, principal, sid)
    conn.commit()
    diff = []
    for k in klist:
        sa, sb = sides["a"].get(k), sides["b"].get(k)
        if k[0] == "link":
            if (sa or {}).get("exists") == (sb or {}).get("exists"):
                continue
        elif sa is None and sb is None or sa == sb:
            continue
        key = str(k[2]) if k[0] == "obj" else f"{k[2]}>{k[3]}"
        op = ("create" if (sb is None or sb == {"exists": False}) else
              "delete" if (sa is None or sa == {"exists": False}) else "modify")
        diff.append({"kind": "object" if k[0] == "obj" else "link", "type": k[1], "key": key, "op": op,
                     "before": sb, "after": sa})
    delta = {m: (kp["a"].get(m) or 0) - (kp["b"].get(m) or 0) for m in kp["a"]}
    return {"a": _public(ia), "b": _public(ib) if ib else {"id": "main"}, "diff": diff[:limit],
            "kpis": {"a": kp["a"], "b": kp["b"], "delta": delta}, "truncated": len(diff) > limit}


def _public(d: dict) -> dict:
    return {k: d[k] for k in ("id", "name", "owner_user", "owner_role", "status")}


# -------------------------------------------------------------------- merge
def _masked(model, role: str, type_: str) -> set[str]:
    r = model.roles.get(role)
    a = r.types.get(type_) if r else None
    return set(a.mask) if a else set()


_NOISE = {"valid_during", "recorded_during", "changed_by", "action_id", "scenario_id", "id", "src", "dst"}


def _current_props(conn, type_: str, id_: UUID) -> dict | None:
    """Main's current version as jsonb (same normalisation as the override `base` snapshot), RLS+masking."""
    row = conn.execute(f"SELECT to_jsonb(r) FROM rd_cur_obj_{bt.snake(type_)} r WHERE r.id = %s", [id_]).fetchone()
    return None if row is None else row[0]


def _conflicts(conn, model, principal_role: str, owner_role: str, overrides: list[dict], relevant: set) -> list[dict]:
    out = []
    for o in overrides:
        kind, typ, a, b = o["kind"], o["type"], o["key_a"], o["key_b"]
        if (kind, typ, a, b) not in relevant:
            continue
        base = o.get("base") or {}
        key = str(a) if kind == "obj" else f"{a}>{b}"
        label = "object" if kind == "obj" else "link"

        def bad(reason):
            out.append({"kind": label, "type": typ, "key": key, "reason": reason})
        if kind == "obj":
            cur = _current_props(conn, typ, a)
            if not base.get("present"):
                if cur is not None:
                    bad("this id was created in the scenario but now exists in main")
                continue
            if cur is None:
                bad("the object no longer exists in main (or is no longer visible to you)")
                continue
            hide = _NOISE | VOLATILE | _masked(model, principal_role, typ) | _masked(model, owner_role, typ)
            was = {k: v for k, v in (base.get("props") or {}).items() if k not in hide}
            now = {k: v for k, v in cur.items() if k not in hide}
            changed = sorted(k for k in set(was) | set(now) if was.get(k) != now.get(k))
            if changed:
                bad(f"main changed since the fork (properties: {', '.join(changed)})")
        else:
            exists = conn.execute(f"SELECT 1 FROM cur_link_{bt.snake(typ)} WHERE src = %s AND dst = %s",
                                  [a, b]).fetchone() is not None
            if exists != bool(base.get("present")):
                bad("the link was added in main since the fork" if exists else "the link was removed in main since the fork")
    return sorted(out, key=lambda c: (c["kind"], c["type"], c["key"]))


def merge(conn, principal, id: int, comment: str = "", *, ontology=None) -> dict:
    """Apply a scenario to main, atomically (ONE transaction; any conflict or failure writes nothing).

    1. owner/admin only; serialised per scenario; the scenario must still be open.
    2. conflict check on every override key touched by a replayed action (see below): the key must still
       equal the fork-time snapshot in main (non-masked, non-volatile properties; links by presence).
    3. replay the scenario's user/agent executions (``source IN api/agent/ui``, ``seq`` order; sim executions,
       rollbacks and the executions they undid are skipped) on main as the MERGING principal with
       ``source='scenario_merge'``, ``run_id='scenario:<id>'``, valid time = now. Approval-gated actions become
       pending proposals (proposer = the merging user) instead of executing.
    4. record 'merged', purge the scenario's rows.
    Returns ``{merged: [{name, action_id | proposal_id}]}``; raises ``MergeConflict(conflicts)``.
    """
    model = core._load(ontology)
    pr = _P(principal)
    now = datetime.now(UTC)
    merged: list[dict] = []
    with conn.transaction():
        with scoped(conn, principal):
            info = _open_info(conn, id)
            core._need_scn(conn, id, write=True)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"scenario-merge:{id}"])
            _open_info(conn, id)  # closed while we waited for the lock?
        owner_role = info["owner_role"]
        # loader-side reads: every execution of the scenario, whoever ran it (an admin may have acted in it)
        overrides = bt.list_overrides(conn, id)
        execs = conn.execute(
            "SELECT action_id, name, params, parent_execution_id, seq FROM audit_event WHERE scenario_id = %s "
            "AND status = 'executed' AND source = ANY(%s) ORDER BY seq", [id, list(REPLAY_SOURCES)]).fetchall()
        undone = {r[0] for r in conn.execute("SELECT original_action_id FROM rollback WHERE scenario_id = %s",
                                             [id]).fetchall()}
        replay = [e for e in execs if e[1] != "rollback" and e[3] is None and e[0] not in undone
                  and e[1] in model.actions]
        diffs = conn.execute("SELECT kind, type, key FROM execution_diff WHERE scenario_id = %s AND action_id = ANY(%s)",
                             [id, [e[0] for e in replay]]).fetchall()
        relevant = set()
        for kind, typ, key in diffs:
            if kind == "object":
                relevant.add(("obj", typ, UUID(key), bt.ZERO_UUID))
            else:
                s_, d_ = key.split(">")
                relevant.add(("link", typ, UUID(s_), UUID(d_)))
        with scoped(conn, principal):
            for o in sorted((o for o in overrides if (o["kind"], o["type"], o["key_a"], o["key_b"]) in relevant),
                            key=lambda o: (o["kind"], o["type"], str(o["key_a"]), str(o["key_b"]))):
                tbl = bt.obj_table(o["type"]) if o["kind"] == "obj" else bt.link_table(o["type"])
                bt._lock(conn, tbl, (o["key_a"],) if o["kind"] == "obj" else (o["key_a"], o["key_b"]), 0)
            conflicts = _conflicts(conn, model, pr.role, owner_role, overrides, relevant)
        if conflicts:
            raise MergeConflict(conflicts)
        for aid_old, name, params, _parent, _seq in replay:
            a = model.actions[name]
            aid = uuid4()
            need_proposal = False
            with scoped(conn, principal):
                try:
                    res = core._do_execute(conn, model, a, params, pr, now, aid, source="scenario_merge",
                                           run_id=f"scenario:{id}", proposal_id=None, tool_trace=None,
                                           on_behalf_of=None, gate=True, sid=0)
                    merged.append({"name": name, "action_id": str(res.action_id)})
                except ApprovalRequired:
                    need_proposal = True
            if need_proposal:
                pid = uuid4()
                core._propose_open(conn, model, a, params, principal, pr, None, 0, pid, name,
                                   f"Merged from scenario {id}" + (f": {comment}" if comment else ""), now,
                                   f"scenario:{id}", None, None, None, None, None)
                merged.append({"name": name, "proposal_id": str(pid)})
        with scoped(conn, principal):
            conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES (%s,'merged',%s,%s::jsonb)",
                         [id, pr.user, _jdump({"comment": comment, "merged": merged})])
        scenario_admin.purge(conn, id)
    conn.commit()
    _invalidate_graph(id)
    return {"merged": merged}
