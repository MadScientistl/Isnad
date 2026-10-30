/**
 * Mock governance layer (Phase 3 contract): /me, /roles, /actions, executions, proposals, approvals, rollback,
 * webhooks and the unified /audit feed. Role-aware: deny by default, approval flow with proposer != approver,
 * rollback conflicts, and webhook delivery state (one seeded dead delivery that can be retried).
 * Wired from ./index.ts through setWorld() so this file does not import the world model.
 */
import { ACTION_META } from "@sdk/client";

type Json = Record<string, any>;
interface World {
  apply(name: string, p: Json): { diff: any[]; undo: () => void } | { fail: string[]; notFound?: boolean };
  current(type: string, id: string): Json | null;
  whoAmI(role: string): Json;
  nowIso(): string;
  visibleKey(type: string, id: string): boolean;
  canSeeObj(role: string, type: string, id: string): boolean;
}
let W: World;
export const setWorld = (w: World) => { W = w; seed(); };

const ROLES = ["admin", "finance_analyst", "planner", "regional_ops_lead"];
const ACTION_TYPE: Record<string, string> = { port: "Port", shipment: "Shipment", invoice: "Invoice", supplier: "Supplier" };
const user = (r: string) => `user:${r}`;
let n = 0;
const gid = (p: string) => `${p}${(++n).toString(16).padStart(4, "0")}0000-0000-4000-8000-${String(n).padStart(12, "0")}`;

interface Delivery { attempt: number; status: string; http_status: number | null; error: string | null; next_attempt_at: string | null; at: string }
interface Exec { action_id: string; name: string; actor: string; role: string; source: string; status: string; params: Json; sim_time: string; recorded_at: string; proposal_id: string | null; parent_execution_id: string | null; rolled_back: boolean; error: string | null; diff: any[]; undo?: () => void }
interface Outbox { id: string; action_id: string; hook: string; url: string; created_at: string; idempotency_key: string; deliveries: Delivery[] }
interface Prop { id: string; action: string; params: Json; proposer: string; proposer_role: string; rationale: string; dry_run: any[]; created_at: string; on_behalf_of?: string | null; evidence?: Json[] | null; run_id?: string | null; expires_at?: string | null; staleKeys?: string[]; approval: null | { id: string; proposal_id: string; decision: string; approver: string; approver_role: string; comment: string; decided_at: string; execution_id: string | null } }
const execs: Exec[] = [], outbox: Outbox[] = [], props: Prop[] = [], rollbacks: Json[] = [];

const err = (status: number, code: string, detail: string, extra: Json = {}): [number, unknown] => [status, { detail: { code, detail, ...extra } }];
const roleMayExec = (role: string, a: string) => (ACTION_META as Record<string, any>)[a]?.roles.includes(role);
const needsApproval = (a: string) => !!(ACTION_META as Record<string, any>)[a]?.requiresApproval;
const approversOf = (a: string): string[] => { const x = (ACTION_META as Record<string, any>)[a]?.approvers; return x?.length ? x : ["admin"]; };
const hooksOf = (a: string): string[] => (ACTION_META as Record<string, any>)[a]?.webhooks ?? [];
const targetOf = (name: string, p: Json): [string, string] | null => {
  for (const k of Object.keys(p)) if (ACTION_TYPE[k] && typeof p[k] === "string") return [ACTION_TYPE[k], p[k]];
  return name ? null : null;
};

