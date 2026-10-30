"""SimRunner: owns a DB connection, a World and (optionally) a background thread.

Error policy
  * a failed `execute` (validation / permission / criteria / data error): rollback, record `last_error`,
    skip that action (it is not part of the digest) and continue;
  * connection-level failures (psycopg OperationalError/InterfaceError) or unexpected exceptions:
    rollback, record `last_error`; in the background thread the runner then stops
    (`running` False, `last_error` set); direct calls (step/run_until) re-raise.
Resume
  * on construction, if `audit_event` holds rows with source='sim' and run_id == this run_id that are
    NEWER than the last `reset` audit event (a reset keeps the audit trail but starts a new epoch),
    sim_now resumes at max(sim_time) and in-flight shipments, open invoices and closed ports are
    restored from the database (RNG streams restart from a seed derived from (seed, resume hour);
    closed ports reopen 24 h after the resume point). Otherwise sim_now = epoch.
  * `reset()` ignores history: sim_now = epoch, digest/counters/injections cleared, entities
    re-read. Re-seeding the database is the caller's job.
sim_now never passes wall-clock now (`capped` in state() reports it).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from engine.actions import ApprovalRequired, CriteriaFailed, PermissionDenied, execute
from engine.db import connect
from engine.db.principal import Principal

from .world import World

# The system principal of the simulation. user='sim' (not the contract's 'system:sim') keeps the
# Phase 2 `actor` / `changed_by` values that the audit queries and tests rely on.
SIM_PRINCIPAL = Principal("sim", "sim", "")
SPEEDS = ("paused", "1", "6", "24")
KINDS = ("port_closure", "shipment_delay", "invoice_anomaly")


def _canon(params: dict) -> str:
    return json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))


def digest_line(valid_at: datetime, name: str, params: dict) -> bytes:
    return f"{valid_at.astimezone(UTC).isoformat()}|{name}|{_canon(params)}\n".encode()


class SimRunner:
    def __init__(self, dsn: str | None = None, schema: str | None = None, seed: int = 1,
                 epoch: datetime = datetime(2026, 1, 1, tzinfo=UTC), pipeline=None,
                 run_id: str | None = None, **world_kw):
        self.dsn, self.schema, self.seed = dsn, schema, seed
        self.epoch = epoch if epoch.tzinfo else epoch.replace(tzinfo=UTC)
        self.pipeline = pipeline
        self.run_id = run_id or f"sim-{seed}"
        self.world_kw = world_kw
        self._lock = threading.RLock()       # serialises conn + world access
        self._slock = threading.Lock()       # guards the state snapshot
        self.conn = connect(dsn, schema)
        self._speed = "paused"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self.last_error: str | None = None
        self._achieved: float | None = None
        self._capped = False
        self.actions = 0
        self.injections: list[dict] = []
        self._reset_state(resume=True)

    # ----------------------------------------------------------- bookkeeping
    def _reset_state(self, resume: bool) -> None:
        h = 0
        if resume:
            row = self.conn.execute(
                "SELECT max(sim_time) FROM audit_event WHERE run_id=%s AND source='sim' "
                "AND seq > coalesce((SELECT max(seq) FROM audit_event WHERE name='reset'), 0)",
                [self.run_id]).fetchone()
            self.conn.commit()
            if row and row[0] is not None:
                h = max(0, int((row[0] - self.epoch).total_seconds() // 3600))
        self._h = h
        self.start_hour = h
        self.world: World | None = None
        self._sha = hashlib.sha256()
        self.actions = 0
        self.injections = []
        self._snap_update()

    def _snap_update(self) -> None:
        with self._slock:
            self._snap_h, self._snap_actions = self._h, self.actions
            self._snap_active = len(self.world.active) if self.world else 0

    def _ensure_world(self) -> World:
        if self.world is None:
            w = World(self.seed, self.epoch, self._apply, start_hour=self._h, **self.world_kw)
            w.load(self.conn)
            self.conn.commit()
            w.start()
            self.world = w
            w.env.run(until=self._h + 0.5)       # events scheduled exactly at the start hour
        return self.world

    def _apply(self, name: str, params: dict, valid_at: datetime):
        try:
            res = execute(self.conn, name, params, SIM_PRINCIPAL, valid_at=valid_at, source="sim",
                          run_id=self.run_id)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            self._rollback()
            raise
        except (ValueError, PermissionDenied, CriteriaFailed, ApprovalRequired, psycopg.Error) as e:
            self._rollback()
            self.last_error = f"{name} at {valid_at.isoformat()}: {e}"
            return None
        self._sha.update(digest_line(valid_at, name, params))
        self.actions += 1
        return res

    def _rollback(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass

    def event_digest(self) -> str:
        with self._lock:
            return self._sha.copy().hexdigest()

    # ---------------------------------------------------------------- state
    @property
    def sim_now(self) -> datetime:
        return self.epoch + timedelta(hours=self._snap_h)

    def _wall_cap_hour(self) -> int:
        return int((datetime.now(UTC) - self.epoch).total_seconds() // 3600)

    def state(self) -> dict:
        with self._slock:
            h, n, act = self._snap_h, self._snap_actions, self._snap_active
        return {"sim_now": (self.epoch + timedelta(hours=h)).isoformat(), "epoch": self.epoch.isoformat(),
                "speed": self._speed, "running": self._running, "seed": self.seed, "tick": h,
                "achieved_hours_per_s": self._achieved, "last_error": self.last_error,
                "run_id": self.run_id, "actions": n, "active_shipments": act, "capped": self._capped}

    def set_speed(self, speed) -> dict:
        speed = str(speed)
        if speed not in SPEEDS:
            raise ValueError(f"speed must be one of {SPEEDS}, got {speed!r}")
        self._speed = speed
        self._achieved = None
        return self.state()

    # ------------------------------------------------------------- advancing
    def _advance(self, target_h: int, sync_every: int | None = None) -> int:
        """Advance to `target_h` (capped at wall-clock). Returns hours advanced."""
        with self._lock:
            w = self._ensure_world()
            cap = self._wall_cap_hour()
            self._capped = target_h > cap
            target_h = min(target_h, cap)
            start = self._h
            try:
                while self._h < target_h:
                    nxt = self._h + 1
                    w.env.run(until=nxt + 0.5)
                    self._h = nxt
                    self._snap_update()
                    if sync_every and (nxt - start) % sync_every == 0:
                        self._sync()
                if self._h > start:
                    self._sync()
            except Exception as e:
                self._rollback()
                self.last_error = f"{type(e).__name__}: {e}"
                raise
            return self._h - start

    def _sync(self) -> None:
        if self.pipeline is not None:
            self.pipeline.sync()
            self.pipeline.maybe_reason()

    def step(self) -> dict:
        """Advance exactly one sim hour (no-op at the wall-clock cap)."""
        self._advance(self._h + 1)
        return self.state()

    def run_until(self, t) -> dict:
        """Unthrottled advance to sim time `t` (datetime, iso string or hours since epoch)."""
        if isinstance(t, (int, float)):
            h = int(t)
        else:
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=UTC)
            h = int((t - self.epoch).total_seconds() // 3600)
        self._advance(h, sync_every=24)
        return self.state()

    # ------------------------------------------------------------- injection
    def inject(self, kind: str, target=None, at=None, duration_h: float = 24.0) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        duration_h = float(duration_h)
        if not duration_h > 0:
            raise ValueError("duration_h must be > 0")
        with self._lock:
            w = self._ensure_world()
            if at is None:
                at_h = self._h
            else:
                if isinstance(at, str):
                    at = datetime.fromisoformat(at.replace("Z", "+00:00"))
                at = at if at.tzinfo else at.replace(tzinfo=UTC)
                secs = (at - self.epoch).total_seconds()
                at_h = int(-(-secs // 3600))
            if at_h < self._h:
                raise ValueError("cannot inject in the past of sim_now")
            if at_h > self._wall_cap_hour():
                raise ValueError("cannot inject beyond wall-clock now")
            pool = {"port_closure": sorted(p for p in w.ports if p not in w.closed),
                    "shipment_delay": sorted(w.active),
                    "invoice_anomaly": sorted(i.id for i in w.invoices.values() if i.status == "open")}[kind]
            allowed = {"port_closure": w.ports, "shipment_delay": w.active,
                       "invoice_anomaly": w.invoices}[kind]
            if target is None:
                if not pool:
                    raise ValueError(f"no candidate target for {kind}")
                target = pool[w.rng["inject"].randrange(len(pool))]
            else:
                target = target if isinstance(target, uuid.UUID) else uuid.UUID(str(target))
                if target not in allowed:
                    raise ValueError(f"unknown target {target} for {kind}")
            w.schedule(kind, target, at_h, duration_h)
            rec = {"kind": kind, "target": str(target), "at": (self.epoch + timedelta(hours=at_h)).isoformat(),
                   "duration_h": duration_h}
            self.injections.append(rec)
            return dict(rec)

    def reset(self) -> dict:
        with self._lock:
            self._reset_state(resume=False)
            self.last_error = None
            self._achieved = None
            self._snap_update()
        return self.state()

    # ----------------------------------------------------------------- thread
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="sim-runner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=30)
        self._running = False

    def close(self) -> None:
        self.stop()
        with self._lock:
            self.conn.close()

    def _loop(self) -> None:
        try:
            t0, done, adv_total, last_speed = time.monotonic(), 0.0, 0, self._speed
            while not self._stop.is_set():
                speed = self._speed
                if speed != last_speed or speed == "paused":
                    t0, done, adv_total, last_speed = time.monotonic(), 0.0, 0, speed
                    if speed == "paused":
                        self._stop.wait(0.05)
                        continue
                due = int((time.monotonic() - t0) * float(speed) - done)
                if due >= 1:
                    adv = self._advance(self._h + due)
                    done += due
                    el = time.monotonic() - t0
                    adv_total += adv
                    self._achieved = round(adv_total / el, 3) if el > 0 else None
                    if adv < due:                      # capped at wall-clock now
                        self._stop.wait(1.0)
                else:
                    self._stop.wait(0.02)
        except Exception as e:   # recorded in _advance too; stop and report
            self.last_error = f"runner stopped: {type(e).__name__}: {e}"
        finally:
            self._running = False
