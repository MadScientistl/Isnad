"""SimPy world: processes that generate supply-chain activity by calling apply_action.

Time: env.now is in sim hours since the epoch. All process events fire at integer hours;
the logical hour of a write is floor(env.now) (the runner runs to H+0.5 so that everything
scheduled at hour H is done when sim_now == H).  The world never touches the database
except through the `apply(name, params, valid_at)` callback (-> ActionResult | None) and
read-only bitemporal reads in `load()`.
"""
from __future__ import annotations

import hashlib
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import simpy

from engine.db import bitemporal as bt

from .geo import Path

VESSEL_KMH = 35.0
MOVE_EVERY_H = 6
SHIPMENTS_PER_DAY = 3.0
MAX_ACTIVE = 30
HAZARD_PER_H = 0.0008          # per in-transit shipment per hour
CLOSURE_MEAN_GAP_H = 96.0      # mean hours between random port closures
ANOMALY_MEAN_GAP_H = 72.0
FAMILIES = ("shipments", "movement", "delays", "closures", "invoices", "anomalies", "inject")
NS = uuid.UUID("6f1c2b7e-0d3a-4c55-9a51-5a1d0c0f5e11")


def family_rng(seed: int, family: str, gen: int = 0) -> random.Random:
    key = f"{seed}:{family}" + (f":{gen}" if gen else "")
    return random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big"))


@dataclass
class Shipment:
    id: uuid.UUID
    ref: str
    path: Path
    ports: list[uuid.UUID]
    port_fracs: list[float]
    cargo_value: float
    status: str = "in_transit"
    progress: float = 0.0
    pidx: int = 0                     # index of next unpassed route port
    hold_until: float = 0.0           # no progress before this hour (delay)
    notified: bool = False            # blocked-by-closure delay already issued
    last_pos: tuple | None = None


@dataclass
class Invoice:
    id: uuid.UUID
    ref: str
    amount: float
    shipment: uuid.UUID
    supplier: uuid.UUID
    status: str = "open"


@dataclass
class Fac:
    id: uuid.UUID
    kind: str                         # Port | Warehouse
    name: str
    xy: tuple[float, float]


def _xy(o: dict) -> tuple[float, float] | None:
    g = o.get("location")
    if isinstance(g, dict) and g.get("coordinates"):
        return (float(g["coordinates"][0]), float(g["coordinates"][1]))
    if o.get("lon") is not None and o.get("lat") is not None:
        return (float(o["lon"]), float(o["lat"]))
    return None


def point(xy) -> dict:
    return {"type": "Point", "coordinates": [xy[0], xy[1]]}


