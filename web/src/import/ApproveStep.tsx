import { useState } from "react";
import { ApiError, ingestApi, type ImportResult, type ProposalDetail } from "../api";
import { toast, useApp } from "../store";
import { ErrorBanner, Trunc } from "../ui";
import { JobLog, errMsg, useJob } from "./common";

const sum = (o?: Record<string, number> | number) => (typeof o === "number" ? o : Object.values(o ?? {}).reduce((a, b) => a + b, 0));

export default function ApproveStep({ det, comment, setComment, decisionId, setDecisionId }: {
  det: ProposalDetail; comment: string; setComment: (c: string) => void; decisionId?: string; setDecisionId: (d: string) => void;
}) {
  const { switchWorkspace, reloadWorkspaces, workspaces } = useApp();
  const doc = det.proposal, sensitive = det.diff.sensitive ?? [];
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const { job, error } = useJob(jobId, (j) => { setBusy(false); if (j.status === "done") { setResult(j.result ?? {}); reloadWorkspaces(); toast("Import finished"); } });
  const needComment = sensitive.length > 0 && !comment.trim();
  const go = async () => {
    setBusy(true); setErr(undefined);
    try {
      let d = decisionId;
      if (!d) { d = (await ingestApi.approve(det.id, comment.trim())).id; setDecisionId(d); }
      setJobId((await ingestApi.runImport(d)).job_id);
    } catch (e) { setBusy(false); setErr(e instanceof ApiError && e.code === "comment_required" ? "A comment is required because this proposal grants permissions." : errMsg(e)); }
  };
  const slug = result?.slug ?? result?.workspace;
  const ws = slug ? workspaces?.find((w) => w.slug === slug) : undefined;
  const dropped = result?.dropped_links;
  const droppedN = sum(dropped as Record<string, number> | number | undefined);
  return (
    <div>
      <section className="card"><h3>Summary</h3>
        <ul className="plain">
          <li><b>{doc.objectTypes.length}</b> object types, <b>{doc.objectTypes.reduce((a, t) => a + t.properties.length, 0)}</b> properties, <b>{doc.linkTypes.length}</b> links, <b>{doc.actions.length}</b> actions, <b>{doc.roles.length}</b> roles.</li>
          <li>Validation: <span className={`chip ${det.validation?.ok ? "ok" : "bad"}`}>{det.validation?.ok ? "passed" : "not passed"}</span>{det.validation && <span className="muted small"> {det.validation.checks.length} checks</span>}</li>
          <li>Rows to load: {Object.entries(det.validation?.stats.rows_loaded ?? {}).map(([k, n]) => `${k} ${n.toLocaleString()}`).join(", ") || "n/a"}</li>
        </ul>
        {sensitive.length > 0 && <div className="warnbox"><b>Permissions granted:</b><ul className="plain small">{sensitive.map((s, i) => <li key={i}><Trunc max={160}>{s}</Trunc></li>)}</ul></div>}
      </section>
      {!jobId && <section className="card">
        <h3>Approve</h3>
        <p className="small muted">Approving records you as the approver in the audit trail, then creates a new workspace with this ontology and loads the data. The import re-reads the stored source and re-checks its checksum. When login is on, the approver must not be the person who uploaded the source.</p>
        <label className="ed wide">Comment{sensitive.length > 0 ? " (required)" : " (optional)"}<textarea rows={2} value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} aria-invalid={needComment} /></label>
        <label className="chk"><input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> I reviewed the types, links, roles and validation results.</label>
        <ErrorBanner error={err} onRetry={go} />
        <p><button className="cta" disabled={!confirm || busy || needComment || !det.validation?.ok} onClick={go}>{busy ? "Starting…" : "Approve and import"}</button>
          {needComment && <span className="small muted"> Add a comment first.</span>}</p>
      </section>}
      {jobId && !result && <JobLog job={job} error={error} title="Importing" />}
      {result && <section className="card" role="status" aria-label="Import result"><h3>Imported {result.already_imported ? <span className="chip info">already imported</span> : <span className="chip ok">done</span>}</h3>
        <p>Workspace <b>{slug}</b>{result.schema ? <> (schema <code className="ref">{result.schema}</code>)</> : null}{result.seconds ? <span className="muted small"> in {result.seconds.toFixed(1)} s</span> : null}</p>
        <div className="tablewrap"><table><thead><tr><th>Object type</th><th>Rows</th></tr></thead><tbody>
          {Object.entries(result.counts ?? {}).map(([k, n]) => <tr key={k}><td><Trunc max={40}>{k}</Trunc></td><td>{n.toLocaleString()}</td></tr>)}
          {!Object.keys(result.counts ?? {}).length && <tr><td colSpan={2} className="muted">No rows reported.</td></tr>}</tbody></table></div>
        <div className="tablewrap"><table><thead><tr><th>Link</th><th>Loaded</th><th>Dropped (unresolved)</th></tr></thead><tbody>
          {Object.entries(result.links ?? {}).map(([k, n]) => { const d = typeof dropped === "object" ? dropped?.[k] ?? 0 : 0; return <tr key={k}><td><Trunc max={40}>{k}</Trunc></td><td>{n.toLocaleString()}</td><td className={d ? "bad-t" : ""}>{d.toLocaleString()}</td></tr>; })}</tbody></table></div>
        {droppedN > 0 && <div className="warnbox" role="alert"><b>{droppedN.toLocaleString()} link{droppedN === 1 ? "" : "s"} were dropped</b> because an endpoint was missing in the data. They are counted in the audit event and the import report, never skipped silently.</div>}
        <p>{ws?.api_base || result.api_base ? <button className="cta" onClick={() => switchWorkspace(slug!)}>Open workspace {slug}</button>
          : <span className="warnbox">No API is running for this workspace yet. Start one with its ontology (<code className="ref">workspaces/{slug}/ontology.yaml</code>) and it appears in the workspace picker.</span>}
          {" "}<a className="lnk" href="#/ontology">Back to the ontology manager</a></p>
      </section>}
    </div>
  );
}
