/**
 * Mock API layer (development and browser verification only).
 *
 * Enabled with `VITE_MOCK=1` (e.g. `cd web && VITE_MOCK=1 npm run dev`). `installMock(base)` replaces
 * `window.fetch` for requests whose URL starts with the API base and answers them from a deterministic
 * in-memory world (no randomness): 8 ports, 4 warehouses, 14 shipments, 6 invoices, suppliers, carriers,
 * closure events. It implements every Phase 2 endpoint with the frozen response shapes
 * (`/sim/state`, `/sim/control`, `/sim/inject`, `/events`, `/geo/{type}`, `/stats/summary`, `/stats/series`,
 * `/graph`) plus the older explorer routes (`/types`, `/objects/...`, `/neighbours/...`). The mock sim clock
 * advances by the chosen speed every wall second; `as_of` is honoured everywhere; inferred graph edges are
 * returned only when `as_of` is absent or "live". Every request is appended to `window.__mockLog`
 * ({method, path, query, role, body}) so tests can assert what the UI sent. The real fetch path is untouched
 * when the flag is off.
 */
import * as gov from "./gov";
import { agentRoute } from "./agent";
import * as scn from "./scenarios";
import * as ing from "./ingest";
import { makeWorld, DEFAULT_NAMES } from "./workspace";
const H = 3600000;
const EPOCH = Date.parse("2026-01-01T00:00:00Z");
const iso = (h: number) => new Date(EPOCH + h * H).toISOString();
const uid = (k: string, i: number) => `${{ port: "a1", wh: "b2", ship: "c3", inv: "d4", sup: "e5", car: "f6", ev: "07" }[k]}000000-0000-4000-8000-${String(i).padStart(12, "0")}`;

const PORTS: [string, number, number][] = [["Rotterdam", 4.4, 51.9], ["Singapore", 103.8, 1.26], ["Shanghai", 121.8, 31.2], ["Los Angeles", -118.2, 33.7], ["Dubai", 55.0, 25.0], ["Santos", -46.3, -23.9], ["Durban", 31.0, -29.9], ["Mumbai", 72.9, 18.9]];
const WHS: [string, number, number][] = [["Warehouse Frankfurt", 8.7, 50.1], ["Warehouse Chicago", -87.6, 41.9], ["Warehouse Nairobi", 36.8, -1.3], ["Warehouse Jakarta", 106.8, -6.2]];
const CARRIERS = ["Maersk-like", "Hapag-like", "Cosco-like"];
const NSHIP = 14;
const pt = (c: [number, number]) => ({ type: "Point", coordinates: c });

interface Win { target: string; start: number; end: number; kind: "port" | "ship" | "inv" }
let base: Win[] = [
  { target: uid("port", 3), start: 300, end: 420, kind: "port" }, { target: uid("port", 5), start: 100, end: 160, kind: "port" },
  { target: uid("inv", 0), start: 200, end: 280, kind: "inv" }, { target: uid("inv", 2), start: 280, end: 360, kind: "inv" }, { target: uid("inv", 4), start: 360, end: 440, kind: "inv" },
];
for (let i = 2; i < NSHIP; i += 5) { const d = dep(i), u = dur(i); base.push({ target: uid("ship", i), start: d + u * 0.4, end: d + u * 0.4 + 60, kind: "ship" }); }
function dep(i: number) { return 24 + i * 30; }
function dur(i: number) { return 96 + ((i * 37) % 120); }