function record(e: Partial<Exec> & { name: string; actor: string; role: string; params: Json }, hours?: string): Exec {
  const now = hours ?? W.nowIso();
  const x: Exec = { action_id: gid("a"), source: "api", status: "executed", sim_time: now, recorded_at: now, proposal_id: null, parent_execution_id: null, rolled_back: false, error: null, diff: [], ...e };
  execs.unshift(x); return x;
}
function addHooks(x: Exec, outcomes?: { status: string; http: number | null; error: string | null }[][]) {
  hooksOf(x.name).forEach((hook, i) => {
    const o: Outbox = { id: gid("b"), action_id: x.action_id, hook, url: `https://hooks.example.test/${hook}`, created_at: x.recorded_at, idempotency_key: `${x.action_id}:${hook}`, deliveries: [] };
    const seq = outcomes?.[i] ?? [{ status: "delivered", http: 200, error: null }];
    seq.forEach((d, k) => o.deliveries.push({ attempt: k + 1, status: d.status, http_status: d.http, error: d.error, next_attempt_at: null, at: x.recorded_at }));
    outbox.unshift(o);
  });
}

function seed() {
  if (execs.length) return;
  const T = (h: number) => new Date(Date.parse("2026-01-01T00:00:00Z") + h * 3600000).toISOString();
  const obj = (t: string, k: number) => `${{ Port: "a1", Invoice: "d4", Shipment: "c3" }[t]}000000-0000-4000-8000-${String(k).padStart(12, "0")}`;
  record({ name: "seed", actor: "system:seed", role: "admin", source: "seed", params: { objects: 62, links: 71 } }, T(0));
  const flag = record({ name: "flag_invoice", actor: user("finance_analyst"), role: "finance_analyst", params: { invoice: obj("Invoice", 3), anomaly: "duplicate billing" },
    diff: [{ kind: "object", type: "Invoice", key: obj("Invoice", 3), op: "modify", before: { status: "open", anomaly: null }, after: { status: "flagged", anomaly: "duplicate billing" } }] }, T(40));
  const rb = record({ name: "rollback", actor: user("admin"), role: "admin", params: { original: flag.action_id, reason: "Flag raised on the wrong invoice" }, parent_execution_id: flag.action_id,
    diff: [{ kind: "object", type: "Invoice", key: obj("Invoice", 3), op: "modify", before: { status: "flagged", anomaly: "duplicate billing" }, after: { status: "open", anomaly: null } }] }, T(41));
  flag.rolled_back = true;
  rollbacks.push({ id: gid("r"), original_action_id: flag.action_id, rollback_action_id: rb.action_id, actor: user("admin"), reason: "Flag raised on the wrong invoice", at: T(41) });
  const cp = record({ name: "close_port", actor: user("planner"), role: "planner", params: { port: obj("Port", 6), reason: "storm", duration_h: 48 },
    diff: [{ kind: "object", type: "Port", key: obj("Port", 6), op: "modify", before: { status: "open" }, after: { status: "closed" } }] }, T(60));
  addHooks(cp, [[{ status: "retry", http: 503, error: "upstream 503" }, { status: "retry", http: 503, error: "upstream 503" }, { status: "dead", http: 500, error: "upstream 500, max attempts reached" }]]);
  record({ name: "close_port", actor: user("finance_analyst"), role: "finance_analyst", params: { port: obj("Port", 1) }, status: "denied", error: "role finance_analyst may not execute close_port" }, T(70));
  const ds = record({ name: "delay_shipment", actor: user("regional_ops_lead"), role: "regional_ops_lead", params: { shipment: obj("Shipment", 2), delay_hours: 12, reason: "customs" } , status: "denied", error: "role regional_ops_lead may not execute delay_shipment" }, T(75));
  void ds;
  const pr: Prop = { id: gid("p"), action: "pay_invoice", params: { invoice: obj("Invoice", 4) }, proposer: user("finance_analyst"), proposer_role: "finance_analyst", rationale: "Goods received, three-way match done.",
    dry_run: [{ kind: "object", type: "Invoice", key: obj("Invoice", 4), op: "modify", before: { status: "open" }, after: { status: "paid" } }], created_at: T(80), approval: null };
  props.unshift(pr);
  const rej: Prop = { id: gid("p"), action: "pay_invoice", params: { invoice: obj("Invoice", 5) }, proposer: user("finance_analyst"), proposer_role: "finance_analyst", rationale: "Pay early for discount.",
    dry_run: [], created_at: T(50), approval: { id: gid("c"), proposal_id: "", decision: "rejected", approver: user("admin"), approver_role: "admin", comment: "Discount terms not confirmed", decided_at: T(52), execution_id: null } };
  rej.approval!.proposal_id = rej.id; props.push(rej);
  const W0 = Date.now(), ev = (ref: string, claim: string, kind: string, tc: string) => ({ ref, claim, kind, tool_call_id: tc });
  props.unshift({ id: gid("p"), action: "close_port", params: { port: obj("Port", 2), reason: "storm", duration_h: 24 }, proposer: "agent:planner", proposer_role: "planner", rationale: "Open storm event at this port; six shipments route through it.",
    dry_run: [{ kind: "object", type: "Port", key: obj("Port", 2), op: "modify", before: { status: "open" }, after: { status: "closed" } }], created_at: T(90), on_behalf_of: user("planner"), run_id: "e0000000-0000-4000-8000-000000000001",
    evidence: [ev(`Event/${obj("Port", 2).slice(0, 8)}`, "Storm event is open and affects the port", "asserted", "tc_1"), ev(`Port/${obj("Port", 2).slice(0, 8)}`, "Shipments exposed via supplies chain", "inferred", "tc_2")],
    expires_at: new Date(W0 + 3 * 3600000).toISOString(), approval: null });
  props.unshift({ id: gid("p"), action: "pay_invoice", params: { invoice: obj("Invoice", 6) }, proposer: "agent:finance_analyst", proposer_role: "finance_analyst", rationale: "Invoice matches the PO; no anomaly signals.",
    dry_run: [{ kind: "object", type: "Invoice", key: obj("Invoice", 6), op: "modify", before: { status: "open" }, after: { status: "paid" } }], created_at: T(85), on_behalf_of: user("finance_analyst"), run_id: "e0000000-0000-4000-8000-000000000002",
    evidence: [ev(`Invoice/${obj("Invoice", 6).slice(0, 8)}`, "Anomaly score 0.05, below threshold", "asserted", "tc_3")],
    expires_at: new Date(W0 + 2 * 3600000).toISOString(), staleKeys: ["status", "amount"], approval: null });
  props.unshift({ id: gid("p"), action: "pay_invoice", params: { invoice: obj("Invoice", 7) }, proposer: "agent:finance_analyst", proposer_role: "finance_analyst", rationale: "Paid early per terms.",
    dry_run: [], created_at: T(70), on_behalf_of: user("finance_analyst"), run_id: "e0000000-0000-4000-8000-000000000002", evidence: [],
    expires_at: new Date(W0 - 60000).toISOString(), approval: null });
}

