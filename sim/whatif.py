"""What-if simulation inside a scenario (Phase 5).

`run(conn_factory, scenario_ctx, *, days, seed, injections, ...) -> WhatIfResult`

The world loads the scenario's EFFECTIVE state (scenario overlay over main as of the fork) at the scenario's
sim time (`scenario_state.clock`, else the fork's `base_sim_time`, else the sim epoch), runs `days` sim days and
writes every action through `engine.actions.execute(..., scenario_id=N, source='sim')`, so the overlay, audit
(`scenario_id=N`), no-webhook and no-graph-outbox rules of the action engine apply. The scenario clock
(`scenario_state.clock`) is advanced every sim hour (so `amarh_now()` follows the what-if) and is NOT bounded by
wall-clock now. Same fork + seed + injections gives the same digest (RNG families as in SimRunner).

Injection spec: `{kind, target?, at_h?, duration_h?, magnitude?}`, `at_h` = hours after the fork's sim time
(default 0). kinds: port_closure (duration_h = closure length, default 24), shipment_delay (duration_h = delay
hours), invoice_anomaly, supplier_delay (delays the active shipments billed by the supplier, duration_h =
delay hours), price_shock (Supplier.price_index *= magnitude, default 1.5). A missing target is picked
deterministically from a sorted pool when the injection fires. Injection specs go into the digest.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from engine.actions import ApprovalRequired, CriteriaFailed, PermissionDenied, execute
from engine.actions.core import SIM
from engine.db.principal import Principal

from .runner import _canon, digest_line
from .world import World

KINDS = ("port_closure", "shipment_delay", "invoice_anomaly", "supplier_delay", "price_shock")
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT_DURATION_H = 24.0
DEFAULT_MAGNITUDE = 1.5
ADMIN = Principal("admin", "demo-admin", "")


@dataclass
class WhatIfResult:
    digest: str
    actions: int
    kpis: dict
    sim_now: datetime
    injections: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    last_error: str | None = None


def max_days() -> int:
    return int(os.environ.get("AMARH_SCN_SIM_MAX_DAYS", "90"))


def _normalise(injections, start_hour: int) -> list[dict]:
    out = []
    for i, spec in enumerate(injections or ()):
        spec = dict(spec)
        kind = spec.get("kind")
        if kind not in KINDS:
            raise ValueError(f"injection {i}: kind must be one of {KINDS}")
        at_h = float(spec.get("at_h") or 0.0)
        dur = float(spec["duration_h"]) if spec.get("duration_h") is not None else DEFAULT_DURATION_H
        mag = float(spec["magnitude"]) if spec.get("magnitude") is not None else DEFAULT_MAGNITUDE
        if at_h < 0:
            raise ValueError(f"injection {i}: at_h must be >= 0")
        if not dur > 0:
            raise ValueError(f"injection {i}: duration_h must be > 0")
        if not mag > 0:
            raise ValueError(f"injection {i}: magnitude must be > 0")
        t = spec.get("target")
        target = None if t is None else (t if isinstance(t, uuid.UUID) else uuid.UUID(str(t)))
        out.append({"kind": kind, "target": target, "at_h": at_h, "abs_h": start_hour + int(-(-at_h // 1)),
                    "duration_h": dur, "magnitude": mag})
    return out


def start_time(conn, ctx, epoch: datetime = EPOCH) -> datetime:
    """The scenario's sim 'now': its clock if a what-if already ran, else the fork's base_sim_time, else the epoch."""
    if ctx.clock is not None:
        t = ctx.clock
    else:
        row = conn.execute("SELECT base_sim_time FROM scenario WHERE id = %s", [ctx.id]).fetchone()
        t = row[0] if row and row[0] is not None else epoch
    conn.commit()
    t = t if t.tzinfo else t.replace(tzinfo=UTC)
    return epoch + timedelta(hours=int((t - epoch).total_seconds() // 3600))


def run(conn_factory, scenario_ctx, *, days, seed, injections=(), ontology=None, schema=None, dsn=None,
        principal: Principal | None = None, epoch: datetime = EPOCH, kpi_principal: Principal | None = None
        ) -> WhatIfResult:
    """Run a what-if in `scenario_ctx`. `conn_factory()` returns a fresh owner-side connection (the caller's
    job runner passes `lambda: connect(dsn, schema=schema)`); `schema`/`dsn` are accepted for symmetry and used
    only when `conn_factory` is None. `principal`/`kpi_principal`: whose RLS the returned KPIs use (default
    admin). Raises ValueError for days outside 1..AMARH_SCN_SIM_MAX_DAYS or a bad injection."""
    cap = max_days()
    days = float(days)
    if not 0 < days <= cap:
        raise ValueError(f"days must be in 1..{cap} (AMARH_SCN_SIM_MAX_DAYS)")
    if conn_factory is None:
        from engine.db import connect
        conn_factory = lambda: connect(dsn, schema=schema)  # noqa: E731
    sid = int(scenario_ctx.id)
    if sid <= 0:
        raise ValueError("what-if needs a scenario id > 0")
    kp = kpi_principal or principal or ADMIN
    run_id = f"scenario:{sid}:{seed}"
    conn = conn_factory()
    try:
        start = start_time(conn, scenario_ctx, epoch)
        start_h = int((start - epoch).total_seconds() // 3600)
        base = conn.execute("SELECT base_sim_time FROM scenario WHERE id=%s", [sid]).fetchone()
        conn.commit()
        base_t = base[0] if base and base[0] is not None else epoch
        end_h = start_h + int(round(days * 24))
        if (epoch + timedelta(hours=end_h) - base_t) > timedelta(days=cap):
            raise ValueError(f"the scenario would run beyond {cap} sim days from its fork (AMARH_SCN_SIM_MAX_DAYS)")
        specs = _normalise(injections, start_h)

        sha = hashlib.sha256()
        state = {"actions": 0, "err": None}

        def rollback():
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

        def apply(name, params, valid_at):
            try:
                res = execute(conn, name, params, SIM, valid_at=valid_at, source="sim", run_id=run_id,
                              scenario_id=sid, ontology=ontology)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                rollback()
                raise
            except (ValueError, PermissionDenied, CriteriaFailed, ApprovalRequired, psycopg.Error) as e:
                rollback()
                state["err"] = f"{name} at {valid_at.isoformat()}: {e}"
                return None
            sha.update(digest_line(valid_at, name, params))
            state["actions"] += 1
            return res

        w = World(seed, epoch, apply, start_hour=start_h, scenario_id=sid)
        w.load(conn)
        conn.commit()
        w.start()
        w.env.run(until=start_h + 0.5)
        for s in specs:           # injections are part of the run's identity
            sha.update(f"inject|{s['abs_h']}|{s['kind']}|{s['target']}|{s['duration_h']}|{s['magnitude']}\n".encode())
            w.schedule(s["kind"], s["target"], s["abs_h"], s["duration_h"], s["magnitude"])

        def set_clock(h: int) -> None:
            conn.execute("UPDATE scenario_state SET clock = %s WHERE scenario_id = %s",
                         [epoch + timedelta(hours=h), sid])
            conn.commit()

        h = start_h
        while h < end_h:
            h += 1
            w.env.run(until=h + 0.5)
            set_clock(h)
        sim_now = epoch + timedelta(hours=h)
        from engine import kpis as kpis_mod
        k = kpis_mod.compute(conn, kp, scenario_id=sid)
        conn.commit()
        return WhatIfResult(digest=sha.hexdigest(), actions=state["actions"], kpis=k, sim_now=sim_now,
                            injections=[{**s, "target": None if s["target"] is None else str(s["target"])}
                                        for s in specs],
                            skipped=list(w.skipped), stats=dict(w.stats), last_error=state["err"])
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
