import { req, reqFull, type DiffRow, type Kpis, type Series, type Metric } from "./api";

/** Phase 5 scenario types and client, built on the frozen contract in docs/contracts.md. */
export type ScenarioStatus = "open" | "merged" | "discarded" | "expired";
export interface Scenario {
  id: number; name: string; owner_user: string; owner_role: string; status: ScenarioStatus; base_recorded: string; base_sim_time: string; clock: string | null;
  shared_roles: string[]; created_at: string; expires_at: string | null; run_id: string | null; override_rows: number; data_version: number;
}
export interface CompareResp { a: Scenario | { id: number | "main" }; b: Scenario | { id: number | "main" }; diff: DiffRow[]; kpis: { a: Kpis; b: Kpis; delta: Kpis }; truncated: boolean }
export interface MergeConflict { kind: string; type: string; key: string; reason: string }
export interface MergeOk { status: "merged"; merged: { name: string; action_id?: string | null; proposal_id?: string | null }[] }
export interface Injection { kind: "port_closure" | "shipment_delay" | "supplier_delay" | "price_shock" | "invoice_anomaly"; target?: string; at_h?: number; duration_h?: number; magnitude?: number }
export interface SimJob { status: "running" | "done" | "error"; sim_now?: string; digest?: string; actions?: Record<string, number>; kpis?: Kpis; error?: string | null }
export const INJECTION_KINDS: Injection["kind"][] = ["port_closure", "shipment_delay", "supplier_delay", "price_shock", "invoice_anomaly"];

/** KPI metadata: label, formatting and which direction is good (null = neutral). */
export const KPI_META: { k: keyof Kpis; label: string; good: "down" | "up" | null; fmt: "int" | "money" | "hours" }[] = [
  { k: "value_at_risk", label: "Value at risk", good: "down", fmt: "money" },
  { k: "delay_hours_total", label: "Delay hours", good: "down", fmt: "hours" },
  { k: "shipments_delayed", label: "Delayed shipments", good: "down", fmt: "int" },
  { k: "shipments_in_transit", label: "In transit", good: null, fmt: "int" },
  { k: "ports_closed", label: "Ports closed", good: "down", fmt: "int" },
  { k: "invoices_flagged", label: "Flagged invoices", good: "down", fmt: "int" },
  { k: "extra_cost", label: "Extra cost", good: "down", fmt: "money" },
  { k: "exposure_shipments", label: "Exposed shipments", good: "down", fmt: "int" },
  { k: "exposure_suppliers", label: "Exposed suppliers", good: "down", fmt: "int" },
];
export const fmtKpi = (v: number | undefined | null, f: "int" | "money" | "hours") => {
  if (v === undefined || v === null || Number.isNaN(v)) return "n/a";
  if (f === "money") return Math.abs(v) >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : Math.abs(v) >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(Math.round(v));
  return String(Math.round(v * 10) / 10);
};
export const deltaCls = (d: number, good: "down" | "up" | null) => (d === 0 ? "" : good === null ? "neutral" : (d < 0) === (good === "down") ? "good" : "bad");
export const fmtDelta = (d: number, f: "int" | "money" | "hours") => (d === 0 ? "0" : `${d > 0 ? "+" : "-"}${fmtKpi(Math.abs(d), f)}`);

export const scnApi = {
  list: (status: "open" | "all" = "open") => req<Scenario[]>("/scenarios", { params: { status } }),
  get: (id: number) => req<Scenario>(`/scenarios/${id}`),
  create: (b: { name: string; expires_in_h?: number; shared_roles?: string[] }) => req<Scenario>("/scenarios", { method: "POST", body: b }),
  discard: (id: number) => req<Record<string, unknown>>(`/scenarios/${id}`, { method: "DELETE" }),
  compare: (id: number, withS: "main" | number) => req<CompareResp>(`/scenarios/${id}/compare`, { params: { with: String(withS) } }),
  merge: (id: number, comment?: string) => reqFull<MergeOk>(`/scenarios/${id}/merge`, { method: "POST", body: { comment } }),
  simulate: (id: number, b: { days: number; seed?: number; injections: Injection[] }) => req<{ job_id: string }>(`/scenarios/${id}/simulate`, { method: "POST", body: b }),
  job: (id: number, job: string) => req<SimJob>(`/scenarios/${id}/simulate/${job}`),
  series: (id: number, metric: Metric, stepH = 24) => req<Series>(`/scenarios/${id}/series`, { params: { metric, step_hours: stepH } }),
};

/** Parse `#/scenarios/<id>/compare?with=main|<id>` into its parts. */
export function parseCompareHash(h = location.hash): { id: number | null; withS: "main" | number } {
  const [path, qs] = h.replace(/^#\/?/, "").split("?");
  const parts = path.split("/");
  const id = parts[1] && /^\d+$/.test(parts[1]) ? Number(parts[1]) : null;
  const w = new URLSearchParams(qs ?? "").get("with");
  return { id, withS: w && /^\d+$/.test(w) ? Number(w) : "main" };
}
export const compareHref = (id: number, withS: "main" | number = "main") => `#/scenarios/${id}/compare?with=${withS}`;
