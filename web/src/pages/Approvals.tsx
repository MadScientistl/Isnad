import { useEffect, useState } from "react";
import { api, ApiError, dryDiff, type Proposal } from "../api";
import { fmtT, toast, usePolled, useApp } from "../store";
import { DiffView } from "./Audit";
import { fmtCountdown, secsUntil } from "../agent";

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
  const [stale, setStale] = useState<string[]>();
  const [expiredErr, setExpiredErr] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (!p.expires_at) return; const h = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(h); }, [p.expires_at]);
  const left = secsUntil(p.expires_at, now);
  const expired = p.expired || expiredErr || (left !== null && left <= 0);
  const agent = p.proposer_role?.startsWith("agent") || !!p.on_behalf_of || !!p.run_id;
  const decide = async (ok: boolean) => {
    setBusy(true); setErr(undefined);
    try {
      const r = await api.decide(p.id, ok, comment);
      const aid = r.action_id ?? r.execution_id;
      toast(ok ? `${p.action} approved and executed` : `${p.action} rejected`, ok && aid ? `#/audit/${aid}` : undefined); onDone();
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409 && e.code === "stale_proposal") {
        setStale((e.payload.changed ?? []) as string[]); setErr(undefined);
      } else if (e instanceof ApiError && e.status === 409 && e.code === "proposal_expired") {
        setExpiredErr(true); setErr(undefined);
      } else setErr(e instanceof ApiError ? (e.status === 403 ? `Not allowed: ${e.text}` : e.status === 404 ? "Not found." : e.status === 422 && e.code === "criteria_failed" ? `Criteria failed: ${(e.payload.failures ?? []).map((f: any) => f.message ?? f).join("; ")}` : e.text) : String(e.message ?? e));
    } finally { setBusy(false); }
  };
  return (
    <div className="card prop">
      <h3>{p.action} <span className="muted">{p.on_behalf_of ? <>proposed by <b>{p.proposer.startsWith("agent:") ? p.proposer : `agent:${p.proposer_role.replace(/^agent:/, "")}`}</b> on behalf of <b>{p.on_behalf_of}</b></> : <>proposed by {p.proposer} ({p.proposer_role})</>} at {fmtT(p.created_at)}</span></h3>
      <p className="badges">
        {agent && <span className="chip info">agent proposal</span>}
        {expired ? <span className="chip bad">expired</span> : left !== null && <span className={`chip ${left < 300 ? "warn" : ""}`}>expires in {fmtCountdown(left)}</span>}
        {p.run_id && <a className="lnk small" href={`#/agent/${p.run_id}`}>originating agent run</a>}
      </p>
      {p.rationale && <p><b>Rationale:</b> {p.rationale}</p>}
      {p.evidence && p.evidence.length > 0 && <div className="evidence"><h4>Evidence</h4><ul className="plain">{p.evidence.map((ev, i) => (
        <li key={i}><span className={`chip ev-${ev.kind}`}>{ev.kind}</span> <code className="ref">{ev.ref}</code> <span>{ev.claim}</span></li>))}</ul></div>}
      <pre>{JSON.stringify(p.params, null, 2)}</pre>
      <h4>Dry run</h4><DiffView diff={dryDiff(p.dry_run)} />
      {stale && <div className="warnbox" role="alert"><b>This proposal is stale.</b> The data it was based on changed ({stale.length ? stale.join(", ") : "unknown fields"}) after it was proposed, so approving it could act on outdated facts. Reject it, or ask the agent to look again and propose a fresh one.</div>}
      {expired && <div className="warnbox" role="alert">This proposal expired and can no longer be approved. Reject it, or ask the agent to propose again.</div>}
      {err && <div className="err" role="alert">{err}</div>}
      <p className="form"><label><span>Comment</span><input value={comment} onChange={(e) => setComment(e.target.value)} /></label>
        <button className="primary" disabled={busy || expired} title={expired ? "Expired" : undefined} onClick={() => decide(true)}>Approve</button> <button className="danger" disabled={busy} onClick={() => decide(false)}>Reject</button></p>
    </div>
  );
}
