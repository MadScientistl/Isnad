import { useEffect, useMemo, useState } from "react";
import { EmptyState, ErrorBanner, Spinner } from "../ui";
import { api, ApiError, dryDiff, isNotFound, type AuditRow, type DiffRow, type ExecDetail, type Execution, type Proposal } from "../api";
import { fmtT, toast, usePolled, useApp } from "../store";
import { ScnChip } from "../ScnUi";

const STATUS_CLS: Record<string, string> = {
  executed: "ok", approved: "ok", delivered: "ok", pending: "warn", pending_approval: "warn", retry: "warn",
  denied: "bad", rejected: "bad", dead: "bad", rolled_back: "info", failed: "bad",
};
export const Chip = ({ s }: { s: string }) => <span className={`chip ${STATUS_CLS[s] ?? ""}`}>{s.replace(/_/g, " ")}</span>;
const j = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const short = (id?: string | null) => (id ? id.slice(0, 8) : "");
const open = (id: string) => { location.hash = `/audit/${id}`; };

export function DiffView({ diff }: { diff: DiffRow[] }) {
  if (!diff.length) return <span className="muted">No changes.</span>;
  return (
    <table className="diff"><thead><tr><th>Kind</th><th>Object</th><th>Op</th><th>Changes</th></tr></thead><tbody>
      {diff.map((d, i) => {
        const keys = [...new Set([...Object.keys(d.before ?? {}), ...Object.keys(d.after ?? {})])].filter((k) => j(d.before?.[k]) !== j(d.after?.[k]));
        return <tr key={i}><td>{d.kind}</td><td>{d.type} <span className="muted">{short(d.key)}</span></td><td><span className={`chip op-${d.op}`}>{d.op}</span></td>
          <td>{keys.map((k) => <div key={k}><b>{k}</b>: <del>{j(d.before?.[k]) || "∅"}</del> → <ins>{j(d.after?.[k]) || "∅"}</ins></div>)}</td></tr>;
      })}</tbody></table>
  );
}

type Tab = "executions" | "proposals" | "approvals" | "webhooks";
const TABS: Tab[] = ["executions", "proposals", "approvals", "webhooks"];

export default function AuditPage({ parts }: { parts: string[] }) {
  const detailId = parts[1];
  const [tab, setTab] = useState<Tab>("executions");
  const [f, setF] = useState({ name: "", actor: "", status: "", kind: "" });
  const data = usePolled(async (): Promise<{ rows: AuditRow[] }> => {
    if (tab === "executions") {
      const ex = await api.executions({ name: f.name, actor: f.actor, status: f.status });
      return { rows: ex.map(execRow) };
    }
    if (tab === "proposals") return { rows: (await api.proposals("all")).map(propRow) };
    return { rows: await api.audit(tab === "approvals" ? "approval" : "webhook") };
  }, [tab, f.name, f.actor, f.status], 5000);
  const rows = useMemo(() => (data.data?.rows ?? []).filter((r) =>
    (!f.name || r.name.includes(f.name)) && (!f.actor || r.actor.includes(f.actor)) && (!f.status || r.status === f.status) &&
    (!f.kind || (f.kind === "rollback") === (r.kind === "rollback"))), [data.data, f]);
  const statuses = tab === "webhooks" ? ["pending", "delivered", "retry", "dead"] : tab === "executions" ? ["executed", "denied", "rolled_back"] : ["pending", "approved", "rejected"];
  return (
    <div className="page">
      <h2>Audit log</h2>
      <div className="tabs" role="tablist">{TABS.map((t) => <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => { setTab(t); setF({ ...f, status: "" }); }}>{t}</button>)}</div>
      <div className="filters">
        <input aria-label="Action name" placeholder="action name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <input aria-label="Actor" placeholder="actor" value={f.actor} onChange={(e) => setF({ ...f, actor: e.target.value })} />
        <select aria-label="Status" value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}><option value="">any status</option>{statuses.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}</select>
        {tab === "executions" && <select aria-label="Kind" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}><option value="">all kinds</option><option value="execution">executions</option><option value="rollback">rollbacks</option></select>}
        <button onClick={data.reload}>Refresh</button>
      </div>
      <ErrorBanner error={data.error} onRetry={data.reload} />
      {data.loading && !data.data && !data.error && <Spinner label="Loading audit trail" />}
      <table><thead><tr><th>Time</th><th>Kind</th><th>Name</th><th>Actor</th><th>Status</th><th>Detail</th></tr></thead><tbody>
        {rows.map((r) => (
          <tr key={r.kind + r.id} className={r.action_id ? "row" : ""} onClick={() => r.action_id && open(r.action_id)}>
            <td>{fmtT(r.at)}</td><td>{r.kind}</td><td>{r.name} <ScnChip id={r.scenario_id as number | undefined} /></td><td>{r.actor}{r.role ? <span className="muted"> ({r.role})</span> : ""}</td><td><Chip s={r.status} /></td><td className="muted">{r.summary}</td></tr>))}
        {!data.loading && !data.error && !rows.length && <tr><td colSpan={6}><EmptyState title="Nothing to show">No audit rows match these filters.</EmptyState></td></tr>}
      </tbody></table>
      {detailId && <Drawer id={detailId} onClose={() => { location.hash = "/audit"; }} onChanged={data.reload} />}
    </div>
  );
}

