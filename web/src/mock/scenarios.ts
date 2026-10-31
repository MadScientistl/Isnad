/**
 * Mock scenario layer (Phase 5 contract): /scenarios CRUD, compare, merge (with a conflict case), simulate (fake job), series, and the
 * `x-scenario` overlay. A scenario owns a private copy of the mock world's mutable state (closure windows, paid invoices, clock); while a
 * request carries `x-scenario`, index.ts swaps that copy in, so objects/geo/stats/graph/actions all read and write the scenario only.
 * Visibility: owner, admin, or a role listed in shared_roles; anything else is 404 scenario_not_found. /sim/*, /webhooks/* and approve/reject
 * answer 400 scenario_unsupported inside a scenario.
 */
import * as gov from "./gov";

type Json = Record<string, any>;
interface Win { target: string; start: number; end: number; kind: "port" | "ship" | "inv" }
interface Snap { base: Win[]; injected: Win[]; paid: string[]; simH: number; priceMult: number }
interface Host {
  snap(): Snap; withSnap<T>(s: Snap, fn: () => T): T; kpis(): Json; keyProps(): Record<string, { type: string; props: Json }>; metric(m: string, h: number): number;
  nowH(): number; iso(h: number): string; EPOCH_H: number; asRole<T>(r: string, fn: () => T): T; uid(k: string, i: number): string; portCount: number; shipCount: number;
  arrived(): number; addWindow(w: Win): void; mainKpisAligned(): Json; currentRole(): string;
}
let H!: Host;
export const setHost = (h: Host) => { H = h; };
export const mainKpisNow = () => H.mainKpisAligned();

interface Scn {
  id: number; name: string; owner_user: string; owner_role: string; status: "open" | "merged" | "discarded" | "expired"; base_recorded: string; base_sim_time: string; clock: string | null;
  shared_roles: string[]; created_at: string; expires_at: string | null; run_id: string | null; data_version: number;
  snap: Snap; inj0: number; paid0: number; baseVals: Record<string, { type: string; props: Json }>; touched: Set<string>; simRuns: number;
}
const scns: Scn[] = [];
let nextId = 1;
const user = (r: string) => `user:${r}`;
const err = (status: number, code: string, detail: string, extra: Json = {}): [number, unknown] => [status, { detail: { code, detail, ...extra } }];
const visible = (s: Scn, role: string) => s.owner_user === user(role) || role === "admin" || s.shared_roles.includes(role);
const out = (s: Scn) => {
  const { snap: _s, inj0: _i, paid0: _p, baseVals: _b, touched: _t, simRuns: _r, ...o } = s;
  const rows = overrides(s);
  return { ...o, clock: s.clock, override_rows: rows, data_version: s.data_version };
};
/** Rows that differ from the fork point: objects touched by actions plus windows and payments added since (a rough stand-in for override rows). */
const overrides = (s: Scn) => s.touched.size + Math.max(0, s.snap.injected.length - s.inj0) + Math.max(0, s.snap.paid.length - s.paid0);

function create(role: string, name: string, opts: { run_id?: string | null; expires_in_h?: number; shared_roles?: string[] } = {}): Scn {
  const snap = H.snap();
  const h = snap.simH;
  const s: Scn = {
    id: nextId++, name, owner_user: user(role), owner_role: role, status: "open", base_recorded: new Date().toISOString(), base_sim_time: H.iso(h), clock: H.iso(h), shared_roles: opts.shared_roles ?? [],
    created_at: new Date().toISOString(), expires_at: new Date(Date.now() + (opts.expires_in_h ?? 168) * 3600000).toISOString(), run_id: opts.run_id ?? null, data_version: 0,
    snap, inj0: snap.injected.length, paid0: snap.paid.length, baseVals: H.asRole(role, () => H.keyProps()), touched: new Set(), simRuns: 0,
  };
  scns.unshift(s); return s;
}

