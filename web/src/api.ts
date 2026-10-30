import { OntologyClient } from "@sdk/client";
import type { TypesResponse } from "@sdk/client";

const env = (import.meta as any).env ?? {};
const lsGet = (k: string): string | null => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k: string, v: string | null) => { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* storage blocked */ } };

/** The API the app was started against (hosts ingest, workspaces, auth). A workspace may switch the active base. */
export const HOME_BASE: string = (env.VITE_API ?? "http://localhost:8000").replace(/\/$/, "");
export const MOCK: boolean = !!env.VITE_MOCK && env.VITE_MOCK !== "0";
let base: string = (() => { const b = lsGet("isnad.apiBase"); return b && /^https?:\/\//.test(b) ? b.replace(/\/$/, "") : HOME_BASE; })();
/** Current API base (the active workspace's). */
export const getBase = () => base;
export const BASE: string = HOME_BASE;
export const client = new OntologyClient(base);
/** Switch the active API base; `null` returns to the home API. Persisted (storage failures are ignored). */
export function setBase(b: string | null) {
  base = (b ?? HOME_BASE).replace(/\/$/, "");
  client.baseUrl = base;
  lsSet("isnad.apiBase", base === HOME_BASE ? null : base);
}

/* ---- auth (optional: ISNAD_AUTH=1 on the server). With a stored token every request carries it and the role switcher is hidden. ---- */
export interface AuthSession { token: string; user: string; role: string; expires_at: string | null }
let auth: AuthSession | null = (() => { try { const a = JSON.parse(lsGet("isnad.auth") ?? "null"); return a && typeof a.token === "string" && (!a.expires_at || Date.parse(a.expires_at) > Date.now()) ? a : null; } catch { return null; } })();
export const getAuth = () => auth;
export function setAuth(a: AuthSession | null) { auth = a; lsSet("isnad.auth", a ? JSON.stringify(a) : null); }
export const authHeader = (): Record<string, string> => (auth ? { authorization: `Bearer ${auth.token}` } : {});
const isApiUrl = (url: string) => url.startsWith(base) || url.startsWith(HOME_BASE);

let curRole: string = "admin";
export const getRole = () => curRole;

/** Active what-if scenario (null = main). Sent as `x-scenario` on every request except /scenarios* routes. */
let curScenario: number | null = null;
export const getScenario = () => curScenario;
export function setScenarioHeader(id: number | null) { curScenario = id; }
/** Headers for a request; `scn` overrides the active scenario ("main" forces main, a number forces that scenario). */
export function scnHeaders(path: string, scn?: number | "main" | null): Record<string, string> {
  if (/^\/scenarios(\/|$)/.test(path)) return {};
  const v = scn === undefined ? curScenario : scn;
  // "main" is a marker stripped by installScenarioFetch: it stops the global wrapper from adding the active scenario.
  return v === null ? {} : { "x-scenario": String(v) };
}
/**
 * Wraps window.fetch (call once after the optional mock is installed): adds `x-scenario` and the bearer token to API calls made by
 * the generated SDK (which cannot be edited), honours the "main" marker, signals 401 so the app can show the login screen,
 * and clears the active scenario when the server says it no longer exists.
 */
export function installScenarioFetch() {
  const orig = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!isApiUrl(url)) return orig(input, init);
    const root = url.startsWith(base) ? base : HOME_BASE;
    const path = url.slice(root.length).split("?")[0];
    const h = { ...((init?.headers as Record<string, string> | undefined) ?? {}) };
    if (h["x-scenario"] === "main") delete h["x-scenario"];
    else if (!h["x-scenario"]) Object.assign(h, scnHeaders(path));
    if (auth && !h.authorization) { Object.assign(h, authHeader()); }
    const r = await orig(input, { ...init, headers: h });
    if (r.status === 401 && path !== "/auth/login") window.dispatchEvent(new CustomEvent("isnad-unauthorized"));
    if (r.status === 404 && h["x-scenario"] && !/^\/scenarios(\/|$)/.test(path)) {
      r.clone().json().then((b) => { const c = b?.detail?.code ?? b?.code; if (c === "scenario_not_found") window.dispatchEvent(new CustomEvent("isnad-scenario-gone", { detail: Number(h["x-scenario"]) })); }, () => {});
    }
    return r;
  };
}
export function setRole(r: string) { curRole = r; client.setRole(r as any); }
setRole(curRole);

