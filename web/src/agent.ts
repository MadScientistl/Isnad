import { getBase, ApiError, getRole, req, scnHeaders } from "./api";

/** Phase 4 agent types and client. All payload text is untrusted: render it as text only. */
export type RunStatus = "running" | "completed" | "capped" | "error" | "cancelled" | "interrupted";
export interface Caps { max_turns: number; max_tool_calls: number; max_output_tokens: number; max_cost_usd: number; max_wall_s: number }
export interface AgentAction { name: string; description: string; params: Record<string, string>; requires_approval: boolean; approvers: string[] }
export interface AgentConfig {
  role: string; user: string; available: boolean; tools: string[]; actions: AgentAction[]; caps: Caps;
  limits: { max_concurrent: number; runs_per_minute: number; daily_usd: number; max_global: number; prompt_max_chars: number };
}
export interface Run {
  id: string; role: string; user: string; prompt: string; model: string; caps: Caps; started_at: string; status: RunStatus;
  summary: string | null; cap: string | null; error: string | null; tokens_in: number; tokens_out: number; cost_usd: number;
  turns: number; tool_calls: number; proposals: number; proposal_ids: string[]; scenario_id?: number | null; scenario_ids?: number[];
}
export interface AgentEvent { seq: number; kind: "delta" | "message" | "tool_call" | "tool_result" | "denied" | "proposal" | "finished"; payload: any; tokens_in?: number; tokens_out?: number; cost_usd?: number; at?: string }
export interface Provenance { asserted?: string[]; inferred?: { edge: string; rule: string; support: string[] }[] }

export const agentApi = {
  config: () => req<AgentConfig>("/agent/config"),
  runs: (limit = 50) => req<Run[]>("/agent/runs", { params: { limit } }),
  run: (id: string) => req<{ run: Run; events: AgentEvent[] }>(`/agent/runs/${id}`),
  start: (prompt: string) => req<{ run_id: string }>("/agent/runs", { method: "POST", body: { prompt } }),
  cancel: (id: string) => req<{ run_id: string; status: string }>(`/agent/runs/${id}/cancel`, { method: "POST" }),
};

/** Readable message for a failed start; the backend codes are rate_limited, agent_unavailable, no_agent and 422. */
export function startError(e: unknown): string {
  if (!(e instanceof ApiError)) return String((e as Error)?.message ?? e);
  if (e.status === 429) { const s = e.payload?.retry_after; return `${e.text || "Rate limited."}${s ? ` Try again in ${s}s.` : ""}`; }
  if (e.status === 503 || e.code === "agent_unavailable") return "The agent is unavailable (no model API key is configured on the server).";
  if (e.status === 422) return "The prompt was rejected: it must be between 1 and 4000 characters.";
  if (e.status === 403) return e.code === "no_agent" ? "This role has no agent." : `Not allowed: ${e.text}`;
  return e.text;
}

/** Incremental SSE parser: feed text chunks, get complete frames. Handles split chunks, CRLF, comments and multi-line data. */
export class SseParser {
  private buf = ""; private id = ""; private event = ""; private data: string[] = [];
  constructor(private onFrame: (f: { id: string; event: string; data: string }) => void) {}
  push(chunk: string) {
    this.buf += chunk;
    let i: number;
    while ((i = this.buf.search(/\r\n|\n|\r/)) >= 0) {
      const m = this.buf.slice(i).match(/^(\r\n|\n|\r)/)!;
      if (m[0] === "\r" && i + 1 === this.buf.length) return; // could be a CRLF split across chunks
      const line = this.buf.slice(0, i); this.buf = this.buf.slice(i + m[0].length);
      this.line(line);
    }
  }
  private line(l: string) {
    if (l === "") { if (this.data.length) this.onFrame({ id: this.id, event: this.event || "message", data: this.data.join("\n") }); this.event = ""; this.data = []; return; }
    if (l.startsWith(":")) return;
    const c = l.indexOf(":"); const f = c < 0 ? l : l.slice(0, c); let v = c < 0 ? "" : l.slice(c + 1); if (v.startsWith(" ")) v = v.slice(1);
    if (f === "id") this.id = v; else if (f === "event") this.event = v; else if (f === "data") this.data.push(v);
  }
}

/**
 * Stream a run with fetch (EventSource cannot send x-role). Reconnects from the last seen non-delta seq until `finished`
 * arrives or the signal aborts. Returns when finished or aborted; throws only on an HTTP error (404 etc.).
 */
export async function streamRun(runId: string, onEvent: (e: AgentEvent) => void, signal: AbortSignal, onState?: (s: "open" | "retry") => void): Promise<void> {
  let last = 0, done = false, fails = 0;
  while (!done && !signal.aborted) {
    try {
      const r = await fetch(`${getBase()}/agent/runs/${runId}/stream${last ? `?after=${last}` : ""}`, {
        headers: { "x-role": getRole(), ...scnHeaders(`/agent/runs/${runId}/stream`), accept: "text/event-stream", ...(last ? { "last-event-id": String(last) } : {}) }, signal,
      });
      if (!r.ok || !r.body) {
        if (r.status >= 400 && r.status < 500) throw new ApiError(r.status, await r.text(), `stream ${r.status}`);
        throw new Error(`stream ${r.status}`);
      }
      onState?.("open"); fails = 0;
      const p = new SseParser((f) => {
        let ev: AgentEvent; try { ev = JSON.parse(f.data); } catch { return; }
        if (ev.kind !== "delta") { if (ev.seq <= last) return; last = ev.seq; }
        if (ev.kind === "finished") done = true;
        onEvent(ev);
      });
      const rd = r.body.getReader(), dec = new TextDecoder();
      for (;;) { const { value, done: d } = await rd.read(); if (d) break; p.push(dec.decode(value, { stream: true })); }
    } catch (e) {
      if (signal.aborted) return;
      if (e instanceof ApiError) throw e;
      if (++fails > 6) throw e;
    }
    if (!done && !signal.aborted) { onState?.("retry"); await new Promise((res) => setTimeout(res, Math.min(4000, 400 * 2 ** fails))); }
  }
}