const paid = new Set<string>();
let curRole = "admin";
const st = { simH: 480, speed: "paused" as "paused" | "1" | "6" | "24", tick: 0, injected: [] as Win[], priceMult: 1 };
const wins = () => [...base, ...st.injected];
/* ---- scenario overlay: the mutable world (windows, paid set, clock) can be swapped for a scenario's private copy ---- */
export interface Snap { base: Win[]; injected: Win[]; paid: string[]; simH: number; priceMult: number }
const cp = (w: Win[]) => w.map((x) => ({ ...x }));
const getSnap = (): Snap => ({ base: cp(base), injected: cp(st.injected), paid: [...paid], simH: st.simH, priceMult: st.priceMult });
const setSnap = (s: Snap) => { base = s.base; st.injected = s.injected; paid.clear(); s.paid.forEach((x) => paid.add(x)); st.simH = s.simH; st.priceMult = s.priceMult; };
let savedMain: Snap | null = null;
function withSnap<T>(s: Snap, fn: () => T): T {
  const prevBase = base, prevInj = st.injected, prevPaid = [...paid], prevH = st.simH, prevPm = st.priceMult, prevSaved = savedMain;
  savedMain = savedMain ?? { base: prevBase, injected: prevInj, paid: prevPaid, simH: prevH, priceMult: prevPm };
  setSnap(s);
  try { return fn(); }
  finally { s.base = base; s.injected = st.injected; s.paid = [...paid]; s.simH = st.simH; s.priceMult = st.priceMult; savedMain = prevSaved; setSnap({ base: prevBase, injected: prevInj, paid: prevPaid, simH: prevH, priceMult: prevPm }); }
}
const inWin = (target: string, kind: Win["kind"], h: number) => wins().some((w) => w.target === target && w.kind === kind && h >= w.start && h < w.end);

const portStatus = (i: number, h: number) => (inWin(uid("port", i), "port", h) ? "closed" : "open");
const via = (i: number) => (i * 3 + 1) % PORTS.length;
const origin = (i: number) => i % PORTS.length;
const dest = (i: number) => i % WHS.length;
function shipment(i: number, h: number) {
  const d = dep(i), u = dur(i);
  const status = h < d ? "planned" : h >= d + u ? "arrived" : inWin(uid("ship", i), "ship", h) ? "delayed" : "in_transit";
  const p = Math.min(1, Math.max(0, (h - d) / u));
  const o = [PORTS[origin(i)][1], PORTS[origin(i)][2]], t = [WHS[dest(i)][1], WHS[dest(i)][2]];
  const pos = [o[0] + (t[0] - o[0]) * p, o[1] + (t[1] - o[1]) * p] as [number, number];
  const w = wins().find((x) => x.target === uid("ship", i) && x.kind === "ship" && h >= x.start);
  return { id: uid("ship", i), ref: `SHP-${String(i + 1).padStart(3, "0")}`, status, progress: +p.toFixed(3), cargo_value: 40000 + i * 13000, delay_hours: status === "delayed" && w ? Math.round(h - w.start) : 0, position: pt(pos) };
}
const stamp = { valid_from: iso(0), valid_to: null, changed_by: "mock" };
const regionOf = (lon: number) => (lon < -20 ? "AMER" : lon <= 60 ? "EMEA" : "APAC");
function objects(type: string, h: number): Record<string, any>[] {
  switch (type) {
    case "Port": return PORTS.map((p, i) => ({ id: uid("port", i), name: p[0], lat: p[2], lon: p[1], location: pt([p[1], p[2]]), region: regionOf(p[1]), status: portStatus(i, h), ...stamp }));
    case "Warehouse": return WHS.map((p, i) => ({ id: uid("wh", i), name: p[0], lat: p[2], lon: p[1], location: pt([p[1], p[2]]), region: regionOf(p[1]), ...stamp }));
    case "Carrier": return CARRIERS.map((n, i) => ({ id: uid("car", i), name: n, ...stamp }));
    case "Supplier": return Array.from({ length: 6 }, (_, i) => ({ id: uid("sup", i), name: `Supplier ${i + 1}`, country: ["DE", "SG", "CN", "US", "AE", "BR"][i], ...stamp }));
    case "Shipment": return Array.from({ length: NSHIP }, (_, i) => ({ ...shipment(i, h), ...stamp }));
    case "Invoice": return Array.from({ length: 6 }, (_, i) => ({ id: uid("inv", i), ref: `INV-${100 + i}`, amount: 12000 + i * 3500, currency: "USD", status: paid.has(uid("inv", i)) ? "paid" : inWin(uid("inv", i), "inv", h) ? "flagged" : h > 500 ? "paid" : "open", anomaly: inWin(uid("inv", i), "inv", h) ? "amount mismatch" : null, ...stamp }));
    case "Event": return evObjs().map((w, i) => ({ id: uid("ev", i), kind: "port_closure", severity: "high", started: iso(w.start), ended: iso(w.end), note: `Closure of ${PORTS[portIdx(w.target)][0]}`, ...stamp }));
    default: return [];
  }
}
const portIdx = (id: string) => Number(id.slice(-12));
const evObjs = () => wins().filter((w) => w.kind === "port");
const TYPES: Record<string, string[]> = {
  Port: ["name", "lat", "lon", "location", "region", "status"], Warehouse: ["name", "lat", "lon", "location", "region"], Carrier: ["name"], Supplier: ["name", "country"],
  Shipment: ["ref", "status", "progress", "cargo_value", "delay_hours", "position"], Invoice: ["ref", "amount", "currency", "status", "anomaly"],
  Event: ["kind", "severity", "started", "ended", "note"], PurchaseOrder: ["ref", "amount", "status"],
};
const ENUMS: Record<string, string[]> = { status: [] };