export type Speed = "paused" | "1" | "6" | "24";
export interface SimState {
  sim_now: string; epoch: string; speed: Speed; running: boolean; seed: number; tick: number;
  achieved_hours_per_s: number | null; last_error: string | null;
}
export interface GeoFeature {
  type: "Feature"; id: string; geometry: { type: string; coordinates: any };
  properties: { id: string; type: string; name?: string; ref?: string; status?: string; origin?: [number, number] | null; destination?: [number, number] | null; [k: string]: unknown };
}
export interface GeoCollection { type: "FeatureCollection"; features: GeoFeature[] }
/** /stats/summary: generic shape {counts, enums, kpis}; the supply-chain ontology also keeps the legacy keys. */
export interface Summary {
  as_of: string;
  counts?: Record<string, number>;
  enums?: Record<string, Record<string, Record<string, number>>>;
  kpis?: Record<string, number | { value: number; label?: string; description?: string }>;
  shipments?: { planned: number; in_transit: number; delayed: number; arrived: number };
  ports?: { open: number; closed: number }; invoices?: { open: number; approved: number; paid: number; flagged: number };
  value_at_risk?: number; delay_hours_total?: number; open_events?: number;
}
export type Metric = "shipments_in_transit" | "shipments_delayed" | "ports_closed" | "invoices_flagged" | "value_at_risk";
export interface Series { metric: Metric; points: { t: string; v: number }[] }
export interface EventRow {
  scenario_id?: number; seq: number; action_id: string; name: string; actor: string; source: string; sim_time: string; recorded_at: string;
  params: Record<string, unknown>; summary: string;
}
export interface GraphNode { id: string; type: string; label: string; status: string | null }
export interface GraphEdge { src: string; dst: string; link: string; inferred: boolean }
export interface GraphResp { center: string; as_of: string | null; inferred_available: boolean; truncated: boolean; nodes: GraphNode[]; edges: GraphEdge[] }

type Params = Record<string, string | number | boolean | undefined | null>;
/** Error carrying the HTTP status and the parsed body; `payload` unwraps FastAPI's {detail: {...}} envelope. */
export class ApiError extends Error {
  constructor(public status: number, public body: any, msg: string) { super(msg); }
  get payload(): any { const b = this.body; return b && typeof b === "object" && b.detail && typeof b.detail === "object" && !Array.isArray(b.detail) ? b.detail : b; }
  get code(): string | undefined { return this.payload?.code ?? this.body?.code; }
  get text(): string {
    const p = this.payload, d = p?.detail ?? this.body?.detail ?? this.body;
    return typeof d === "string" ? d : typeof p?.message === "string" ? p.message : JSON.stringify(d);
  }
}
/** Human-readable error text from a FastAPI `detail` (string, {code, detail|message, ...}, validation list); keeps the code for matching. */
function readable(d: unknown): string {
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const o = d as { code?: unknown; detail?: unknown; message?: unknown };
    const t = typeof o.detail === "string" ? o.detail : typeof o.message === "string" ? o.message : null;
    if (t) return o.code ? `${t} (${String(o.code)})` : t;
  }
  return JSON.stringify(d);
}
export type ReqOpts = { method?: string; body?: unknown; params?: Params; scenario?: number | "main" | null; home?: boolean };
export async function reqFull<T>(path: string, o: ReqOpts = {}): Promise<{ status: number; data: T }> {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(o.params ?? {})) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const q = u.toString();
  const r = await fetch((o.home ? HOME_BASE : base) + path + (q ? `?${q}` : ""), {
    method: o.method ?? "GET",
    headers: { "content-type": "application/json", "x-role": curRole, ...scnHeaders(path, o.scenario) },
    body: o.body === undefined ? undefined : JSON.stringify(o.body),
  });
  if (!r.ok) {
    const raw = await r.text();
    let body: unknown = raw;
    try { body = JSON.parse(raw); } catch { /* text */ }
    const d = body && typeof body === "object" && "detail" in (body as object) ? (body as { detail: unknown }).detail : body;
    throw new ApiError(r.status, body, `API ${r.status}: ${readable(d)}`);
  }
  return { status: r.status, data: (await r.json()) as T };
}
export const req = async <T,>(path: string, o: ReqOpts = {}): Promise<T> => (await reqFull<T>(path, o)).data;
export const isNotFound = (e: unknown) => (e instanceof ApiError ? e.status === 404 : /API 404/.test(String((e as Error)?.message ?? e)));