/** Row-level visibility of an execution for a role (admin all; others own rows plus rows whose objects they can see). */
function execVisible(role: string, x: Exec): boolean {
  if (role === "admin" || x.actor === user(role)) return true;
  if (x.status === "denied" || !x.diff.length) return false;
  return x.diff.every((d) => W.canSeeObj(role, d.type, d.key));
}
const propVisible = (role: string, p: Prop) => role === "admin" || p.proposer === user(role) || p.on_behalf_of === user(role) || (approversOf(p.action).includes(role));
const rowOf = (x: Exec) => { const { diff: _d, undo: _u, ...r } = x; return r; };
const state = (p: Prop) => (p.approval ? p.approval.decision : "pending");
const propOut = (p: Prop) => {
  const { staleKeys: _s, ...r } = p;
  return { on_behalf_of: null, evidence: null, run_id: null, expires_at: null, ...r, expired: !p.approval && !!p.expires_at && Date.parse(p.expires_at) <= Date.now(), stale: null, state: state(p) };
};
/** Agent-created proposal (used by the mock agent runs). */
export function addAgentProposal(role: string, action: string, params: Json, rationale: string, evidence: Json[], runId: string, ttlMin = 60, staleKeys?: string[]): Prop {
  const r = W.apply(action, params); let dry: any[] = [];
  if (!("fail" in r) && !("notFound" in r)) { dry = r.diff; r.undo(); }
  const p: Prop = { id: gid("p"), action, params, proposer: `agent:${role}`, proposer_role: role, rationale, dry_run: dry, created_at: W.nowIso(), on_behalf_of: user(role), evidence, run_id: runId,
    expires_at: new Date(Date.now() + ttlMin * 60000).toISOString(), staleKeys, approval: null };
  props.unshift(p); return p;
}
export const proposalRef = (id: string) => { const p = props.find((x) => x.id === id); return p ? { state: state(p), execution_id: p.approval?.execution_id ?? null } : null; };

