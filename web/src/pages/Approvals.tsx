import { useState } from "react";
import { api, ApiError, dryDiff, type Proposal } from "../api";
import { fmtT, toast, usePolled, useApp } from "../store";
import { DiffView } from "./Audit";

export default function ApprovalsPage() {
  const { role, me } = useApp();
  const q = usePolled(() => api.proposals("pending"), [role], 5000);
  return (
    <div className="page">
      <h2>Approval inbox</h2>
      <p className="muted">Signed in as {me?.user ?? role}. A proposal cannot be approved by the user who proposed it.</p>
      {q.error && <div className="err">{q.error}</div>}
      {q.data?.map((p) => <Card key={p.id} p={p} onDone={q.reload} />)}
      {q.data && !q.data.length && <p className="muted">No pending proposals.</p>}
    </div>
  );
}

function Card({ p, onDone }: { p: Proposal; onDone: () => void }) {
  const [comment, setComment] = useState("");
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const decide = async (ok: boolean) => {
    setBusy(true); setErr(undefined);
    try {
      const r = await api.decide(p.id, ok, comment);
      const aid = r.action_id ?? r.execution_id;
      toast(ok ? `${p.action} approved and executed` : `${p.action} rejected`, ok && aid ? `#/audit/${aid}` : undefined); onDone();
    } catch (e: any) {
      setErr(e instanceof ApiError ? (e.status === 403 ? `Not allowed: ${e.text}` : e.status === 404 ? "Not found." : e.status === 422 && e.code === "criteria_failed" ? `Criteria failed: ${(e.payload.failures ?? []).map((f: any) => f.message ?? f).join("; ")}` : e.text) : String(e.message ?? e));
    } finally { setBusy(false); }
  };
  return (
    <div className="card prop">
      <h3>{p.action} <span className="muted">proposed by {p.proposer} ({p.proposer_role}) at {fmtT(p.created_at)}</span></h3>
      {p.rationale && <p><b>Rationale:</b> {p.rationale}</p>}
      <pre>{JSON.stringify(p.params, null, 2)}</pre>
      <h4>Dry run</h4><DiffView diff={dryDiff(p.dry_run)} />
      {err && <div className="err" role="alert">{err}</div>}
      <p className="form"><label><span>Comment</span><input value={comment} onChange={(e) => setComment(e.target.value)} /></label>
        <button className="primary" disabled={busy} onClick={() => decide(true)}>Approve</button> <button className="danger" disabled={busy} onClick={() => decide(false)}>Reject</button></p>
    </div>
  );
}