export interface Me { role: string; user: string; region: string | null; types: Record<string, { read: boolean; write: boolean; masked: string[] }>; actions: string[] }
export interface ParamMeta { name: string; type: string; ref?: string | null; required?: boolean }
export interface ActionInfo { name: string; description: string; params: ParamMeta[] | Record<string, string>; requires_approval: boolean; allowed: boolean; reason: string | null }
export interface DiffRow { kind: "object" | "link"; type: string; key: string; op: "create" | "modify" | "delete"; before: Record<string, unknown> | null; after: Record<string, unknown> | null }
export interface Execution {
  scenario_id?: number; action_id: string; name: string; actor: string; role?: string; source?: string; status: string; params: Record<string, unknown>;
  sim_time?: string; recorded_at: string; proposal_id?: string | null; parent_execution_id?: string | null; rolled_back?: boolean; error?: string | null; on_behalf_of?: string | null; tool_trace?: unknown;
}
export interface Delivery { attempt: number; status: "pending" | "delivered" | "retry" | "dead"; http_status: number | null; error: string | null; next_attempt_at?: string | null; at: string }
export interface Outbox { id: string; action_id: string; hook: string; url: string; created_at: string; idempotency_key?: string }
export interface ExecDetail { execution: Execution; diff: DiffRow[]; webhooks: { outbox: Outbox; deliveries: Delivery[] }[]; rollback: null | { id: string; original_action_id: string; rollback_action_id: string; actor: string; reason: string; at: string } }
export interface Approval { id: string; proposal_id: string; decision: "approved" | "rejected"; approver: string; approver_role: string; comment: string | null; decided_at: string; execution_id: string | null }
export interface Proposal {
  id: string; action: string; params: Record<string, unknown>; proposer: string; proposer_role: string; rationale: string | null;
  dry_run: DiffRow[] | { diff: DiffRow[]; scenario_id?: number; impact?: Impact } | null; scenario_id?: number; created_at: string; state?: "pending" | "approved" | "rejected"; approval?: Approval | null;
  on_behalf_of?: string | null; evidence?: Evidence[] | null; run_id?: string | null; expires_at?: string | null; expired?: boolean; stale?: boolean | null;
}
export interface Kpis { value_at_risk: number; delay_hours_total: number; shipments_delayed: number; shipments_in_transit: number; ports_closed: number; invoices_flagged: number; extra_cost: number; exposure_shipments: number; exposure_suppliers: number }
export interface Impact { before: { kpis: Kpis }; after: { kpis: Kpis }; delta: { kpis: Kpis } }
export interface Evidence { ref: string; claim: string; kind: "asserted" | "inferred"; tool_call_id?: string | null }
export interface AuditRow { kind: string; id: string; at: string; actor: string; role?: string; name: string; status: string; action_id?: string | null; proposal_id?: string | null; summary?: string; [k: string]: unknown }
export const dryImpact = (d: Proposal["dry_run"]): { scenario_id?: number; impact?: Impact } => (d && !Array.isArray(d) ? { scenario_id: (d as any).scenario_id, impact: (d as any).impact } : {});
export const dryDiff = (d: Proposal["dry_run"]): DiffRow[] => (Array.isArray(d) ? d : d && Array.isArray((d as { diff: DiffRow[] }).diff) ? (d as { diff: DiffRow[] }).diff : []);
export const paramList = (p: ActionInfo["params"], _name?: string): ParamMeta[] =>
  Array.isArray(p) ? p : p && typeof p === "object" && Object.keys(p).length ? Object.entries(p).map(([n, t]) => ({ name: n, type: String(t).replace(/[!?]/g, ""), required: String(t).endsWith("!") }))
    : [];

