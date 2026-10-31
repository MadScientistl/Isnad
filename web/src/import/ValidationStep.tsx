import { useState } from "react";
import { ApiError, ingestApi, type ProposalDetail, type ProposalRow } from "../api";
import { fmtT } from "../store";
import { ErrorBanner, Trunc } from "../ui";
import { JobLog, errMsg, useJob } from "./common";

const fmtVal = (v: unknown) => (typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(3)) : v === null || v === undefined ? "" : String(v));
const isCount = (n: string) => n === "key_violations";

export default function ValidationStep({ det, rounds, onOpen, onRepaired, onNext }: {
  det: ProposalDetail; rounds: ProposalRow[]; onOpen: (pid: string) => void; onRepaired: (pid: string) => void; onNext: () => void;
}) {
  const v = det.validation;
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const { job, error } = useJob(jobId, (j) => { setBusy(false); if (j.status === "done" && j.proposal_id) onRepaired(j.proposal_id); });
  const repair = async () => {
    setBusy(true); setErr(undefined);
    try { setJobId((await ingestApi.repair(det.id)).job_id); }
    catch (e) { setBusy(false); setErr(e instanceof ApiError && (e.status === 503 || e.code === "agent_unavailable") ? "Automatic repair needs a model API key and this server has none. Go back to the Review step and edit the proposal by hand, or open an earlier round below." : e instanceof ApiError && e.status === 409 ? `No repair rounds left: ${e.text}. Edit the proposal by hand instead.` : errMsg(e)); }
  };
  const failed = v?.checks.filter((c) => !c.ok) ?? [];
  const repairs = rounds.filter((r) => r.kind === "repair").length;
  return (
    <div>
      {!v && <div className="warnbox">This proposal has no validation report yet.</div>}
      {v && <section className="card">
        <h3>Checks <span className={`chip ${v.ok ? "ok" : "bad"}`}>{v.ok ? "all passed" : `${failed.length} failed`}</span> <span className="muted small">ran in {v.seconds.toFixed(1)} s in a scratch schema</span></h3>
        <div className="tablewrap"><table className="checks"><thead><tr><th>Check</th><th>Result</th><th>Value</th><th>Threshold</th><th>Detail</th></tr></thead><tbody>
          {v.checks.map((c) => <tr key={c.name} className={c.ok ? "" : "failrow"}>
            <td><code className="ref">{c.name}</code></td><td><span className={`chip ${c.ok ? "ok" : "bad"}`}>{c.ok ? "pass" : "fail"}</span></td>
            <td>{isCount(c.name) ? fmtVal(c.value) : fmtVal(c.value)}</td><td className="muted">{c.threshold === null ? "" : `${c.name.includes("resolution") ? ">= " : ""}${fmtVal(c.threshold)}`}</td>
            <td className="small"><Trunc max={140}>{c.detail}</Trunc>{c.examples.length > 0 && <ul className="plain examples-list">{c.examples.slice(0, 3).map((x, i) => <li key={i}><code className="ref"><Trunc max={120}>{x}</Trunc></code></li>)}</ul>}</td></tr>)}
        </tbody></table></div>
        <p className="small muted">Rows loaded: {Object.entries(v.stats.rows_loaded).map(([k, n]) => `${k} ${n.toLocaleString()}`).join(", ") || "none"}. Links loaded: {Object.entries(v.stats.links_loaded).map(([k, n]) => `${k} ${n.toLocaleString()}`).join(", ") || "none"}.</p>
        {!v.ok && <div className="repairbox"><p><b>{failed.length} check{failed.length === 1 ? "" : "s"} failed.</b> Repair asks the generator to fix the failures ({repairs} of 3 rounds used), or go back and edit the proposal.</p>
          <button className="primary" onClick={repair} disabled={busy || repairs >= 5}>{busy ? "Repairing…" : "Repair automatically"}</button> <ErrorBanner error={err} onRetry={repair} /></div>}
      </section>}
      {jobId && <JobLog job={job} error={error} title="Repair round" />}
      <section className="card"><h3>Rounds ({rounds.length})</h3>
        {!rounds.length ? <p className="muted">No history.</p> : <table><thead><tr><th>#</th><th>Kind</th><th>Valid</th><th>Created</th><th></th></tr></thead><tbody>
          {rounds.map((r) => <tr key={r.id} className={r.id === det.id ? "cur-row" : ""}><td>{r.seq}</td><td>{r.kind}{r.parent_id ? <span className="muted small"> from #{rounds.find((x) => x.id === r.parent_id)?.seq ?? "?"}</span> : ""}</td>
            <td>{r.ok === undefined || r.ok === null ? "" : <span className={`chip ${r.ok ? "ok" : "bad"}`}>{r.ok ? "valid" : "failed"}</span>}</td><td>{fmtT(r.created_at)}</td>
            <td>{r.id === det.id ? <span className="muted small">current</span> : <button onClick={() => onOpen(r.id)}>Open</button>}</td></tr>)}</tbody></table>}
      </section>
      <p className="stepfoot"><button className="primary" disabled={!v?.ok} onClick={onNext} title={v?.ok ? undefined : "Validation must pass before approval"}>Next: approve and import</button></p>
    </div>
  );
}