function performExecute(name: string, params: Json, role: string, proposalId: string | null = null): [number, unknown] {
  if (!roleMayExec(role, name)) {
    record({ name, actor: user(role), role, params, status: "denied", error: `role ${role} may not execute ${name}` });
    return err(403, "permission_denied", `role ${role} may not execute ${name}`);
  }
  const r = W.apply(name, params);
  if ("notFound" in r && r.notFound) return [404, { detail: "not found" }];
  if ("fail" in r) {
    record({ name, actor: user(role), role, params, status: "denied", error: r.fail.join("; ") });
    return err(422, "criteria_failed", "criteria failed", { failures: r.fail.map((message) => ({ message })) });
  }
  const x = record({ name, actor: user(role), role, params, diff: r.diff, undo: r.undo, proposal_id: proposalId });
  addHooks(x);
  return [200, { action_id: x.action_id, status: "executed", changed: r.diff.length, webhooks: hooksOf(name) }];
}

export function route(method: string, path: string, q: URLSearchParams, body: Json | undefined, role: string): [number, unknown] | null {
  const mine = /^\/(me|roles|actions|proposals|executions|audit|webhooks)(\/|$)/.test(path);
  if (!mine) return null;
  if (!ROLES.includes(role)) return err(403, "permission_denied", `unknown or non-selectable role '${role}'`);
  let m: RegExpMatchArray | null;
  if (path === "/me") return [200, { ...W.whoAmI(role), actions: Object.keys(ACTION_META).filter((a) => roleMayExec(role, a)) }];
  if (path === "/roles") return [200, ROLES];
  if (path === "/actions" && method === "GET") {
    const type = q.get("type") ?? "", id = q.get("id") ?? "";
    if (!W.visibleKey(type, id)) return [404, { detail: "not found" }];
    const out = Object.entries(ACTION_META as Record<string, any>).filter(([a, meta]) => {
      if (a === "ship_position") return type === "Shipment";
      return meta.params.some((p: any) => ACTION_TYPE[p.name] === type) || (meta.params.some((p: any) => p.name === "id") && meta.params.some((p: any) => p.name === "type"));
    }).map(([a, meta]) => {
      const may = roleMayExec(role, a);
      return { name: a, description: meta.description, params: meta.params, requires_approval: meta.requiresApproval, allowed: may, reason: may ? null : `Role ${role} is not permitted to execute ${a}` };
    });
    return [200, out];
  }
  if ((m = path.match(/^\/actions\/(\w+)\/execute$/)) && method === "POST") {
    const name = m[1];
    if (!(name in ACTION_META)) return [404, { detail: "unknown action" }];
    if (!roleMayExec(role, name)) return performExecute(name, body?.params ?? {}, role);
    if (needsApproval(name)) return propose(name, body?.params ?? {}, "", role, 202);
    return performExecute(name, body?.params ?? {}, role);
  }
  if ((m = path.match(/^\/actions\/(\w+)\/propose$/)) && method === "POST") {
    if (!(m[1] in ACTION_META)) return [404, { detail: "unknown action" }];
    return propose(m[1], body?.params ?? {}, body?.rationale ?? "", role, 201);
  }
  if (path === "/proposals") {
    const all = q.get("state") === "all";
    return [200, props.filter((p) => propVisible(role, p) && (all || !p.approval)).map(propOut)];
  }
  if ((m = path.match(/^\/proposals\/([\w-]+)\/(approve|reject)$/)) && method === "POST") {
    const p = props.find((x) => x.id === m![1]);
    if (!p || !propVisible(role, p)) return [404, { detail: "not found" }];
    if (p.approval) return err(422, "invalid_state", "proposal already decided");
    if (m[2] === "approve" && p.expires_at && Date.parse(p.expires_at) <= Date.now()) return err(409, "proposal_expired", "proposal expired");
    if (m[2] === "approve" && p.staleKeys?.length) return err(409, "stale_proposal", "the data changed since the proposal was made", { changed: p.staleKeys });
    if (!approversOf(p.action).includes(role)) return err(403, "permission_denied", `role ${role} may not approve ${p.action}`);
    if (p.proposer === user(role)) return err(403, "permission_denied", "a proposer cannot approve their own proposal");
    const approve = m[2] === "approve";
    let execId: string | null = null;
    if (approve) {
      const r = performExecute(p.action, p.params, role, p.id);
      if (r[0] !== 200) return r;
      execId = (r[1] as Json).action_id;
    }
    p.approval = { id: gid("c"), proposal_id: p.id, decision: approve ? "approved" : "rejected", approver: user(role), approver_role: role, comment: body?.comment ?? "", decided_at: W.nowIso(), execution_id: execId };
    return [200, { proposal_id: p.id, status: p.approval.decision, action_id: execId }];
  }
  if (path === "/executions" && method === "GET") {
    const lim = Number(q.get("limit") ?? 100), name = q.get("name"), actor = q.get("actor"), status = q.get("status");
    return [200, execs.filter((x) => execVisible(role, x) && (!name || x.name.includes(name)) && (!actor || x.actor.includes(actor)) && (!status || x.status === status || (status === "rolled_back" && x.rolled_back))).slice(0, lim).map(rowOf)];
  }
  if ((m = path.match(/^\/executions\/([\w-]+)$/))) {
    const x = execs.find((e) => e.action_id === m![1]);
    if (!x || !execVisible(role, x)) return [404, { detail: "not found" }];
    return [200, { execution: rowOf(x), diff: x.diff, webhooks: outbox.filter((o) => o.action_id === x.action_id).map(({ deliveries, ...ob }) => ({ outbox: ob, deliveries })), rollback: rollbacks.find((r) => r.original_action_id === x.action_id) ?? null }];
  }
  if ((m = path.match(/^\/executions\/([\w-]+)\/rollback$/)) && method === "POST") {
    const x = execs.find((e) => e.action_id === m![1]);
    if (!x || !execVisible(role, x)) return [404, { detail: "not found" }];
    if (!roleMayExec(role, x.name)) return err(403, "permission_denied", `role ${role} may not roll back ${x.name}`);
    if (x.rolled_back || x.status !== "executed") return err(422, "invalid_state", "execution is not rollbackable");
    const conflicts = x.diff.filter((d) => d.kind === "object").map((d) => ({ d, now: W.current(d.type, d.key) }))
      .filter(({ d, now }) => now && Object.keys(d.after ?? {}).some((k) => JSON.stringify(now[k]) !== JSON.stringify(d.after[k])))
      .map(({ d, now }) => `${d.type}:${d.key} expected ${JSON.stringify(d.after)} but found ${JSON.stringify(now)}`);
    if (conflicts.length) return err(409, "rollback_conflict", "objects changed since this action", { keys: conflicts });
    x.undo?.();
    const rb = record({ name: "rollback", actor: user(role), role, params: { original: x.action_id, reason: body?.reason ?? "" }, parent_execution_id: x.action_id, diff: x.diff.map((d) => ({ ...d, before: d.after, after: d.before })) });
    x.rolled_back = true;
    rollbacks.push({ id: gid("r"), original_action_id: x.action_id, rollback_action_id: rb.action_id, actor: user(role), reason: body?.reason ?? "", at: rb.recorded_at });
    return [200, { action_id: rb.action_id, status: "executed", changed: rb.diff.length, webhooks: [] }];
  }
  if ((m = path.match(/^\/webhooks\/([\w-]+)\/retry$/)) && method === "POST") {
    const o = outbox.find((x) => x.id === m![1]);
    const ex = o && execs.find((e) => e.action_id === o.action_id);
    if (!o || !ex || !execVisible(role, ex)) return [404, { detail: "not found" }];
    const last = o.deliveries[o.deliveries.length - 1];
    if (last.status === "delivered") return err(422, "invalid_state", "already delivered");
    o.deliveries.push({ attempt: last.attempt + 1, status: "pending", http_status: null, error: null, next_attempt_at: null, at: W.nowIso() });
    o.deliveries.push({ attempt: last.attempt + 1, status: "delivered", http_status: 200, error: null, next_attempt_at: null, at: W.nowIso() });
    return [200, { outbox_id: o.id, status: "pending" }];
  }
  if (path === "/audit") {
    const kind = q.get("kind") ?? "execution", lim = Number(q.get("limit") ?? 100);
    if (kind === "approval") return [200, props.filter((p) => p.approval && propVisible(role, p)).map((p) => ({ kind: "approval", id: p.approval!.id, at: p.approval!.decided_at, actor: p.approval!.approver, role: p.approval!.approver_role, name: p.action, status: p.approval!.decision, action_id: p.approval!.execution_id, proposal_id: p.id, summary: p.approval!.comment }))];
    if (kind === "webhook") return [200, outbox.filter((o) => { const ex = execs.find((e) => e.action_id === o.action_id); return ex && execVisible(role, ex); }).slice(0, lim).map((o) => {
      const l = o.deliveries[o.deliveries.length - 1], ex = execs.find((e) => e.action_id === o.action_id)!;
      return { kind: "webhook", id: o.id, at: l.at, actor: ex.actor, name: o.hook, status: l.status, action_id: o.action_id, summary: `${o.deliveries.length} attempt(s)${l.error ? `: ${l.error}` : ""}` };
    })];
    if (kind === "proposal") return [200, props.filter((p) => propVisible(role, p)).map((p) => ({ kind: "proposal", id: p.id, at: p.created_at, actor: p.proposer, role: p.proposer_role, name: p.action, status: state(p), proposal_id: p.id, action_id: p.approval?.execution_id ?? null, summary: p.rationale }))];
    return [200, execs.filter((x) => execVisible(role, x) && (kind === "rollback" ? !!x.parent_execution_id : !x.parent_execution_id)).slice(0, lim).map((x) => ({ kind: x.parent_execution_id ? "rollback" : "execution", id: x.action_id, action_id: x.action_id, at: x.recorded_at, actor: x.actor, role: x.role, name: x.name, status: x.status, summary: x.error ?? "" }))];
  }
  return [404, { detail: `mock: no route ${method} ${path}` }];
}

function propose(name: string, params: Json, rationale: string, role: string, okStatus: number): [number, unknown] {
  if (!roleMayExec(role, name)) { record({ name, actor: user(role), role, params, status: "denied", error: `role ${role} may not execute ${name}` }); return err(403, "permission_denied", `role ${role} may not propose ${name}`); }
  const r = W.apply(name, params);
  if ("notFound" in r && r.notFound) return [404, { detail: "not found" }];
  let dry: any[] = [];
  if ("fail" in r) { if (r.fail.length) return err(422, "criteria_failed", "criteria failed", { failures: r.fail.map((message) => ({ message })) }); }
  else { dry = r.diff; r.undo(); }
  const p: Prop = { id: gid("p"), action: name, params, proposer: user(role), proposer_role: role, rationale, dry_run: dry, created_at: W.nowIso(), approval: null };
  props.unshift(p);
  return okStatus === 201 ? [201, { proposal_id: p.id }] : [202, { proposal_id: p.id, status: "pending_approval" }];
}
void targetOf;