export const api = {
  simState: () => req<SimState>("/sim/state", { scenario: "main" }),
  simControl: (b: { speed?: Speed; step?: boolean; reset?: boolean }) => req<SimState>("/sim/control", { method: "POST", body: b }),
  inject: (b: { kind: string; target?: string; at?: string; duration_h?: number }) =>
    req<{ injected: { kind: string; target: string | null; at: string } }>("/sim/inject", { method: "POST", body: b }),
  geo: (type: string, asOf?: string, scn?: number | "main") => req<GeoCollection>(`/geo/${type}`, { params: { as_of: asOf }, scenario: scn }),
  roles: async (): Promise<RoleInfo[]> => (await req<(RoleInfo | string)[]>("/roles", { scenario: "main" })).map((r) => (typeof r === "string" ? { role: r, description: "", region: null, user: "" } : r)),
  types: () => req<TypesInfo>("/types", { scenario: "main" }),
  summary: (asOf?: string) => req<Summary>("/stats/summary", { params: { as_of: asOf } }),
  series: (metric: Metric, from: string, to: string, stepH = 24, scn?: number | "main") => req<Series>("/stats/series", { params: { metric, from, to, step_hours: stepH }, scenario: scn }),
  events: (to?: string, limit = 60) => req<EventRow[]>("/events", { params: { to, limit } }),
  graph: (center: string, depth: number, asOf: string | undefined, inferred: boolean, scn?: number | "main") =>
    req<GraphResp>("/graph", { params: { center, depth, as_of: asOf ?? "live", include_inferred: inferred }, scenario: scn }),
  me: () => req<Me>("/me", { scenario: "main" }),
  actions: (type: string, id: string) => req<ActionInfo[]>("/actions", { params: { type, id } }),
  execute: (name: string, params: Record<string, unknown>) => reqFull<Record<string, any>>(`/actions/${name}/execute`, { method: "POST", body: { params } }),
  propose: (name: string, params: Record<string, unknown>, rationale: string) => reqFull<Record<string, any>>(`/actions/${name}/propose`, { method: "POST", body: { params, rationale } }),
  proposals: (state: "pending" | "all") => req<Proposal[]>("/proposals", { params: { state } }),
  decide: (id: string, approve: boolean, comment: string) => req<Record<string, any>>(`/proposals/${id}/${approve ? "approve" : "reject"}`, { method: "POST", body: { comment } }),
  executions: (f: { name?: string; actor?: string; status?: string; limit?: number }) => req<Execution[]>("/executions", { params: { limit: 100, ...f } }),
  execution: (id: string) => req<ExecDetail>(`/executions/${id}`),
  rollback: (id: string, reason: string) => req<Record<string, any>>(`/executions/${id}/rollback`, { method: "POST", body: { reason } }),
  retryWebhook: (id: string) => req<Record<string, any>>(`/webhooks/${id}/retry`, { method: "POST" }),
  audit: (kind: string, limit = 100) => req<AuditRow[]>("/audit", { params: { kind, limit } }),
};

/* ------------------------------------------------------------------ runtime metadata ------------------------------------------------------------------ */
export interface RoleInfo { role: string; description: string; region: string | null; user: string }
export interface TypeInfo { name: string; label?: string; geo?: boolean; volatile?: boolean; properties: { name: string; type: string; required: boolean; enum: string[]; volatile?: boolean }[] }
export interface TypesInfo extends Omit<TypesResponse, "objectTypes"> { objectTypes: TypeInfo[] }

/* ------------------------------------------------------------------ auth ------------------------------------------------------------------ */
export const authApi = {
  login: async (user: string, password: string): Promise<AuthSession> => {
    const r = await req<AuthSession>("/auth/login", { method: "POST", body: { user, password }, home: true });
    setAuth(r); return r;
  },
  logout: () => setAuth(null),
};

/* ------------------------------------------------------------------ workspaces ------------------------------------------------------------------ */
export interface WorkspaceInfo { slug: string; schema: string; ontology_hash: string; imported_at: string | null; rows: number | null; api_base: string | null }