class World:
    def __init__(self, seed: int, epoch: datetime, apply: Callable, *, start_hour: int = 0,
                 max_active: int = MAX_ACTIVE, shipments_per_day: float = SHIPMENTS_PER_DAY):
        self.seed, self.epoch, self._apply = seed, epoch, apply
        self.start_hour, self.max_active = start_hour, max_active
        self.gen = start_hour          # id-generation tag: resumed runs never reuse ids
        self.rate_h = shipments_per_day / 24.0
        self.env = simpy.Environment(initial_time=start_hour)
        self.rng = {f: family_rng(seed, f, self.gen) for f in FAMILIES}
        self.counters: dict[str, int] = {}
        self.ports: dict[uuid.UUID, Fac] = {}
        self.warehouses: dict[uuid.UUID, Fac] = {}
        self.carriers: list[uuid.UUID] = []
        self.suppliers: list[uuid.UUID] = []
        self.active: dict[uuid.UUID, Shipment] = {}
        self.invoices: dict[uuid.UUID, Invoice] = {}
        self.closed: dict[uuid.UUID, float] = {}     # port -> reopen hour
        self.stats: dict[str, int] = {}
        self.skipped: list[str] = []

    # ------------------------------------------------------------------ utils
    def hour(self) -> int:
        return int(math.floor(self.env.now))

    def when(self, h: int | None = None) -> datetime:
        return self.epoch + timedelta(hours=self.hour() if h is None else h)

    def new_id(self, kind: str) -> uuid.UUID:
        n = self.counters[kind] = self.counters.get(kind, 0) + 1
        return uuid.uuid5(NS, f"{self.seed}:{self.gen}:{kind}:{n}")

    def do(self, name: str, params: dict):
        self.stats[name] = self.stats.get(name, 0) + 1
        return self._apply(name, params, self.when())

    # ------------------------------------------------------------------- load
    def load(self, conn) -> None:
        """Read entities as of sim_now (restores in-flight state when resuming)."""
        at = self.when(self.start_hour)

        def rows(t):
            r = bt.list_objects(conn, t, as_of_valid=at)
            return r or (bt.list_objects(conn, t) if self.start_hour == 0 else r)

        for t, store, kind in (("Port", self.ports, "Port"), ("Warehouse", self.warehouses, "Warehouse")):
            for o in rows(t):
                xy = _xy(o)
                if xy:
                    store[o["id"]] = Fac(o["id"], kind, o.get("name") or str(o["id"]), xy)
        self.carriers = [o["id"] for o in rows("Carrier")]
        self.suppliers = [o["id"] for o in rows("Supplier")]
        if not self.start_hour:
            return
        for o in bt.list_objects(conn, "Port", as_of_valid=at):
            if o.get("status") == "closed" and o["id"] in self.ports:
                self.closed[o["id"]] = self.start_hour + 24.0
        fac = {**self.ports, **self.warehouses}
        for s in bt.list_objects(conn, "Shipment", as_of_valid=at):
            if s.get("status") not in ("in_transit", "delayed"):
                continue
            sid = s["id"]
            o = [fac[i] for i in bt.neighbours(conn, "ships_from", sid, "out", as_of_valid=at) if i in fac]
            d = [fac[i] for i in bt.neighbours(conn, "ships_to", sid, "out", as_of_valid=at) if i in fac]
            if not o or not d:
                continue
            vias = [self.ports[i] for i in bt.neighbours(conn, "ships_via", sid, "out", as_of_valid=at)
                    if i in self.ports]
            vias.sort(key=lambda p: Path([o[0].xy, p.xy]).total)
            path = Path([o[0].xy, *[p.xy for p in vias], d[0].xy])
            sh = Shipment(sid, s["ref"], path, [p.id for p in vias],
                          [path.frac(i + 1) for i in range(len(vias))], float(s.get("cargo_value") or 0),
                          status=s["status"], progress=float(s.get("progress") or 0),
                          notified=s["status"] == "delayed")
            sh.pidx = sum(1 for f in sh.port_fracs if f < sh.progress - 1e-9)
            self.active[sid] = sh
        for inv in bt.list_objects(conn, "Invoice", as_of_valid=at):
            if inv.get("status") != "open":
                continue
            sh = bt.neighbours(conn, "invoices_for", inv["id"], "out", as_of_valid=at)
            su = bt.neighbours(conn, "billed_by", inv["id"], "out", as_of_valid=at)
            if sh and su:
                self.invoices[inv["id"]] = Invoice(inv["id"], inv["ref"], float(inv["amount"]), sh[0], su[0])

    # -------------------------------------------------------------- processes
    def start(self) -> None:
        env = self.env
        env.process(self._shipment_gen())
        env.process(self._movement())
        env.process(self._hazards())
        env.process(self._closures())
        env.process(self._anomalies())
        for pid, until in sorted(self.closed.items()):
            env.process(self._reopen_later(pid, until))
        for inv in sorted(self.invoices.values(), key=lambda i: str(i.id)):
            env.process(self._pay(inv, self._pay_lag()))

    def _shipment_gen(self):
        rng = self.rng["shipments"]
        while True:
            yield self.env.timeout(round(rng.expovariate(self.rate_h)))
            if len(self.active) < self.max_active:
                self.create_shipment()

    def create_shipment(self):
        rng = self.rng["shipments"]
        facs = sorted([*self.ports.values(), *self.warehouses.values()], key=lambda f: str(f.id))
        whs = sorted(self.warehouses.values(), key=lambda f: str(f.id))
        if not whs or not self.carriers or not self.suppliers or len(facs) < 2:
            return None
        origin = rng.choice(facs)
        dest = rng.choice([w for w in whs if w.id != origin.id] or [None])
        carrier = rng.choice(self.carriers)
        supplier = rng.choice(self.suppliers)
        value = round(rng.lognormvariate(12.0, 0.7), 2)    # median about 160k
        nports = rng.choice([1, 1, 2])
        if dest is None:
            return None
        direct = Path([origin.xy, dest.xy]).total
        cands = sorted((Path([origin.xy, p.xy, dest.xy]).total - direct, str(p.id), p)
                       for p in self.ports.values() if p.id != origin.id)
        pool = [c[2] for c in cands[:3]]
        vias = rng.sample(pool, min(nports, len(pool)))
        vias.sort(key=lambda p: Path([origin.xy, p.xy]).total)
        path = Path([origin.xy, *[p.xy for p in vias], dest.xy])
        if path.total < 1.0:
            return None
        sid = self.new_id("Shipment")
        ref = f"SH-{str(sid)[:8].upper()}"
        eta = self.when() + timedelta(hours=path.total / VESSEL_KMH)
        res = self.do("create_object", {"type": "Shipment", "id": sid, "props": {
            "ref": ref, "eta": eta, "status": "planned", "position": point(origin.xy),
            "progress": 0.0, "cargo_value": value, "delay_hours": 0.0}})
        if res is None:
            return None
        links = [("ships_from", origin.id), ("ships_to", dest.id), ("operated_by", carrier),
                 *[("ships_via", p.id) for p in vias]]
        for link, dst in links:
            self.do("create_link", {"link": link, "src": sid, "dst": dst})
        self.do("ship_depart", {"shipment": sid})
        sh = Shipment(sid, ref, path, [p.id for p in vias], [path.frac(i + 1) for i in range(len(vias))],
                      value, last_pos=origin.xy)
        self.active[sid] = sh
        self.issue_invoice(sh, supplier, value)
        return sh

    def issue_invoice(self, sh: Shipment, supplier, value, ref=None, anomaly_dup=False, amount=None):
        rng = self.rng["invoices"]
        inv_id = self.new_id("Invoice")
        draw = rng.uniform(0.6, 0.95)
        amount = amount if amount is not None else round(value * draw, 2)
        res = self.do("issue_invoice", {
            "shipment": sh.id, "supplier": supplier, "amount": amount, "currency": "USD",
            "due": self.when() + timedelta(days=30), "invoice_id": inv_id,
            **({"ref": ref} if ref else {})})
        if res is None:
            return None
        inv = Invoice(inv_id, ref or f"INV-{str(inv_id)[:8].upper()}", amount, sh.id, supplier)
        self.invoices[inv_id] = inv
        if not anomaly_dup:
            self.env.process(self._pay(inv, self._pay_lag()))
        return inv

    def _pay_lag(self) -> int:
        rng = self.rng["invoices"]
        if rng.random() < 0.25:
            return rng.randint(2, 8) * 24
        return rng.randint(10, 30) * 24

    def _pay(self, inv: Invoice, lag_h: int):
        yield self.env.timeout(lag_h)
        if inv.status == "open":
            if self.do("pay_invoice", {"invoice": inv.id}) is not None:
                inv.status = "paid"

    # movement ---------------------------------------------------------------
    def _movement(self):
        step = MOVE_EVERY_H * VESSEL_KMH
        while True:
            yield self.env.timeout(MOVE_EVERY_H - (self.env.now % MOVE_EVERY_H) if self.env.now % MOVE_EVERY_H
                                   else MOVE_EVERY_H)
            now = self.env.now
            for sh in list(self.active.values()):
                if now < sh.hold_until:
                    continue
                dp = step / sh.path.total
                new = sh.progress + dp
                if sh.pidx < len(sh.ports):
                    f, pid = sh.port_fracs[sh.pidx], sh.ports[sh.pidx]
                    if new >= f:
                        if pid in self.closed:
                            new = f
                            if not sh.notified:
                                sh.notified = True
                                left = max(1.0, self.closed[pid] - now)
                                self.do("delay_shipment", {"shipment": sh.id, "delay_hours": float(left),
                                                           "reason": "port closed ahead"})
                                sh.status = "delayed"
                        else:
                            sh.pidx += 1
                            sh.notified = False
                if new >= 1.0 - 1e-9:
                    self.do("ship_position", {"shipment": sh.id, "position": point(sh.path.pts[-1]),
                                              "progress": 1.0})
                    self.do("ship_arrive", {"shipment": sh.id})
                    del self.active[sh.id]
                    continue
                if new > sh.progress + 1e-12:
                    sh.progress = new
                    xy = sh.path.at(new)
                    self.do("ship_position", {"shipment": sh.id, "position": point(xy),
                                              "progress": round(new, 6)})

    def _hazards(self):
        rng = self.rng["delays"]
        while True:
            yield self.env.timeout(1)
            for sh in list(self.active.values()):
                if rng.random() < HAZARD_PER_H:       # draw for every shipment: stable stream
                    h = float(max(1, round(rng.lognormvariate(math.log(12), 0.6))))
                    self.delay(sh, h, "weather")

    def delay(self, sh: Shipment, hours: float, reason: str) -> bool:
        if self.do("delay_shipment", {"shipment": sh.id, "delay_hours": float(hours), "reason": reason}) is None:
            return False
        sh.status = "delayed"
        sh.hold_until = max(sh.hold_until, self.env.now) + hours
        return True

    # closures ---------------------------------------------------------------
    def _closures(self):
        rng = self.rng["closures"]
        while True:
            yield self.env.timeout(max(1, round(rng.expovariate(1 / CLOSURE_MEAN_GAP_H))))
            open_ports = sorted(p for p in self.ports if p not in self.closed)
            if not open_ports:
                continue
            port = rng.choice(open_ports)
            dur = int(min(120, max(24, round(rng.lognormvariate(math.log(60), 0.5)))))
            reason = rng.choice(["storm", "strike", "congestion", "security incident"])
            self.env.process(self.closure(port, dur, reason))

    def closure(self, port, dur_h: float, reason: str):
        if port in self.closed or port not in self.ports:
            self.skipped.append(f"close_port {port}: already closed or unknown")
            return
        dur_h = max(1.0, float(dur_h))
        if self.do("close_port", {"port": port, "reason": reason, "duration_h": dur_h,
                                  "event_id": self.new_id("Event")}) is None:
            return
        until = self.env.now + dur_h
        self.closed[port] = until
        yield from self._reopen_later(port, until)

    def _reopen_later(self, port, until: float):
        yield self.env.timeout(max(0, until - self.env.now))
        self.do("reopen_port", {"port": port})
        self.closed.pop(port, None)

    # anomalies --------------------------------------------------------------
    def _anomalies(self):
        rng = self.rng["anomalies"]
        while True:
            yield self.env.timeout(max(1, round(rng.expovariate(1 / ANOMALY_MEAN_GAP_H))))
            cands = sorted(str(i.id) for i in self.invoices.values() if i.status == "open")
            if cands:
                kind = rng.choice(["duplicate", "amount_mismatch", "missing_approval"])
                self.anomaly(self.invoices[uuid.UUID(rng.choice(cands))], kind)

    def anomaly(self, inv: Invoice, kind: str) -> bool:
        if inv.status != "open":
            self.skipped.append(f"anomaly {kind} on {inv.id}: invoice not open")
            return False
        if kind == "duplicate":
            sh = self.active.get(inv.shipment) or Shipment(inv.shipment, "", Path([(0, 0), (0, 1)]), [], [], inv.amount)
            dup = self.issue_invoice(sh, inv.supplier, inv.amount, ref=inv.ref, anomaly_dup=True, amount=inv.amount)
            if dup is None:
                return False
            ok = self.do("flag_invoice", {"invoice": dup.id, "anomaly": f"duplicate of {inv.ref}"})
            if ok is not None:
                dup.status = "flagged"
            return ok is not None
        text = (f"amount mismatch: invoiced {inv.amount:.2f}, expected {inv.amount * 0.92:.2f}"
                if kind == "amount_mismatch" else "missing approval")
        ok = self.do("flag_invoice", {"invoice": inv.id, "anomaly": text})
        if ok is not None:
            inv.status = "flagged"
        return ok is not None

    # injections -------------------------------------------------------------
    def schedule(self, kind: str, target, at_hour: int, duration_h: float) -> None:
        self.env.process(self._injected(kind, target, at_hour, duration_h))

    def _injected(self, kind, target, at_hour, duration_h):
        yield self.env.timeout(max(0.0, at_hour - self.env.now))
        if kind == "port_closure":
            yield from self.closure(target, duration_h, "injected")
        elif kind == "shipment_delay":
            sh = self.active.get(target)
            if sh is None:
                self.skipped.append(f"shipment_delay {target}: shipment not active")
            else:
                self.delay(sh, float(duration_h), "injected")
        elif kind == "invoice_anomaly":
            inv = self.invoices.get(target)
            if inv is None:
                self.skipped.append(f"invoice_anomaly {target}: unknown invoice")
            else:
                self.anomaly(inv, self.rng["inject"].choice(["duplicate", "amount_mismatch", "missing_approval"]))
