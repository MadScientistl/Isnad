import { OntologyClient } from "@sdk/client";
import type { Role } from "@sdk/client";

const env = (import.meta as any).env ?? {};
export const BASE: string = env.VITE_API ?? "http://localhost:8000";
export const MOCK: boolean = !!env.VITE_MOCK && env.VITE_MOCK !== "0";
export const client = new OntologyClient(BASE);

let curRole: Role = "admin";
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
export async function req<T>(path: string, o: { method?: string; body?: unknown; params?: Params } = {}): Promise<T> {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(o.params ?? {})) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const q = u.toString();
  const r = await fetch(BASE.replace(/\/$/, "") + path + (q ? `?${q}` : ""), {
    method: o.method ?? "GET",
    headers: { "content-type": "application/json", "x-role": curRole },
    body: o.body === undefined ? undefined : JSON.stringify(o.body),
  });
  if (!r.ok) {
    let d: unknown = await r.text();
    try { d = (JSON.parse(d as string) as { detail?: unknown }).detail ?? d; } catch { /* text */ }
    throw new Error(`API ${r.status}: ${typeof d === "string" ? d : JSON.stringify(d)}`);
  }
  return r.json() as Promise<T>;
}

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
};