/* ------------------------------------------------------------------ ingest (Phase 6 contract) ------------------------------------------------------------------ */
export interface IngestSource { name: string; kind: string }
export interface ProfColumn {
  id: string; name: string; ident: string; type: "int" | "float" | "bool" | "timestamp" | "string" | "geojson"; null_rate: number; distinct: number; unique: boolean;
  min: string | null; max: string | null; top: [string, number][]; enum_candidate: string[] | null; format: string | null; geo: { role: string; pair: string | null } | null; time: boolean; notes: string[];
}
export interface ProfTable { id: string; name: string; ident: string; rows: number; sampled_rows: number; columns: ProfColumn[]; key_candidates: string[][]; is_join_table: boolean }
export interface ProfFk { id: string; from: string; to: string; declared: boolean; containment: number; name_score: number; confidence: number; cardinality: string; orphans: number }
export interface Profile {
  version: number; source: { kind: string; name: string; sha256: string; bytes: number | null };
  caps: Record<string, number>; truncated: boolean; seed: number; tables: ProfTable[]; fks: ProfFk[]; warnings: string[];
}
export interface ProposalProp { name: string; type: string | { enum: string[] }; required: boolean; column: string | null; transform: string; label?: string }
export interface ProposalType { name: string; label?: string; extends: string | null; key: string[]; source_table: string; properties: ProposalProp[] }
export interface ProposalLink { name: string; from: string; to: string; cardinality: string; inverse: string | null; transitive: boolean; required: boolean; source: { kind: string; [k: string]: unknown } }
export interface ProposalAction { name: string; description: string; kind: string; type: string; params: Record<string, unknown> }
export interface ProposalRole { name: string; description: string; access: "read" | "write" | "admin" }
export interface ProposalDoc {
  version: number; profile_hash: string; namespace: string; objectTypes: ProposalType[]; linkTypes: ProposalLink[]; classes: { name: string; extends: string | null }[];
  actions: ProposalAction[]; roles: ProposalRole[]; rationale: string; notes: string[];
}
export interface ValidationCheck { name: string; ok: boolean; value: number | string | boolean | null; threshold: number | string | null; detail: string; examples: string[] }
export interface ValidationReport { ok: boolean; checks: ValidationCheck[]; stats: { rows_loaded: Record<string, number>; links_loaded: Record<string, number> }; seconds: number }
/** engine.versioning.diff: kinds are objectTypes, properties (Type.prop), linkTypes, actions, roles, functions, webhooks. */
export interface OntologyDiff { added: Record<string, string[]>; removed: Record<string, string[]>; changed: Record<string, Record<string, Record<string, [unknown, unknown]>>>; breaking: string[]; sensitive: string[] }
export interface ProposalRow { id: string; seq: number; parent_id: string | null; kind: "baseline" | "model" | "repair" | "edit"; model: string | null; cost_usd: number | null; created_at: string; created_by?: string; ok?: boolean | null }
export interface IngestSessionInfo { id: string; source_kind: string; source_name: string; source_sha256: string; created_at: string; created_by?: string }
export interface IngestSession { session: IngestSessionInfo; profile: Profile; proposals: ProposalRow[] }
export interface ProposalDetail { id: string; kind?: ProposalRow["kind"]; session_id?: string; seq?: number; cost_usd?: number | null; model?: string | null; proposal: ProposalDoc; yaml: string; mapping: unknown; validation: ValidationReport | null; diff: OntologyDiff }
export interface JobEvent { t?: string; kind?: string; message?: string; [k: string]: unknown }
export interface ImportResult { workspace?: string; slug?: string; schema?: string; counts?: Record<string, number>; links?: Record<string, number>; dropped_links?: Record<string, number> | number; already_imported?: boolean; api_base?: string | null; seconds?: number }
export interface IngestJob { status: "queued" | "running" | "done" | "error"; events: (JobEvent | string)[]; proposal_id?: string | null; cost_usd?: number | null; result?: ImportResult | null; error?: string | null; progress?: number | null }
export type ItemEdit = { path: string; action: "accept" | "reject" | "edit"; value?: unknown };
export interface Decision { id: string; proposal_id: string; decision: "approved" | "rejected"; approver: string; comment: string | null; at: string }

