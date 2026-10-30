import { useState } from "react";
import { ApiError, ingestApi } from "../api";
import { fmtT, usePolled } from "../store";
import { ErrorBanner, Spinner } from "../ui";
import { JobLog, errMsg, useJob } from "./common";

const money = (v: number | null | undefined) => (v === null || v === undefined ? "n/a" : v === 0 ? "free" : `$${v.toFixed(2)}`);

export default function ProposalStep({ sid, onProposal }: { sid: string; onProposal: (pid: string) => void }) {
  const [mode, setMode] = useState<"baseline" | "model">("baseline");
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string>();
  const [unavailable, setUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const sess = usePolled(() => ingestApi.session(sid), [sid, jobId]);
  const { job, error } = useJob(jobId, (j) => { if (j.status === "done" && j.proposal_id) onProposal(j.proposal_id); else setBusy(false); });
  const go = async () => {
    setBusy(true); setErr(undefined); setUnavailable(false);
    try { setJobId((await ingestApi.createProposal(sid, mode)).job_id); }
    catch (e) {
      setBusy(false);
      if (e instanceof ApiError && e.status === 503) setUnavailable(true); else setErr(errMsg(e));
    }
  };
  const prev = sess.data?.proposals ?? [];
  return (
    <div>
      <section className="card">
        <h3>How should the ontology be proposed?</h3>
        <fieldset className="choices" disabled={busy}>
          <legend className="sr">Proposal mode</legend>
          <label className={`choice${mode === "baseline" ? " on" : ""}`}><input type="radio" name="mode" checked={mode === "baseline"} onChange={() => setMode("baseline")} />
            <span><b>Baseline</b><small>Deterministic heuristics: one type per table, foreign keys become links, small value sets become enums. No model, no cost, always available.</small></span></label>
          <label className={`choice${mode === "model" ? " on" : ""}`}><input type="radio" name="mode" checked={mode === "model"} onChange={() => setMode("model")} />
            <span><b>Claude</b><small>A model reads the profile and samples, drafts names, roles and actions, and checks its draft in a scratch schema. Capped at 8 turns and USD 1.00 per proposal; the cost is shown when it finishes.</small></span></label>
        </fieldset>
        {unavailable && <div className="warnbox" role="alert"><b>The model is unavailable.</b> The server has no model API key configured (or the daily budget is used up), so Claude cannot draft proposals. Use the baseline now, or ask an operator to set the key. <button onClick={() => { setMode("baseline"); setUnavailable(false); }}>Switch to baseline</button></div>}
        <ErrorBanner error={err} onRetry={go} />
        <p><button className="primary" onClick={go} disabled={busy}>{busy ? "Working…" : mode === "model" ? "Ask Claude for a proposal" : "Generate baseline proposal"}</button></p>
      </section>
      {jobId && <JobLog job={job} error={error} title={mode === "model" ? "Claude is drafting" : "Building the proposal"} />}
      {job?.status === "done" && <p role="status" className="small">Done. Cost {money(job.cost_usd)}. Opening the review…</p>}
      <section className="card"><h3>Proposals for this source</h3>
        {sess.loading && !sess.data && <Spinner label="Loading" />}
        <ErrorBanner error={sess.error} onRetry={sess.reload} />
        {sess.data && !prev.length && <p className="muted">None yet.</p>}
        {prev.length > 0 && <table><thead><tr><th>#</th><th>Kind</th><th>Model</th><th>Cost</th><th>Valid</th><th>Created</th><th></th></tr></thead><tbody>
          {prev.map((p) => <tr key={p.id}><td>{p.seq}</td><td>{p.kind}</td><td>{p.model ?? "heuristic"}</td><td>{money(p.cost_usd)}</td>
            <td>{p.ok === undefined || p.ok === null ? "" : <span className={`chip ${p.ok ? "ok" : "bad"}`}>{p.ok ? "valid" : "failed"}</span>}</td><td>{fmtT(p.created_at)}</td><td><button onClick={() => onProposal(p.id)}>Open</button></td></tr>)}</tbody></table>}
      </section>
    </div>
  );
}