/* ---- transcript model ---- */
export interface CallItem {
  type: "call"; id: string; seq: number; name: string; input: unknown; flags: string[];
  result?: { ok: boolean; data: unknown; untrusted?: boolean; provenance?: Provenance; truncated?: boolean; error?: string | null; flags: string[] };
  denied?: { kind: string; reason: string };
}
export type Item =
  | { type: "text"; seq: number; turn: number; text: string; stop?: string }
  | CallItem
  | { type: "proposal"; seq: number; proposal_id: string; action: string; params: Record<string, unknown>; expires_at: string | null }
  | { type: "finished"; seq: number; status: RunStatus; summary: string | null; cap: string | null; error: string | null };
export interface Transcript {
  items: Item[]; live: string; tokens_in: number; tokens_out: number; cost: number; turns: number; toolCalls: number; proposals: number; finished: boolean; lastSeq: number;
}
export const emptyTranscript = (): Transcript => ({ items: [], live: "", tokens_in: 0, tokens_out: 0, cost: 0, turns: 0, toolCalls: 0, proposals: 0, finished: false, lastSeq: 0 });

/** Pure reducer. Orders by arrival, dedupes by seq (never assumes contiguity), and drops live delta text once the message lands. */
export function applyEvent(t: Transcript, e: AgentEvent): Transcript {
  if (e.kind === "delta") return { ...t, live: t.live + String(e.payload?.text ?? "") };
  if (e.seq <= t.lastSeq) return t;
  const n: Transcript = { ...t, items: [...t.items], lastSeq: e.seq, tokens_in: t.tokens_in + (e.tokens_in ?? 0), tokens_out: t.tokens_out + (e.tokens_out ?? 0), cost: t.cost + (e.cost_usd ?? 0) };
  const p = e.payload ?? {};
  switch (e.kind) {
    case "message":
      n.live = ""; n.turns = Math.max(n.turns, Number(p.turn ?? n.turns + 1));
      if (p.text) n.items.push({ type: "text", seq: e.seq, turn: p.turn, text: String(p.text), stop: p.stop_reason });
      break;
    case "tool_call":
      n.toolCalls += 1;
      n.items.push({ type: "call", id: p.tool_call_id, seq: e.seq, name: p.name, input: p.input, flags: p.flags ?? [] });
      break;
    case "tool_result": {
      const i = n.items.findIndex((x) => x.type === "call" && x.id === p.tool_call_id);
      const res = { ok: !!p.ok, data: p.result?.data, untrusted: p.result?.untrusted, provenance: p.result?.provenance, truncated: p.result?.truncated, error: p.result?.error, flags: p.flags ?? [] };
      if (i >= 0) n.items[i] = { ...(n.items[i] as CallItem), result: res };
      else n.items.push({ type: "call", id: p.tool_call_id, seq: e.seq, name: p.name, input: undefined, flags: [], result: res });
      break;
    }
    case "denied": {
      const i = n.items.findIndex((x) => x.type === "call" && x.id === p.tool_call_id);
      const d = { kind: String(p.kind ?? ""), reason: String(p.reason ?? "") };
      if (i >= 0) n.items[i] = { ...(n.items[i] as CallItem), denied: d };
      else { n.toolCalls += 1; n.items.push({ type: "call", id: p.tool_call_id, seq: e.seq, name: p.name, input: undefined, flags: [], denied: d }); }
      break;
    }
    case "proposal":
      n.proposals += 1;
      n.items.push({ type: "proposal", seq: e.seq, proposal_id: p.proposal_id, action: p.action, params: p.params ?? {}, expires_at: p.expires_at ?? null });
      break;
    case "finished":
      n.finished = true; n.live = "";
      n.items.push({ type: "finished", seq: e.seq, status: p.status, summary: p.summary ?? null, cap: p.cap ?? null, error: p.error ?? null });
      n.turns = Number(p.turns ?? n.turns); n.toolCalls = Number(p.tool_calls ?? n.toolCalls); n.proposals = Number(p.proposals ?? n.proposals);
      break;
  }
  return n;
}

export const money = (x: number) => `$${x.toFixed(x < 0.1 ? 4 : 2)}`;
/** Seconds until an ISO time (negative when past). */
export const secsUntil = (iso: string | null | undefined, now = Date.now()) => (iso ? Math.round((Date.parse(iso) - now) / 1000) : null);
export const fmtCountdown = (s: number) => { const a = Math.abs(s), h = Math.floor(a / 3600), m = Math.floor((a % 3600) / 60); return h ? `${h}h ${m}m` : m ? `${m}m ${a % 60}s` : `${a}s`; };