/** Uploads the file as a raw body with progress (XMLHttpRequest: fetch has no upload progress). Mock mode simulates progress. */
export function uploadFile(file: File, onProgress: (frac: number) => void, signal?: AbortSignal): Promise<{ session_id: string; profile_summary?: Record<string, unknown> }> {
  const url = `${HOME_BASE}/ingest/uploads?filename=${encodeURIComponent(file.name)}`;
  const headers: Record<string, string> = { "x-role": curRole, "content-type": "application/octet-stream", ...authHeader() };
  const fail = (status: number, raw: string) => {
    let body: unknown = raw; try { body = JSON.parse(raw); } catch { /* text */ }
    const d = body && typeof body === "object" && "detail" in (body as object) ? (body as { detail: unknown }).detail : body;
    return new ApiError(status, body, `API ${status}: ${typeof d === "string" ? d : JSON.stringify(d)}`);
  };
  if (MOCK) {
    return (async () => {
      for (let i = 1; i <= 5; i++) { if (signal?.aborted) throw new DOMException("aborted", "AbortError"); onProgress(i / 5); await new Promise((r) => setTimeout(r, 120)); }
      const r = await fetch(url, { method: "POST", headers, body: file, signal });
      const raw = await r.text();
      if (!r.ok) throw fail(r.status, raw);
      return JSON.parse(raw);
    })();
  }
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    for (const [k, v] of Object.entries(headers)) x.setRequestHeader(k, v);
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => { if (x.status === 401) window.dispatchEvent(new CustomEvent("isnad-unauthorized")); x.status >= 200 && x.status < 300 ? resolve(JSON.parse(x.responseText)) : reject(fail(x.status, x.responseText)); };
    x.onerror = () => reject(new Error("Network error during upload"));
    x.onabort = () => reject(new DOMException("aborted", "AbortError"));
    signal?.addEventListener("abort", () => x.abort());
    x.send(file);
  });
}

const H = { home: true, scenario: "main" as const };
export const ingestApi = {
  sources: () => req<IngestSource[]>("/ingest/sources", H),
  profileSource: (name: string) => req<{ session_id: string }>(`/ingest/sources/${encodeURIComponent(name)}/profile`, { method: "POST", ...H }),
  session: async (id: string): Promise<IngestSession> => {
    const r = await req<any>(`/ingest/sessions/${id}`, H);
    const session: IngestSessionInfo = r.session ?? r;
    return { session, profile: r.profile ?? r.session?.profile, proposals: r.proposals ?? [] };
  },
  createProposal: (sid: string, mode: "baseline" | "model") => req<{ job_id: string }>(`/ingest/sessions/${sid}/proposals`, { method: "POST", body: { mode }, ...H }),
  job: (id: string) => req<IngestJob>(`/ingest/jobs/${id}`, H),
  proposal: (id: string) => req<ProposalDetail>(`/ingest/proposals/${id}`, H),
  repair: (id: string) => req<{ job_id: string }>(`/ingest/proposals/${id}/repair`, { method: "POST", ...H }),
  patchItems: (id: string, items: ItemEdit[]) => req<{ id?: string; proposal_id?: string }>(`/ingest/proposals/${id}/items`, { method: "PATCH", body: { items }, ...H }),
  approve: (id: string, comment: string) => req<Decision>(`/ingest/proposals/${id}/approve`, { method: "POST", body: { comment }, ...H }),
  runImport: (decisionId: string) => req<{ job_id: string }>(`/ingest/decisions/${decisionId}/import`, { method: "POST", ...H }),
  workspaces: () => req<WorkspaceInfo[]>("/workspaces", H),
};

/* ------------------------------------------------------------------ ontology versioning (admin) ------------------------------------------------------------------ */
export interface OntVersion { id: number; hash: string; parent_hash: string | null; created_by: string; created_at: string; source: "startup" | "proposal" | "rollback" | "import"; note: string | null }
export interface OntProposal { id: string; base_hash: string; breaking: boolean; rationale: string | null; proposer: string; created_at: string; diff: OntologyDiff; decision: "approved" | "rejected" | null; approver: string | null; version_id: number | null; decision_comment?: string | null }
const M = { scenario: "main" as const };
export const ontApi = {
  versions: () => req<OntVersion[]>("/ontology/versions", M),
  version: (id: number) => req<OntVersion & { yaml: string }>(`/ontology/versions/${id}`, M),
  diff: (from: number, to: number) => req<OntologyDiff>("/ontology/diff", { params: { from, to }, ...M }),
  proposals: () => req<OntProposal[]>("/ontology/proposals", M),
  decide: (id: string, approve: boolean, comment: string) => req<Record<string, any>>(`/ontology/proposals/${id}/${approve ? "approve" : "reject"}`, { method: "POST", body: { comment }, ...M }),
  rollback: (vid: number) => req<Record<string, any>>(`/ontology/versions/${vid}/rollback`, { method: "POST", ...M }),
};
