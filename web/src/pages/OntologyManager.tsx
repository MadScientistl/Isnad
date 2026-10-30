import { useEffect, useMemo, useState } from "react";
import { ApiError, ontApi, type OntProposal, type OntVersion } from "../api";
import OntDiff from "../OntDiff";
import { fmtT, toast, useApp, usePolled } from "../store";
import { EmptyState, ErrorBanner, Spinner, Trunc } from "../ui";

const errText = (e: unknown) => (e instanceof ApiError ? e.text : String((e as Error)?.message ?? e));
const srcCls = (s: string) => (s === "rollback" ? "warn" : s === "import" ? "info" : s === "proposal" ? "ok" : "");

export default function OntologyManager() {
  const { role, types, roles, workspace, me } = useApp();
  const ver = usePolled(() => ontApi.versions(), [role]);
  const props = usePolled(() => ontApi.proposals(), [role]);
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [yamlFor, setYamlFor] = useState<number | null>(null);
  const versions = ver.data ?? [];
  const latest = versions[0];
  useEffect(() => { if (versions.length >= 1 && to === null) { setTo(versions[0].id); setFrom((versions[1] ?? versions[0]).id); } }, [versions, to]);
  const diff = usePolled(() => (from !== null && to !== null ? ontApi.diff(from, to) : Promise.resolve(null)), [from, to]);
  const yaml = usePolled(() => (yamlFor !== null ? ontApi.version(yamlFor) : Promise.resolve(null)), [yamlFor]);
  const summary = useMemo(() => {
    const ot = types?.objectTypes ?? [];
    return { types: ot.length, props: ot.reduce((a, t) => a + t.properties.length, 0), links: types?.linkTypes.length ?? 0, geo: ot.filter((t) => t.geo).length };
  }, [types]);
  const rollback = async (v: OntVersion) => {
    if (!confirm(`Roll back to version ${v.id}? A new version is recorded that restores its definition. Data and columns are kept. The API needs a restart to apply it.`)) return;
    try { await ontApi.rollback(v.id); toast(`Rolled back to version ${v.id}. Restart the API to apply.`, undefined, "warn"); ver.reload(); setTo(null); } catch (e) { toast(errText(e), undefined, "err"); }
  };
  if (role !== "admin") return <div className="page"><EmptyState title="The ontology manager is for admins">Versions, change proposals and rollbacks are only visible to the admin role. Switch roles to continue.</EmptyState></div>;
  const pend = (props.data ?? []).filter((p) => !p.decision);
  const shown = showAll ? props.data ?? [] : pend;
  return (
    <div className="page ontpage">
      <h2>Ontology <span className="muted small">workspace {workspace}</span></h2>
      <p><a className="btnlink cta" href="#/ontology/import">Import data as a new ontology</a></p>

      <section className="card"><h3>Current ontology</h3>
        {!types ? <Spinner label="Loading" /> : <ul className="plain sumlist">
          <li><b>{summary.types}</b> object types</li><li><b>{summary.props}</b> properties</li><li><b>{summary.links}</b> link types</li>
          <li><b>{roles.length}</b> roles</li><li><b>{me?.actions?.length ?? 0}</b> actions you can run</li><li><b>{summary.geo}</b> mappable types</li>
          {latest && <li>version <b>{latest.id}</b> <code className="ref" title={latest.hash}>{latest.hash.slice(0, 10)}</code></li>}</ul>}
        {types && !summary.types && <EmptyState title="No object types">This workspace has an empty ontology.</EmptyState>}
      </section>

      <section className="card"><h3>Versions ({versions.length})</h3>
        <p className="small muted">Versions are append-only. Changes are additive: removals and type changes are rejected. A rollback records a new version and keeps all data.</p>
        {ver.loading && !ver.data && <Spinner label="Loading versions" />}
        <ErrorBanner error={ver.error} onRetry={ver.reload} />
        {ver.data && !versions.length && <EmptyState title="No versions yet">The API registers the running definition at startup.</EmptyState>}
        {versions.length > 0 && <div className="tablewrap"><table><thead><tr><th>Version</th><th>Source</th><th>Hash</th><th>Created</th><th>By</th><th>Note</th><th>Diff</th><th></th></tr></thead><tbody>
          {versions.map((v) => <tr key={v.id} className={v.id === latest.id ? "cur-row" : ""}>
            <td><b>{v.id}</b>{v.id === latest.id && <span className="chip ok"> current</span>}</td><td><span className={`chip ${srcCls(v.source)}`}>{v.source}</span></td>
            <td><code className="ref" title={v.hash}>{v.hash.slice(0, 10)}</code>{v.parent_hash && <span className="muted small" title={v.parent_hash}> ← {v.parent_hash.slice(0, 6)}</span>}</td>
            <td>{fmtT(v.created_at)}</td><td><Trunc max={24}>{v.created_by}</Trunc></td><td><Trunc max={50}>{v.note ?? ""}</Trunc></td>
            <td className="rowbtns"><label className="chk"><input type="radio" name="dfrom" checked={from === v.id} onChange={() => setFrom(v.id)} aria-label={`Diff from version ${v.id}`} /> from</label>
              <label className="chk"><input type="radio" name="dto" checked={to === v.id} onChange={() => setTo(v.id)} aria-label={`Diff to version ${v.id}`} /> to</label></td>
            <td className="rowbtns"><button onClick={() => setYamlFor(yamlFor === v.id ? null : v.id)} aria-expanded={yamlFor === v.id}>YAML</button>
              <button disabled={v.id === latest.id} onClick={() => rollback(v)} title={v.id === latest.id ? "Already current" : "Restore this definition as a new version"}>Roll back</button></td></tr>)}</tbody></table></div>}
        {yamlFor !== null && <div>{yaml.loading && !yaml.data && <Spinner />}<ErrorBanner error={yaml.error} onRetry={yaml.reload} />{yaml.data && <pre aria-label={`YAML of version ${yamlFor}`}>{yaml.data.yaml}</pre>}</div>}
      </section>

      <section className="card"><h3>Diff {from !== null && to !== null && <span className="muted small">version {from} → {to}</span>}</h3>
        {versions.length < 2 && ver.data && <p className="muted">There is only one version, so there is nothing to compare yet.</p>}
        {diff.loading && !diff.data && from !== null && <Spinner label="Computing diff" />}
        <ErrorBanner error={diff.error} onRetry={diff.reload} />
        {diff.data && (from === to ? <p className="muted">Pick two different versions.</p> : <OntDiff diff={diff.data} />)}
      </section>

      <section className="card"><h3>Change proposals ({pend.length} pending)</h3>
        <label className="chk small"><input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> Show decided proposals</label>
        {props.loading && !props.data && <Spinner label="Loading proposals" />}
        <ErrorBanner error={props.error} onRetry={props.reload} />
        {props.data && !shown.length && <EmptyState title={showAll ? "No proposals" : "No pending proposals"}>Changes proposed by admins or by an agent appear here for a human decision.</EmptyState>}
        {shown.map((p) => <ProposalCard key={p.id} p={p} onDone={() => { props.reload(); ver.reload(); setTo(null); }} />)}
      </section>
    </div>
  );
}

