import { useEffect, useState } from "react";
import { ApiError } from "./api";
import { fmtT, toast, useApp } from "./store";
import { compareHref, scnApi, type MergeConflict, type MergeOk, type Scenario } from "./scenarios";

/** Shown under the header while a scenario is active, so a user cannot mistake it for main. */
export default function ScenarioBanner() {
  const { scenario, scenarioInfo: s, setScenario } = useApp();
  const [merging, setMerging] = useState(false);
  if (!scenario) return null;
  const discard = async () => {
    if (!s || !confirm(`Discard scenario "${s.name}"? Its changes are deleted and cannot be merged afterwards.`)) return;
    try { await scnApi.discard(s.id); toast(`Scenario "${s.name}" discarded`); setScenario(null); } catch (e: any) { toast(String(e.message ?? e), undefined, "err"); }
  };
  return (
    <div className="scnbanner" role="region" aria-label="Active scenario">
      <span className="scn-tag">SCENARIO</span>
      <b className="scn-name">{s?.name ?? `#${scenario}`}</b>
      {s ? <>
        <span>owner {s.owner_user}</span>
        <span className="chip info">{s.status}</span>
        <span title="Main state this scenario was forked from">base {fmtT(s.base_sim_time)}</span>
        <span title="What-if clock">clock {fmtT(s.clock ?? s.base_sim_time)}</span>
        <span>{s.override_rows} changed row{s.override_rows === 1 ? "" : "s"}</span>
      </> : <span className="muted">loading…</span>}
      <span className="scn-sp" />
      <span className="scn-note">Nothing you do here changes main until you merge.</span>
      <a className="btnlink" href={compareHref(scenario)}>Compare</a>
      <button disabled={!s} onClick={() => setMerging(true)}>Merge</button>
      <button className="danger" disabled={!s} onClick={discard}>Discard</button>
      <button onClick={() => { setScenario(null); }}>Exit scenario</button>
      {merging && s && <MergeDialog scenario={s} onClose={() => setMerging(false)} />}
    </div>
  );
}

/** Merge confirmation: shows conflicts (409) or the executed actions and created proposals. */
export function MergeDialog({ scenario, onClose }: { scenario: Scenario; onClose: () => void }) {
  const { setScenario, scenario: active } = useApp();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [conflicts, setConflicts] = useState<MergeConflict[]>();
  const [done, setDone] = useState<MergeOk>();
  useEffect(() => { const f = (e: KeyboardEvent) => e.key === "Escape" && !busy && close(); addEventListener("keydown", f); return () => removeEventListener("keydown", f); });
  const close = () => { if (done && active === scenario.id) setScenario(null); onClose(); };
  const go = async () => {
    setBusy(true); setErr(undefined); setConflicts(undefined);
    try { const r = await scnApi.merge(scenario.id, comment.trim() || undefined); setDone(r.data); }
    catch (e: any) {
      if (e instanceof ApiError && e.status === 409 && e.code === "merge_conflict") setConflicts((e.payload.conflicts ?? []) as MergeConflict[]);
      else if (e instanceof ApiError && e.status === 429) setErr(`Scenario limit reached: ${e.text}`);
      else if (e instanceof ApiError && e.status === 404) setErr("Scenario not found (already merged or discarded?).");
      else if (e instanceof ApiError && e.status === 403) setErr(`Not allowed: ${e.text}`);
      else setErr(e instanceof ApiError ? e.text : String(e.message ?? e));
    } finally { setBusy(false); }
  };
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label="Merge scenario" onClick={() => !busy && close()}>
      <div className="dlg wide" onClick={(e) => e.stopPropagation()}>
        <h3>Merge "{scenario.name}" into main</h3>
        {!done && <>
          <p className="muted">The actions recorded in this scenario are replayed on main as you. Actions that need approval become proposals. Simulated events and injections are never merged. If main changed on any touched object since the fork, the whole merge is refused.</p>
          <div className="form"><label><span>Comment</span><input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="optional" /></label></div>
        </>}
        {conflicts && <div className="err" role="alert"><b>Merge refused: {conflicts.length} conflict{conflicts.length === 1 ? "" : "s"}. Nothing was changed on main.</b>
          <table className="diff"><thead><tr><th>Kind</th><th>Object</th><th>Reason</th></tr></thead><tbody>
            {conflicts.map((c, i) => <tr key={i}><td>{c.kind}</td><td>{c.type} <span className="muted">{c.key.slice(0, 8)}</span></td><td>{c.reason}</td></tr>)}</tbody></table>
          <p className="small">Resolve by discarding this scenario and re-doing the change on current main, or by forking a new scenario.</p></div>}
        {err && <div className="err" role="alert">{err}</div>}
        {done && <div role="status"><p><span className="chip ok">merged</span> {done.merged.length} step{done.merged.length === 1 ? "" : "s"} applied to main.</p>
          {done.merged.length > 0 && <table className="diff"><thead><tr><th>Action</th><th>Result</th></tr></thead><tbody>
            {done.merged.map((m, i) => <tr key={i}><td><b>{m.name}</b></td><td>{m.action_id ? <><span className="chip ok">executed</span> <a className="lnk" href={`#/audit/${m.action_id}`} onClick={close}>audit entry</a></>
              : <><span className="chip warn">proposal</span> <a className="lnk" href="#/approvals" onClick={close}>approvals inbox</a></>}</td></tr>)}</tbody></table>}
          {done.merged.some((m) => m.proposal_id) && <p className="muted small">Proposals still need an approver. They are not applied yet.</p>}</div>}
        <p>{!done && <button className="primary" disabled={busy} onClick={go}>{busy ? "Merging…" : conflicts ? "Retry merge" : "Merge into main"}</button>} <button onClick={close}>{done ? "Close" : "Cancel"}</button></p>
      </div>
    </div>
  );
}