/** Runs `fn` inside the scenario world as `role`, with governance rows tagged for it. Returns whatever fn returns. */
export function runIn<T>(s: Scn, role: string, fn: () => T): T {
  gov.setScn(s.id);
  try { return H.asRole(role, () => H.withSnap(s.snap, fn)); } finally { gov.setScn(0); }
}

/** Overlay entry used by index.ts for requests carrying x-scenario. */
export function overlay(hdr: string, path: string, method: string, role: string, run: () => Response | [number, unknown]): Response | [number, unknown] {
  const id = Number(hdr);
  const s = scns.find((x) => x.id === id);
  if (!s || s.status !== "open" || !visible(s, role)) return err(404, "scenario_not_found", "scenario not found");
  if (/^\/(sim|webhooks)(\/|$)/.test(path) || /^\/proposals\/[\w-]+\/(approve|reject)$/.test(path)) return err(400, "scenario_unsupported", `${path} is not available inside a scenario`);
  const before = JSON.stringify([s.snap.injected, s.snap.paid, s.snap.base]);
  const res = runIn(s, role, run);
  if (method !== "GET") {
    if (JSON.stringify([s.snap.injected, s.snap.paid, s.snap.base]) !== before) s.data_version++;
    if (res instanceof Response) return res;
    const x = gov.scenarioExecs(s.id).slice(-1)[0];
    for (const d of (x ? ((x as any).diff ?? []) : [])) s.touched.add(d.key);
  }
  return res;
}

const kpiOf = (s: Scn | "main", role: string, atH: number) => {
  if (s === "main") return H.asRole(role, () => { const m = H.snap(); m.simH = atH; return H.withSnap(m, () => ({ kpis: H.kpis(), vals: H.keyProps() })); });
  return H.asRole(role, () => { const c = { ...s.snap, base: s.snap.base.map((x) => ({ ...x })), injected: s.snap.injected.map((x) => ({ ...x })), paid: [...s.snap.paid] }; return H.withSnap(c, () => ({ kpis: H.kpis(), vals: H.keyProps() })); });
};

function compare(s: Scn, other: Scn | "main", role: string) {
  const atH = s.snap.simH;
  const A = kpiOf(s, role, atH), B = other === "main" ? kpiOf("main", role, atH) : kpiOf(other, role, atH);
  const diff: Json[] = [];
  for (const key of new Set([...Object.keys(A.vals), ...Object.keys(B.vals)])) {
    const a = A.vals[key], b = B.vals[key], t = (a ?? b).type;
    if (a && b && JSON.stringify(a.props) === JSON.stringify(b.props)) continue;
    // before = the baseline side (with=), after = the path scenario
    diff.push({ kind: "object", type: t, key, op: !b ? "create" : !a ? "delete" : "modify", before: b?.props ?? null, after: a?.props ?? null });
  }
  const delta: Json = {}; for (const k of Object.keys(A.kpis)) delta[k] = B.kpis[k] - A.kpis[k]; // contract: delta = b - a
  return { a: out(s), b: other === "main" ? { id: "main" } : out(other), diff: diff.slice(0, 500), kpis: { a: A.kpis, b: B.kpis, delta }, truncated: diff.length > 500 };
}

const hash = (str: string) => { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(16).padStart(8, "0"); };
interface Job { id: string; sid: number; t0: number; result: Json }
const jobs: Job[] = [];