/* ---- role model (mirrors the Phase 3 contract: deny by default, row filters, masking) ---- */
const REGION: Record<string, string | null> = { admin: null, finance_analyst: null, planner: null, regional_ops_lead: "EMEA" };
const NO_READ: Record<string, string[]> = { admin: [], planner: ["Invoice", "PurchaseOrder"], finance_analyst: ["Event"], regional_ops_lead: ["Invoice", "PurchaseOrder"] };
const MASK: Record<string, Record<string, string[]>> = { planner: { Supplier: ["country"] }, regional_ops_lead: { Supplier: ["country"] }, finance_analyst: {}, admin: {} };
const NO_WRITE: Record<string, string[]> = { admin: [], planner: ["Invoice", "PurchaseOrder"], finance_analyst: ["Port", "Warehouse", "Shipment", "Carrier", "Event"], regional_ops_lead: ["Supplier", "Carrier", "Event", "Invoice"] };
function visible(role: string, type: string, o: Record<string, any>): boolean {
  if (!(role in REGION) || NO_READ[role].includes(type)) return false;
  const reg = REGION[role];
  if (!reg) return true;
  if (type === "Port" || type === "Warehouse") return o.region === reg;
  if (type === "Shipment") return regionOf(WHS[dest(portIdx(o.id))][1]) === reg;
  return true;
}
const mask = (role: string, type: string, o: Record<string, any>) => { const m = MASK[role]?.[type]; if (!m) return o; const c = { ...o }; for (const k of m) c[k] = null; return c; };
const vobjs = (type: string, h: number) => objects(type, h).filter((o) => visible(curRole, type, o)).map((o) => mask(curRole, type, o));
const visId = (type: string, id: string, h = st.simH) => objects(type, h).some((o) => o.id === id && visible(curRole, type, o));
function whoAmI(role: string) {
  const types: Record<string, { read: boolean; write: boolean; masked: string[] }> = {};
  for (const t of Object.keys(TYPES)) { const r = !NO_READ[role]?.includes(t); types[t] = { read: r, write: r && !NO_WRITE[role]?.includes(t), masked: MASK[role]?.[t] ?? [] }; }
  return { role, user: `user:${role}`, region: REGION[role] ?? null, types, actions: [] as string[] };
}
const ACT_PROPS = ["status", "anomaly"];
const pickP = (o: Record<string, any> | undefined) => (o ? Object.fromEntries(ACT_PROPS.filter((k) => k in o).map((k) => [k, o[k]])) : null);
/** Applies an action's effect on the mock world; returns the diff and an undo, or criteria failures. */
function applyEffect(name: string, p: Record<string, any>): { diff: any[]; undo: () => void } | { fail: string[]; notFound?: boolean } {
  const h = st.simH;
  const one = (type: string, id: string) => objects(type, h).find((o) => o.id === id);
  const mk = (type: string, id: string, mut: () => (() => void)) => {
    const before = pickP(one(type, id)); const undo = mut(); const after = pickP(one(type, id));
    return { diff: [{ kind: "object", type, key: id, op: "modify", before, after }], undo };
  };
  const push = (w: Win) => { st.injected.push(w); return () => { st.injected = st.injected.filter((x) => x !== w); }; };
  switch (name) {
    case "close_port": { const o = one("Port", p.port); if (!o || !visible(curRole, "Port", o)) return { fail: [], notFound: true }; if (o.status === "closed") return { fail: ["Port is already closed"] };
      return mk("Port", p.port, () => push({ target: p.port, start: h, end: h + (p.duration_h ?? 24), kind: "port" })); }
    case "reopen_port": { const o = one("Port", p.port); if (!o || !visible(curRole, "Port", o)) return { fail: [], notFound: true }; if (o.status !== "closed") return { fail: ["Port is not closed"] };
      const cut = wins().filter((w) => w.target === p.port && w.kind === "port" && h >= w.start && h < w.end).map((w) => [w, w.end] as const);
      return mk("Port", p.port, () => { cut.forEach(([w]) => (w.end = h)); return () => cut.forEach(([w, e]) => (w.end = e)); }); }
    case "flag_invoice": { const o = one("Invoice", p.invoice); if (!o || !visible(curRole, "Invoice", o)) return { fail: [], notFound: true }; if (o.status !== "open") return { fail: [`Invoice is ${o.status}, only open invoices can be flagged`] };
      return mk("Invoice", p.invoice, () => push({ target: p.invoice, start: h, end: h + 100000, kind: "inv" })); }
    case "pay_invoice": { const o = one("Invoice", p.invoice); if (!o || !visible(curRole, "Invoice", o)) return { fail: [], notFound: true }; if (o.status === "paid") return { fail: ["Invoice is already paid"] }; if (o.status === "flagged") return { fail: ["Invoice is flagged: resolve the anomaly first"] };
      return mk("Invoice", p.invoice, () => { paid.add(p.invoice); return () => paid.delete(p.invoice); }); }
    case "delay_shipment": { const o = one("Shipment", p.shipment); if (!o || !visible(curRole, "Shipment", o)) return { fail: [], notFound: true }; if (o.status !== "in_transit") return { fail: [`Shipment is ${o.status}, only in-transit shipments can be delayed`] };
      return mk("Shipment", p.shipment, () => push({ target: p.shipment, start: h, end: h + (p.delay_hours ?? 12), kind: "ship" })); }
    default: return { diff: [], undo: () => {} };
  }
}
const currentProps = (type: string, id: string) => pickP(objects(type, st.simH).find((o) => o.id === id));
gov.setWorld({ apply: applyEffect, current: currentProps, whoAmI, nowIso: () => iso(st.simH), visibleKey: (type, id) => visId(type, id), canSeeObj: (role, type, id) => { const prev = curRole; curRole = role; const r = visId(type, id); curRole = prev; return r; } });