const execRow = (e: Execution): AuditRow => ({
  kind: e.parent_execution_id ? "rollback" : "execution", scenario_id: e.scenario_id, id: e.action_id, action_id: e.action_id, at: e.recorded_at, actor: e.actor, role: e.role, name: e.name,
  status: e.rolled_back ? "rolled_back" : e.status, summary: Object.entries(e.params ?? {}).map(([k, v]) => `${k}=${j(v).slice(0, 12)}`).join(" ") + (e.error ? ` ${e.error}` : ""),
});
const propRow = (p: Proposal): AuditRow => ({
  kind: "proposal", scenario_id: p.scenario_id, id: p.id, at: p.created_at, actor: p.proposer, role: p.proposer_role, name: p.action, status: p.state ?? (p.approval ? p.approval.decision : "pending"),
  action_id: p.approval?.execution_id ?? null, proposal_id: p.id, summary: p.rationale ?? "",
});

function Drawer({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const d = usePolled<ExecDetail>(() => api.execution(id), [id]);
  const { role, scenario } = useApp();
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [err, setErr] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { const f = (e: KeyboardEvent) => e.key === "Escape" && onClose(); addEventListener("keydown", f); return () => removeEventListener("keydown", f); }, [onClose]);
  const ex = d.data?.execution;
  const rollback = async () => {
    if (!reason.trim()) { setErr(["A reason is required."]); return; }
    setBusy(true); setErr([]);
    try {
      const r = await api.rollback(id, reason);
      toast("Rolled back", r.action_id ? `#/audit/${r.action_id}` : undefined); setAsking(false); setReason(""); d.reload(); onChanged();
    } catch (e: any) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 422)) {
        const p = e.payload ?? {}; const keys: unknown[] = p.keys ?? p.conflicts ?? p.failures ?? [];
        setErr([`Rollback refused: ${typeof p.detail === "string" ? p.detail : "objects changed since this action"}`, ...keys.map((k) => (typeof k === "string" ? k : j((k as any).key ?? (k as any).message ?? k)))]);
      } else setErr([e instanceof ApiError ? e.text : String(e.message ?? e)]);
    } finally { setBusy(false); }
  };
  const retry = async (outboxId: string) => {
    try { await api.retryWebhook(outboxId); toast("Webhook retry queued"); d.reload(); onChanged(); } catch (e: any) { setErr([e instanceof ApiError ? e.text : String(e.message ?? e)]); }
  };
  const canRollback = ex && ex.status === "executed" && !d.data?.rollback && !ex.parent_execution_id && ex.name !== "seed";
  return (
    <aside className="drawer" aria-label="Execution detail">
      <div className="drawer-h"><h3>Execution {short(id)}</h3><button onClick={onClose} aria-label="Close">✕</button></div>
      {d.error && (isNotFound(d.error) ? <p className="muted">Not found.</p> : <div className="err">{d.error}</div>)}
      {ex && <>
        <table><tbody>
          <tr><th>Action</th><td>{ex.name}</td></tr><tr><th>Status</th><td><Chip s={d.data!.rollback ? "rolled_back" : ex.status} /></td></tr>
          <tr><th>Actor</th><td>{ex.actor} {ex.role && <span className="muted">({ex.role})</span>}</td></tr>
          <tr><th>Source</th><td>{ex.source} <ScnChip id={ex.scenario_id} /></td></tr><tr><th>Recorded</th><td>{fmtT(ex.recorded_at)}</td></tr>
          {ex.proposal_id && <tr><th>Proposal</th><td>{short(ex.proposal_id)}</td></tr>}
          {ex.parent_execution_id && <tr><th>Rollback of</th><td><a className="lnk" href={`#/audit/${ex.parent_execution_id}`}>{short(ex.parent_execution_id)}</a></td></tr>}
          {ex.error && <tr><th>Error</th><td className="bad-t">{ex.error}</td></tr>}</tbody></table>
        <h4>Params</h4><pre>{JSON.stringify(ex.params, null, 2)}</pre>
        <h4>Changes (before → after)</h4><DiffView diff={d.data!.diff} />
        <h4>Webhooks</h4>
        {!d.data!.webhooks.length && <span className="muted">None.</span>}
        {d.data!.webhooks.map((w) => {
          const last = w.deliveries[w.deliveries.length - 1];
          return <div key={w.outbox.id} className="wh"><b>{w.outbox.hook}</b> <span className="muted">{w.outbox.url}</span> {last && <Chip s={last.status} />}
            <table><thead><tr><th>#</th><th>Status</th><th>HTTP</th><th>Error</th><th>At</th></tr></thead><tbody>
              {w.deliveries.map((x, i) => <tr key={i}><td>{x.attempt}</td><td><Chip s={x.status} /></td><td>{x.http_status ?? ""}</td><td>{x.error ?? ""}</td><td>{fmtT(x.at)}</td></tr>)}</tbody></table>
            {last && (last.status === "dead" || last.status === "retry") && <button disabled={scenario !== null} title={scenario !== null ? "Not available inside a scenario" : undefined} onClick={() => retry(w.outbox.id)}>Retry webhook</button>}</div>;
        })}
        {d.data!.rollback && <div className="warnbox">Rolled back by {d.data!.rollback.actor} at {fmtT(d.data!.rollback.at)}: "{d.data!.rollback.reason}" (<a className="lnk" href={`#/audit/${d.data!.rollback.rollback_action_id}`}>{short(d.data!.rollback.rollback_action_id)}</a>)</div>}
        {err.length > 0 && <div className="err" role="alert">{err[0]}{err.length > 1 && <ul>{err.slice(1).map((e, i) => <li key={i}>{e}</li>)}</ul>}</div>}
        {canRollback && !asking && <p><button className="danger" onClick={() => setAsking(true)}>Rollback…</button></p>}
        {asking && <p className="form"><label><span>Reason*</span><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="why roll back?" /></label>
          <button className="danger" disabled={busy} onClick={rollback}>Confirm rollback</button> <button onClick={() => { setAsking(false); setErr([]); }}>Cancel</button></p>}
        <p className="muted small">Viewing as {role}. You only see executions your role may see.</p>
      </>}
    </aside>
  );
}

export { dryDiff };