function simulate(s: Scn, role: string, b: Json): [number, unknown] {
  const days = Number(b?.days);
  if (!Number.isInteger(days) || days < 1 || days > 90) return [422, { detail: [{ loc: ["body", "days"], msg: "days must be 1..90", type: "value_error" }] }];
  const inj: Json[] = Array.isArray(b?.injections) ? b.injections : [];
  const seed = Number(b?.seed ?? 1);
  const clock0 = s.clock;
  const res = runIn(s, role, () => {
    const arrived0 = H.arrived(), h0 = H.nowH();
    for (const i of inj) {
      const start = h0 + Number(i.at_h ?? 0), end = start + Number(i.duration_h ?? 24);
      if (i.kind === "port_closure") H.addWindow({ target: i.target || H.uid("port", 1), start, end, kind: "port" });
      else if (i.kind === "shipment_delay") H.addWindow({ target: i.target || H.uid("ship", 0), start, end, kind: "ship" });
      else if (i.kind === "supplier_delay") for (let k = 0; k < H.shipCount; k += 6) H.addWindow({ target: H.uid("ship", k), start, end, kind: "ship" });
      else if (i.kind === "invoice_anomaly") H.addWindow({ target: i.target || H.uid("inv", 1), start, end, kind: "inv" });
    }
    return { arrived0, h0 };
  });
  const price = inj.filter((i) => i.kind === "price_shock").reduce((m, i) => m * Number(i.magnitude ?? 1.5), 1);
  s.snap.priceMult *= price;
  s.snap.simH += days * 24; s.clock = H.iso(s.snap.simH); s.data_version++; s.simRuns++;
  const kpis = runIn(s, role, () => H.kpis());
  const arrived1 = runIn(s, role, () => H.arrived());
  const digest = hash(JSON.stringify([clock0, seed, days, inj]));
  const job: Job = { id: `job-${s.id}-${jobs.length + 1}`, sid: s.id, t0: Date.now(), result: { sim_now: s.clock, digest: `${digest}${hash(digest + seed)}`, actions: { ship_arrive: Math.max(0, arrived1 - res.arrived0), injected: inj.length, ticks: days * 24 }, kpis, error: null } };
  jobs.push(job);
  return [202, { job_id: job.id }];
}

export function route(method: string, path: string, q: URLSearchParams, body: Json | undefined, role: string): [number, unknown] | null {
  if (!/^\/scenarios(\/|$)/.test(path)) return null;
  let m: RegExpMatchArray | null;
  if (path === "/scenarios" && method === "POST") {
    const name = String(body?.name ?? "").trim();
    if (!name) return [422, { detail: [{ loc: ["body", "name"], msg: "name required", type: "value_error" }] }];
    const open = scns.filter((x) => x.owner_user === user(role) && x.status === "open").length;
    if (open >= 5) return err(429, "scenario_limit", "you already have 5 open scenarios (ISNAD_SCN_MAX_PER_USER)");
    return [201, out(create(role, name, { expires_in_h: body?.expires_in_h, shared_roles: body?.shared_roles ?? [] }))];
  }
  if (path === "/scenarios" && method === "GET") {
    const all = q.get("status") === "all";
    return [200, scns.filter((x) => visible(x, role) && (all || x.status === "open")).map(out)];
  }
  if ((m = path.match(/^\/scenarios\/(\d+)(\/[\w/-]+)?$/))) {
    const s = scns.find((x) => x.id === Number(m![1]));
    const sub = m[2] ?? "";
    if (!s || !visible(s, role) || (s.status !== "open" && !(method === "GET" && (sub === "" || sub === "/compare")))) return err(404, "scenario_not_found", "scenario not found");
    if (!sub && method === "GET") return [200, out(s)];
    if (!sub && method === "DELETE") {
      if (s.owner_user !== user(role) && role !== "admin") return err(403, "permission_denied", "only the owner or an admin can discard a scenario");
      s.status = "discarded"; return [200, { id: s.id, status: "discarded" }];
    }
    if (sub === "/compare" && method === "GET") {
      const w = q.get("with") ?? "main";
      let other: Scn | "main" = "main";
      if (w !== "main") { const o = scns.find((x) => x.id === Number(w)); if (!o || !visible(o, role)) return err(404, "scenario_not_found", "scenario not found"); other = o; }
      return [200, compare(s, other, role)];
    }
    if (sub === "/series" && method === "GET") {
      const metric = q.get("metric") ?? "value_at_risk", step = Number(q.get("step_hours") ?? 24);
      const to = s.snap.simH, baseH = (Date.parse(s.base_sim_time) - Date.parse(H.iso(0))) / 3600000, from = Math.max(0, baseH - 120);
      const pts: { t: string; v: number }[] = [];
      runIn(s, role, () => { for (let x = from; x <= to + 1e-6; x += step) pts.push({ t: H.iso(x), v: H.metric(metric, x) }); if (pts.length && pts[pts.length - 1].t !== H.iso(to)) pts.push({ t: H.iso(to), v: H.metric(metric, to) }); });
      return [200, { metric, points: pts }];
    }
    if (sub === "/simulate" && method === "POST") {
      if (s.owner_user !== user(role) && role !== "admin") return err(403, "permission_denied", "only the owner can simulate in a scenario");
      return simulate(s, role, body ?? {});
    }
    if ((m = sub.match(/^\/simulate\/([\w-]+)$/)) && method === "GET") {
      const j = jobs.find((x) => x.id === m![1] && x.sid === s.id);
      if (!j) return [404, { detail: "job not found" }];
      return [200, Date.now() - j.t0 < 1800 ? { status: "running", sim_now: H.iso(s.snap.simH - Math.max(0, (1 - (Date.now() - j.t0) / 1800)) * 24) } : { status: "done", ...j.result }];
    }
    if (sub === "/merge" && method === "POST") {
      if (s.owner_user !== user(role) && role !== "admin") return err(403, "permission_denied", "only the owner or an admin can merge a scenario");
      const mine = H.asRole(role, () => H.keyProps()), conflicts: Json[] = [];
      for (const x of gov.scenarioExecs(s.id)) for (const d of ((x as any).diff ?? []) as Json[]) {
        const base = s.baseVals[d.key], now = mine[d.key];
        if (!base || !now) continue;
        if (JSON.stringify(base.props) !== JSON.stringify(now.props) && !conflicts.some((c) => c.key === d.key))
          conflicts.push({ kind: "object", type: d.type, key: d.key, reason: `main changed since the fork: ${Object.keys(base.props).filter((k) => base.props[k] !== now.props[k]).map((k) => `${k} ${JSON.stringify(base.props[k])} -> ${JSON.stringify(now.props[k])}`).join(", ")}` });
      }
      if (conflicts.length) return err(409, "merge_conflict", "main changed on objects this scenario touched", { conflicts });
      const merged = H.asRole(role, () => gov.replayOnMain(s.id, role));
      s.status = "merged";
      return [200, { status: "merged", merged }];
    }
  }
  return err(404, "not_found", `mock: no route ${method} ${path}`);
}