function ProposalCard({ p, onDone }: { p: OntProposal; onDone: () => void }) {
  const [comment, setComment] = useState("");
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const sensitive = (p.diff.sensitive?.length ?? 0) > 0;
  const decided = !!p.decision;
  const decide = async (approve: boolean) => {
    setBusy(true); setErr(undefined);
    try { const r = await ontApi.decide(p.id, approve, comment.trim()); toast(approve ? `Approved${r.notes?.[0] ? `. ${r.notes[0]}` : ""}` : "Rejected", undefined, approve ? "ok" : "warn"); onDone(); }
    catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  return (
    <article className={`card propcard2${decided ? " decided" : ""}`} aria-label={`Proposal ${p.id.slice(0, 8)}`}>
      <h4>{p.rationale ? <Trunc max={120}>{p.rationale}</Trunc> : <span className="muted">No rationale</span>}
        {p.breaking && <span className="chip bad"> breaking</span>}{sensitive && <span className="chip restricted"> sensitive</span>}
        {decided && <span className={`chip ${p.decision === "approved" ? "ok" : "bad"}`}> {p.decision}{p.version_id ? ` as v${p.version_id}` : ""}</span>}</h4>
      <p className="small muted">Proposed by <b><Trunc max={30}>{p.proposer}</Trunc></b> {fmtT(p.created_at)} against <code className="ref" title={p.base_hash}>{p.base_hash.slice(0, 8)}</code>{decided && p.approver && <> · decided by <b>{p.approver}</b>{p.decision_comment ? <> · “<Trunc max={80}>{p.decision_comment}</Trunc>”</> : null}</>}</p>
      <OntDiff diff={p.diff} />
      {!decided && <div className="decide">
        <label className="ed wide">Comment{sensitive ? " (required: this widens permissions)" : " (optional)"}<textarea rows={2} value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} aria-invalid={sensitive && !comment.trim()} /></label>
        <ErrorBanner error={err} />
        <div className="rowbtns"><button className="primary" disabled={busy || p.breaking || (sensitive && !comment.trim())} onClick={() => decide(true)}
          title={p.breaking ? "Breaking changes cannot be applied (additive only)" : sensitive && !comment.trim() ? "Add a comment first" : undefined}>Approve</button>
          <button className="danger" disabled={busy} onClick={() => decide(false)}>Reject</button>
          {p.breaking && <span className="small bad-t">Breaking: can only be rejected.</span>}</div>
        <p className="small muted">Approving records a new version; the running API needs a restart to use it.</p>
      </div>}
    </article>
  );
}