interface Edge { src: string; dst: string; link: string; inferred: boolean }
function edges(h: number, inferred: boolean): Edge[] {
  const e: Edge[] = [];
  const a = (src: string, link: string, dst: string, inf = false) => e.push({ src, dst, link, inferred: inf });
  for (let i = 0; i < NSHIP; i++) {
    a(uid("ship", i), "ships_from", uid("port", origin(i))); a(uid("ship", i), "ships_to", uid("wh", dest(i))); a(uid("ship", i), "ships_via", uid("port", via(i)));
  }
  for (let i = 0; i < 6; i++) { a(uid("inv", i), "invoices_for", uid("ship", i)); a(uid("inv", i), "billed_by", uid("sup", i)); }
  for (let i = 0; i < 5; i++) a(uid("sup", i), "supplies", uid("sup", i + 1));
  WHS.forEach((_, i) => a(uid("wh", i), "operated_by", uid("car", i % 3)));
  evObjs().forEach((w, i) => a(uid("ev", i), "affects", w.target));
  if (inferred) {
    for (let i = 0; i < 4; i++) a(uid("sup", i), "supplies", uid("sup", i + 2), true);
    evObjs().forEach((w, i) => { if (h >= w.start && h < w.end) for (let s = 0; s < NSHIP; s++) if (uid("port", via(s)) === w.target) a(uid("ev", i), "affects", uid("ship", s), true); });
  }
  return e;
}
const nodeIndex = (h: number) => {
  const m = new Map<string, { id: string; type: string; label: string; status: string | null }>();
  for (const t of Object.keys(TYPES)) for (const o of vobjs(t, h)) m.set(o.id, { id: o.id, type: t, label: String(o.name ?? o.ref ?? o.kind), status: t === "Port" || t === "Shipment" || t === "Invoice" ? o.status : null });
  return m;
};

