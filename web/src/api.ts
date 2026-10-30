import { OntologyClient } from "@sdk/client";
import { SELECTABLE_ROLES, ACTION_META } from "@sdk/client";
import type { Role, RoleName, ActionMeta } from "@sdk/client";

const env = (import.meta as any).env ?? {};
export const BASE: string = env.VITE_API ?? "http://localhost:8000";
export const MOCK: boolean = !!env.VITE_MOCK && env.VITE_MOCK !== "0";
export const client = new OntologyClient(BASE);

export const ROLE_LIST: RoleName[] = SELECTABLE_ROLES;
let curRole: Role = "admin";
export const getRole = () => curRole;
export function setRole(r: Role) { curRole = r; client.setRole(r); }
setRole(curRole);

export type Speed = "paused" | "1" | "6" | "24";
export interface SimState {
  sim_now: string; epoch: string; speed: Speed; running: boolean; seed: number; tick: number;
  achieved_hours_per_s: number | null; last_error: string | null;
}
export interface GeoFeature {
  type: "Feature"; id: string; geometry: { type: "Point"; coordinates: [number, number] };
  properties: { id: string; type: string; name?: string; ref?: string; status?: string; origin?: [number, number] | null; destination?: [number, number] | null; [k: string]: unknown };
}
export interface GeoCollection { type: "FeatureCollection"; features: GeoFeature[] }
export interface Summary {
  as_of: string; shipments: { planned: number; in_transit: number; delayed: number; arrived: number };
  ports: { open: number; closed: number }; invoices: { open: number; approved: number; paid: number; flagged: number };
  value_at_risk: number; delay_hours_total: number; open_events: number;
}
export type Metric = "shipments_in_transit" | "shipments_delayed" | "ports_closed" | "invoices_flagged" | "value_at_risk";
export interface Series { metric: Metric; points: { t: string; v: number }[] }
export interface EventRow {
  seq: number; action_id: string; name: string; actor: string; source: string; sim_time: string; recorded_at: string;
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
export async function reqFull<T>(path: string, o: { method?: string; body?: unknown; params?: Params } = {}): Promise<{ status: number; data: T }> {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(o.params ?? {})) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const q = u.toString();
  const r = await fetch(BASE.replace(/\/$/, "") + path + (q ? `?${q}` : ""), {
    method: o.method ?? "GET",
    headers: { "content-type": "application/json", "x-role": curRole },
    body: o.body === undefined ? undefined : JSON.stringify(o.body),
  });
  if (!r.ok) {
    const raw = await r.text();
    let body: unknown = raw;
    try { body = JSON.parse(raw); } catch { /* text */ }
    const d = body && typeof body === "object" && "detail" in (body as object) ? (body as { detail: unknown }).detail : body;
    throw new ApiError(r.status, body, `API ${r.status}: ${typeof d === "string" ? d : JSON.stringify(d)}`);
  }
  return { status: r.status, data: (await r.json()) as T };
}
export const req = async <T,>(path: string, o: { method?: string; body?: unknown; params?: Params } = {}): Promise<T> => (await reqFull<T>(path, o)).data;
export const isNotFound = (e: unknown) => (e instanceof ApiError ? e.status === 404 : /API 404/.test(String((e as Error)?.message ?? e)));

export interface Me { role: string; user: string; region: string | null; types: Record<string, { read: boolean; write: boolean; masked: string[] }>; actions: string[] }
export interface ParamMeta { name: string; type: string; ref?: string | null; required?: boolean }
export interface ActionInfo { name: string; description: string; params: ParamMeta[] | Record<string, string>; requires_approval: boolean; allowed: boolean; reason: string | null }
export interface DiffRow { kind: "object" | "link"; type: string; key: string; op: "create" | "modify" | "delete"; before: Record<string, unknown> | null; after: Record<string, unknown> | null }
export interface Execution {
  action_id: string; name: string; actor: string; role?: string; source?: string; status: string; params: Record<string, unknown>;
  sim_time?: string; recorded_at: string; proposal_id?: string | null; parent_execution_id?: string | null; rolled_back?: boolean; error?: string | null; on_behalf_of?: string | null; tool_trace?: unknown;
}
export interface Delivery { attempt: number; status: "pending" | "delivered" | "retry" | "dead"; http_status: number | null; error: string | null; next_attempt_at?: string | null; at: string }
export interface Outbox { id: string; action_id: string; hook: string; url: string; created_at: string; idempotency_key?: string }
export interface ExecDetail { execution: Execution; diff: DiffRow[]; webhooks: { outbox: Outbox; deliveries: Delivery[] }[]; rollback: null | { id: string; original_action_id: string; rollback_action_id: string; actor: string; reason: string; at: string } }
export interface Approval { id: string; proposal_id: string; decision: "approved" | "rejected"; approver: string; approver_role: string; comment: string | null; decided_at: string; execution_id: string | null }
export interface Proposal {
  id: string; action: string; params: Record<string, unknown>; proposer: string; proposer_role: string; rationale: string | null;
  dry_run: DiffRow[] | { diff: DiffRow[] } | null; created_at: string; state?: "pending" | "approved" | "rejected"; approval?: Approval | null;
  on_behalf_of?: string | null; evidence?: Evidence[] | null; run_id?: string | null; expires_at?: string | null; expired?: boolean; stale?: boolean | null;
}
export interface Evidence { ref: string; claim: string; kind: "asserted" | "inferred"; tool_call_id?: string | null }
export interface AuditRow { kind: string; id: string; at: string; actor: string; role?: string; name: string; status: string; action_id?: string | null; proposal_id?: string | null; summary?: string; [k: string]: unknown }
export const dryDiff = (d: Proposal["dry_run"]): DiffRow[] => (Array.isArray(d) ? d : d && Array.isArray((d as { diff: DiffRow[] }).diff) ? (d as { diff: DiffRow[] }).diff : []);
export const paramList = (p: ActionInfo["params"], name: string): ParamMeta[] =>
  Array.isArray(p) ? p : p && typeof p === "object" && Object.keys(p).length ? Object.entries(p).map(([n, t]) => ({ name: n, type: String(t).replace(/[!?]/g, ""), required: String(t).endsWith("!") }))
    : (ACTION_META as Record<string, ActionMeta | undefined>)[name]?.params ?? [];

export const api = {
  simState: () => req<SimState>("/sim/state"),
  simControl: (b: { speed?: Speed; step?: boolean; reset?: boolean }) => req<SimState>("/sim/control", { method: "POST", body: b }),
  inject: (b: { kind: string; target?: string; at?: string; duration_h?: number }) =>
    req<{ injected: { kind: string; target: string | null; at: string } }>("/sim/inject", { method: "POST", body: b }),
  geo: (type: "Port" | "Warehouse" | "Shipment", asOf?: string) => req<GeoCollection>(`/geo/${type}`, { params: { as_of: asOf } }),
  summary: (asOf?: string) => req<Summary>("/stats/summary", { params: { as_of: asOf } }),
  series: (metric: Metric, from: string, to: string, stepH = 24) => req<Series>("/stats/series", { params: { metric, from, to, step_hours: stepH } }),
  events: (to?: string, limit = 60) => req<EventRow[]>("/events", { params: { to, limit } }),
  graph: (center: string, depth: number, asOf: string | undefined, inferred: boolean) =>
    req<GraphResp>("/graph", { params: { center, depth, as_of: asOf ?? "live", include_inferred: inferred } }),
  me: () => req<Me>("/me"),
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