/* ---- helpers for the mock agent (scenario_open / apply / compare) ---- */
export function agentOpen(role: string, runId: string, name: string): number { return create(role, name, { run_id: runId }).id; }
export function agentApply(id: number, role: string, action: string, params: Json): Json {
  const s = scns.find((x) => x.id === id)!;
  const [status, data] = runIn(s, role, () => gov.executeInScenario(action, params, role));
  s.data_version++;
  for (const d of ((gov.scenarioExecs(id).slice(-1)[0] as any)?.diff ?? []) as Json[]) s.touched.add(d.key);
  return { status, ...(data as Json) };
}
export function agentCompare(id: number, role: string): Json {
  const s = scns.find((x) => x.id === id)!, c = compare(s, "main", role);
  const delta: Json = {}; for (const k of Object.keys(c.kpis.a)) delta[k] = c.kpis.a[k] - c.kpis.b[k];
  return { kpis: { main: c.kpis.b, scenario: c.kpis.a, delta }, diff_summary: { changed_objects: c.diff.length } };
}
export function agentProposalEvidence(id: number, role: string, action: string, params: Json): { diff: any[]; impact: Json } {
  const s = scns.find((x) => x.id === id)!, c = compare(s, "main", role);
  void action; void params;
  const delta: Json = {}; for (const k of Object.keys(c.kpis.a)) delta[k] = c.kpis.a[k] - c.kpis.b[k];
  return { diff: c.diff, impact: { before: { kpis: c.kpis.b }, after: { kpis: c.kpis.a }, delta: { kpis: delta } } };
}
export function setRunScenarioClock() { /* reserved */ }