function summary(h: number) {
  const sh = objects("Shipment", h), cnt = (k: string) => sh.filter((s) => s.status === k).length;
  const inv = objects("Invoice", h), ic = (k: string) => inv.filter((s) => s.status === k).length;
  const closed = PORTS.filter((_, i) => portStatus(i, h) === "closed").length;
  const risk = sh.filter((s, i) => s.status !== "arrived" && (portStatus(via(i), h) === "closed" || s.status === "delayed")).reduce((a, s) => a + s.cargo_value, 0);
  return { as_of: iso(h), shipments: { planned: cnt("planned"), in_transit: cnt("in_transit"), delayed: cnt("delayed"), arrived: cnt("arrived") }, ports: { open: PORTS.length - closed, closed },
    invoices: NO_READ[curRole]?.includes("Invoice") ? { open: 0, approved: 0, paid: 0, flagged: 0 } : { open: ic("open"), approved: ic("approved"), paid: ic("paid"), flagged: ic("flagged") }, value_at_risk: risk, delay_hours_total: sh.reduce((a, s) => a + s.delay_hours, 0), open_events: evObjs().filter((w) => h >= w.start && h < w.end).length,
    counts: Object.fromEntries(["Port", "Warehouse", "Shipment", "Carrier", "Supplier", "Invoice", "Event"].filter((t) => !NO_READ[curRole]?.includes(t)).map((t) => [t, vobjs(t, h).length])),
    enums: { Shipment: { status: { planned: cnt("planned"), in_transit: cnt("in_transit"), delayed: cnt("delayed"), arrived: cnt("arrived") } } }, kpis: { value_at_risk: risk } };
}
function metric(m: string, h: number) {
  const s = summary(h);
  return ({ shipments_in_transit: s.shipments.in_transit, shipments_delayed: s.shipments.delayed, ports_closed: s.ports.closed, invoices_flagged: s.invoices.flagged, value_at_risk: s.value_at_risk } as Record<string, number>)[m] ?? 0;
}
function kpisNow() {
  const h = st.simH, s = summary(h), sh = objects("Shipment", h);
  const exposed = sh.map((x, i) => ({ x, i })).filter(({ x, i }) => x.status !== "arrived" && portStatus(via(i), h) === "closed");
  const expSup = new Set(exposed.filter(({ i }) => i < 6).map(({ i }) => i));
  return { value_at_risk: s.value_at_risk, delay_hours_total: s.delay_hours_total, shipments_delayed: s.shipments.delayed, shipments_in_transit: s.shipments.in_transit, ports_closed: s.ports.closed,
    invoices_flagged: s.invoices.flagged, extra_cost: Math.round(s.delay_hours_total * 85 * st.priceMult + (st.priceMult - 1) * 20000), exposure_shipments: exposed.length, exposure_suppliers: expSup.size };
}
const keyProps = () => {
  const out: Record<string, { type: string; props: Record<string, any> }> = {};
  for (const t of ["Port", "Shipment", "Invoice"]) for (const o of vobjs(t, st.simH)) out[o.id] = { type: t, props: { status: o.status, ...(t === "Invoice" ? { anomaly: o.anomaly } : {}) } };
  return out;
};
scn.setHost({
  snap: getSnap, withSnap, kpis: kpisNow, keyProps, metric, nowH: () => st.simH, iso, EPOCH_H: H,
  asRole: <T,>(r: string, fn: () => T): T => { const prev = curRole; curRole = r; try { return fn(); } finally { curRole = prev; } },
  uid, portCount: PORTS.length, shipCount: NSHIP, arrived: () => objects("Shipment", st.simH).filter((x) => x.status === "arrived").length,
  addWindow: (w) => { st.injected.push(w); },
  mainKpisAligned: () => (savedMain ? withSnap({ ...savedMain, base: cp(savedMain.base), injected: cp(savedMain.injected), paid: [...savedMain.paid], simH: st.simH }, kpisNow) : kpisNow()),
  currentRole: () => curRole,
});
gov.setScenarioKpis(() => ({ before: scn.mainKpisNow(), after: kpisNow() }));

function feed(h: number) {
  const out: { t: number; name: string; id: string; type: string; summary: string }[] = [];
  for (let i = 0; i < NSHIP; i++) { out.push({ t: dep(i), name: "ship_depart", id: uid("ship", i), type: "Shipment", summary: `SHP-${String(i + 1).padStart(3, "0")} departed ${PORTS[origin(i)][0]}` }); out.push({ t: dep(i) + dur(i), name: "ship_arrive", id: uid("ship", i), type: "Shipment", summary: `SHP-${String(i + 1).padStart(3, "0")} arrived at ${WHS[dest(i)][0]}` }); }
  for (const w of wins()) {
    if (w.kind === "port") { out.push({ t: w.start, name: "close_port", id: w.target, type: "Port", summary: `${PORTS[portIdx(w.target)][0]} closed` }); out.push({ t: w.end, name: "reopen_port", id: w.target, type: "Port", summary: `${PORTS[portIdx(w.target)][0]} reopened` }); }
    if (w.kind === "inv") out.push({ t: w.start, name: "flag_invoice", id: w.target, type: "Invoice", summary: `Invoice INV-${100 + portIdx(w.target)} flagged: amount mismatch` });
    if (w.kind === "ship") out.push({ t: w.start, name: "delay_shipment", id: w.target, type: "Shipment", summary: `Shipment SHP-${String(portIdx(w.target) + 1).padStart(3, "0")} delayed` });
  }
  return out.filter((x) => x.t <= h).sort((a, b) => b.t - a.t || (a.name < b.name ? 1 : -1));
}
const simState = () => ({ sim_now: iso(st.simH), epoch: iso(0), speed: st.speed, running: st.speed !== "paused", seed: 1, tick: st.tick, achieved_hours_per_s: st.speed === "paused" ? null : Number(st.speed) * 0.98, last_error: null });

const hOf = (v: string | null) => (!v || v === "live" ? st.simH : (Date.parse(v) - EPOCH) / H);

function route(method: string, path: string, q: URLSearchParams, body: any): [number, unknown] {
  const asOfH = hOf(q.get("as_of") ?? q.get("as_of_valid"));
  let m: RegExpMatchArray | null;
  const g = gov.route(method, path, q, body, curRole);
  if (g) return g;
  if (path === "/sim/state") return [200, simState()];
  if (path === "/sim/control") {
    if (body?.reset) { st.simH = 48; st.tick = 0; st.injected = []; st.speed = "paused"; }
    if (body?.speed) st.speed = body.speed;
    if (body?.step) { st.simH += 1; st.tick += 1; }
    return [200, simState()];
  }
  if (path === "/sim/inject") {
    const at = body?.at ? hOf(body.at) : st.simH, d = body?.duration_h ?? 24;
    const kind = body?.kind as string;
    let target = body?.target as string | undefined;
    if (kind === "port_closure") target ??= uid("port", 1);
    if (kind === "shipment_delay") target ??= uid("ship", 0);
    if (kind === "invoice_anomaly") target ??= uid("inv", 1);
    st.injected.push({ target: target!, start: at, end: at + d, kind: kind === "port_closure" ? "port" : kind === "shipment_delay" ? "ship" : "inv" });
    return [200, { injected: { kind, target: target ?? null, at: iso(at) } }];
  }
  if (path === "/events") {
    const lim = Number(q.get("limit") ?? 100), to = q.get("to") ? hOf(q.get("to")) : st.simH, from = q.get("from") ? hOf(q.get("from")) : -1;
    const rows = feed(to).filter((x) => x.t >= from && (x.type === "Shipment" || x.type === "Port" || x.type === "Invoice" ? visId(x.type, x.id, st.simH) : true)).slice(0, lim).map((x, i, a) => ({ seq: 100000 - i, action_id: `act-${x.name}-${Math.round(x.t)}-${i}`, name: x.name, actor: "sim", source: "sim", sim_time: iso(x.t), recorded_at: iso(x.t), params: { id: x.id, type: x.type }, summary: x.summary }));
    return [200, rows];
  }
  if ((m = path.match(/^\/geo\/(\w+)$/))) {
    const t = m[1];
    const feats = vobjs(t, asOfH).filter((o) => o.position ?? o.location).map((o) => {
      const { position, location, ...rest } = o; const props: any = { ...rest, type: t };
      if (t === "Shipment") { const i = portIdx(o.id); props.origin = [PORTS[origin(i)][1], PORTS[origin(i)][2]]; props.destination = [WHS[dest(i)][1], WHS[dest(i)][2]]; }
      return { type: "Feature", id: o.id, geometry: position ?? location, properties: props };
    });
    return [200, { type: "FeatureCollection", features: feats }];
  }
  if (path === "/stats/summary") return [200, summary(asOfH)];
  if (path === "/stats/series") {
    const f = hOf(q.get("from")), t = hOf(q.get("to")), s = Number(q.get("step_hours") ?? 24), pts = [];
    for (let x = f; x <= t + 1e-6; x += s) pts.push({ t: iso(x), v: metric(q.get("metric")!, x) });
    if (pts.length && Date.parse(pts[pts.length - 1].t) < EPOCH + t * H - 1) pts.push({ t: iso(t), v: metric(q.get("metric")!, t) });
    return [200, { metric: q.get("metric"), points: pts }];
  }
  if (path === "/graph") {
    const live = !q.get("as_of") || q.get("as_of") === "live", c = q.get("center")!, depth = Math.min(3, Number(q.get("depth") ?? 1));
    const want = q.get("include_inferred") !== "false";
    if (!nodeIndex(asOfH).has(c)) return [404, { detail: "not found" }];
    const es = edges(asOfH, live && want), idx = nodeIndex(asOfH);
    const seen = new Set([c]); let frontier = [c];
    for (let d = 0; d < depth; d++) { const nx: string[] = []; for (const e of es) for (const [x, y] of [[e.src, e.dst], [e.dst, e.src]]) if (frontier.includes(x) && !seen.has(y)) { seen.add(y); nx.push(y); } frontier = nx; }
    const ids = [...seen].slice(0, 300);
    const set = new Set(ids);
    return [200, { center: c, as_of: live ? null : q.get("as_of"), inferred_available: live, truncated: seen.size > 300, nodes: ids.map((id) => idx.get(id)).filter(Boolean), edges: es.filter((e) => set.has(e.src) && set.has(e.dst)) }];
  }
  if (path === "/types") return [200, { namespace: "mock", classes: {}, role: curRole, linkTypes: [], objectTypes: Object.entries(TYPES).filter(([n]) => !NO_READ[curRole]?.includes(n)).map(([name, ps]) => ({ name, geo: ["Port", "Warehouse", "Shipment"].includes(name), properties: ps.map((p) => ({ name: p, type: "string", required: false, enum: ENUMS[p] ?? [] })) })) }];
  if ((m = path.match(/^\/objects\/(\w+)$/)) && method === "GET") return [200, vobjs(m[1], asOfH).slice(Number(q.get("offset") ?? 0), Number(q.get("offset") ?? 0) + Number(q.get("limit") ?? 100))];
  if ((m = path.match(/^\/objects\/(\w+)\/([\w-]+)\/history$/))) return [200, vobjs(m[1], st.simH).filter((o) => o.id === m![2]).map((o) => ({ ...o, recorded_from: iso(0), recorded_to: null }))];
  if ((m = path.match(/^\/objects\/(\w+)\/([\w-]+)$/)) && method === "GET") { const o = vobjs(m[1], asOfH).find((x) => x.id === m![2]); return o ? [200, o] : [404, { detail: "not found" }]; }
  if ((m = path.match(/^\/neighbours\/(\w+)\/([\w-]+)$/))) {
    const idx = nodeIndex(asOfH), id = m[2], out = [];
    for (const e of edges(asOfH, false)) { const other = e.src === id ? e.dst : e.dst === id ? e.src : null; const n = other && idx.get(other); if (n) out.push({ link: e.link, direction: e.src === id ? "out" : "in", type: n.type, id: n.id, object: vobjs(n.type, asOfH).find((o) => o.id === n.id) ?? null }); }
    return [200, out];
  }
  return [404, { detail: `mock: no route ${method} ${path}` }];
}

const wsWorlds = new Map<string, ReturnType<typeof makeWorld>>();
const MOCK_AUTH = !!((import.meta as any).env?.VITE_MOCK_AUTH);

export function installMock(base: string) {
  const w = window as any;
  w.__mockLog = [] as unknown[];
  ing.setHost(base);
  const orig = window.fetch.bind(window);
  setInterval(() => { if (st.speed !== "paused") { st.simH += Number(st.speed); st.tick += Number(st.speed); } }, 1000);
  const json = (status: number, data: unknown) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!url.startsWith(base)) return orig(input, init);
    const u = new URL(url), method = (init?.method ?? "GET").toUpperCase();
    const rawBody = init?.body;
    const blob = rawBody instanceof Blob ? rawBody : undefined;
    let body: any;
    if (rawBody && !blob) { try { body = JSON.parse(String(rawBody)); } catch { body = undefined; } }
    const hdrs = (init?.headers as Record<string, string> | undefined) ?? {};
    const tok = ing.tokenRole(hdrs.authorization);
    let role = hdrs["x-role"];
    // workspace prefix: /__ws/<slug>/... is served by another mock world
    let pathname = u.pathname, wsSlug = "default";
    const wm = pathname.match(/^\/__ws\/([\w-]+)(\/.*)$/);
    if (wm) { wsSlug = wm[1]; pathname = wm[2]; }
    w.__mockLog.push({ method, path: u.pathname, query: Object.fromEntries(u.searchParams), role, body, ws: wsSlug });
    if (w.__mockLog.length > 500) w.__mockLog.splice(0, 250);
    await new Promise((r) => setTimeout(r, 30));
    if (pathname === "/auth/config") return json(200, { enabled: MOCK_AUTH });
    if (pathname === "/auth/login") return json(...ing.login(body));
    if (pathname === "/auth/logout") return json(200, { ok: true });
    if (MOCK_AUTH) { if (!tok) return json(401, { code: "unauthenticated", detail: "authentication required" }); role = tok.role; }
    curRole = role ?? "admin";
    if (wm) {
      let world = wsWorlds.get(wsSlug);
      if (!world) {
        const roles = [{ role: "admin", description: "Full access" }, ...(ing.workspaceRoles[wsSlug] ?? [{ role: "operator", description: "Operates" }, { role: "viewer", description: "Read-only" }]).filter((r) => r.role !== "admin")];
        world = makeWorld({ ...DEFAULT_NAMES, ...(ing.wsNames[wsSlug] ?? {}) }, roles); wsWorlds.set(wsSlug, world);
      }
      const o = ing.ontRoute(method, pathname, u.searchParams, body, curRole, wsSlug) ?? world.route(method, pathname, u.searchParams, curRole);
      return o ? json(...o) : json(404, { detail: `mock: no route ${method} ${pathname} in workspace ${wsSlug}` });
    }
    const adm = ing.route(method, pathname, u.searchParams, body, curRole, blob) ?? ing.ontRoute(method, pathname, u.searchParams, body, curRole, "default");
    if (adm) return json(...adm);
    const scnHdr = hdrs["x-scenario"];
    const dispatch = (): Response | [number, unknown] => {
      const ar = agentRoute(method, pathname, u.searchParams, body, curRole);
      if (ar) return ar;
      const sr = scn.route(method, pathname, u.searchParams, body, curRole);
      return sr ?? route(method, pathname, u.searchParams, body);
    };
    const out = scnHdr && !pathname.startsWith("/scenarios") && !pathname.startsWith("/agent") ? scn.overlay(scnHdr, pathname, method, curRole, dispatch) : dispatch();
    if (out instanceof Response) return out;
    const [status, data] = out;
    return json(status, data);
  };
}
