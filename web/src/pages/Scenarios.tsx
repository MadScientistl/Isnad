import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { EmptyState, ErrorBanner } from "../ui";
import { fmtT, toast, useApp, usePolled } from "../store";
import { INJECTION_KINDS, KPI_META, compareHref, fmtKpi, scnApi, type Injection, type Scenario, type SimJob } from "../scenarios";

const ago = (iso: string) => { const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000); return s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 129600 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`; };
const errText = (e: any) => (e instanceof ApiError ? (e.status === 429 ? `Limit reached: ${e.text}` : e.status === 403 ? `Not allowed: ${e.text}` : e.text) : String(e?.message ?? e));

export default function ScenariosPage() {
  const { role, roles, scenario, setScenario, refreshScenario } = useApp();
  const [all, setAll] = useState(false);
  const list = usePolled(() => scnApi.list(all ? "all" : "open"), [all, role], 5000);
  const [name, setName] = useState("");
  const [hours, setHours] = useState(168);
  const [shared, setShared] = useState<string[]>([]);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (!name.trim()) { setErr("A name is required."); return; }
    setBusy(true); setErr(undefined);
    try {
      const s = await scnApi.create({ name: name.trim(), expires_in_h: hours, shared_roles: shared });
      setName(""); setShared([]); setScenario(s.id); toast(`Scenario "${s.name}" created and activated`); list.reload();
    } catch (e) { setErr(errText(e)); } finally { setBusy(false); }
  };
  const discard = async (s: Scenario) => {
    if (!confirm(`Discard scenario "${s.name}"? Its ${s.override_rows} changed row(s) are deleted and cannot be merged afterwards.`)) return;
    try { await scnApi.discard(s.id); if (scenario === s.id) setScenario(null); toast(`Scenario "${s.name}" discarded`); list.reload(); } catch (e) { toast(errText(e), undefined, "err"); }
  };
  const active = list.data?.find((s) => s.id === scenario);
  return (
    <div className="page">
      <h2>Scenarios</h2>
      <p className="muted">A scenario is a private fork of main. Changes you make while one is active are recorded in the fork only; main changes when you merge, and anything that needs approval becomes a proposal.</p>
      <section className="card"><h3>New scenario</h3>
        <form className="form inline" onSubmit={(e) => { e.preventDefault(); create(); }}>
          <label><span>Name*</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Rotterdam closed for a week" maxLength={80} /></label>
          <label><span>Expires after (hours)</span><input type="number" min={1} max={720} value={hours} onChange={(e) => setHours(Number(e.target.value))} style={{ width: 90 }} /></label>
          <fieldset className="roles"><legend>Share with roles</legend>{roles.map((x) => x.role).filter((r) => r !== role).map((r) => (
            <label key={r} className="chk"><input type="checkbox" checked={shared.includes(r)} onChange={(e) => setShared(e.target.checked ? [...shared, r] : shared.filter((x) => x !== r))} /> {r}</label>))}</fieldset>
          <button className="primary" type="submit" disabled={busy}>{busy ? "Creating…" : "Create and switch"}</button>
        </form>
        {err && <div className="err" role="alert">{err}</div>}
      </section>
      <section className="card"><h3>Your scenarios <label className="chk" style={{ marginLeft: 12, textTransform: "none" }}><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> include merged, discarded, expired</label></h3>
        <ErrorBanner error={list.error} onRetry={list.reload} />
        <table><thead><tr><th>#</th><th>Name</th><th>Owner</th><th>Status</th><th>Age</th><th>Rows</th><th>Shared with</th><th></th></tr></thead><tbody>
          {(list.data ?? []).map((s) => (
            <tr key={s.id} className={scenario === s.id ? "scn-active" : ""}>
              <td>{s.id}</td><td><b>{s.name}</b>{scenario === s.id && <span className="chip scn" style={{ marginLeft: 6 }}>active</span>}</td><td>{s.owner_user}</td>
              <td><span className={`chip ${s.status === "open" ? "info" : s.status === "merged" ? "ok" : "warn"}`}>{s.status}</span></td>
              <td title={fmtT(s.created_at)}>{ago(s.created_at)}</td><td>{s.override_rows}</td><td>{s.shared_roles.join(", ") || <span className="muted">only you and admin</span>}</td>
              <td className="rowbtns">
                {s.status === "open" && (scenario === s.id ? <button onClick={() => setScenario(null)}>Exit</button> : <button onClick={() => setScenario(s.id)}>Switch</button>)}
                <a className="btnlink" href={compareHref(s.id)}>Compare</a>
                {s.status === "open" && <button className="danger" onClick={() => discard(s)}>Discard</button>}
              </td></tr>))}
          {list.data && !list.data.length && <tr><td colSpan={8} className="muted">No scenarios yet. Create one above.</td></tr>}
        </tbody></table>
      </section>
      <WhatIf scenario={active ?? null} onDone={() => { refreshScenario(); list.reload(); }} />
    </div>
  );
}

const blank = (): Injection => ({ kind: "port_closure", target: "", at_h: 0, duration_h: 48, magnitude: 1.5 });

function WhatIf({ scenario, onDone }: { scenario: Scenario | null; onDone: () => void }) {
  const { simAvailable } = useApp();
  const [days, setDays] = useState(7);
  const [seed, setSeed] = useState(1);
  const [inj, setInj] = useState<Injection[]>([blank()]);
  const [job, setJob] = useState<{ id: string; res?: SimJob } | null>(null);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const timer = useRef<number>();
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => { setJob(null); setErr(undefined); }, [scenario?.id]);
  if (simAvailable === false) return <section className="card"><h3>What-if simulation</h3><EmptyState title="Not available for this ontology">This workspace has no simulation, so scenarios hold manual changes only.</EmptyState></section>;
  if (!scenario) return <section className="card"><h3>What-if simulation</h3><p className="muted">Switch to a scenario to run a what-if simulation inside it. Main's simulation is never touched.</p></section>;
  const set = (i: number, p: Partial<Injection>) => setInj(inj.map((x, k) => (k === i ? { ...x, ...p } : x)));
  const clean = (x: Injection): Injection => {
    const o: Injection = { kind: x.kind };
    if (x.target?.trim()) o.target = x.target.trim();
    if (x.at_h !== undefined && !Number.isNaN(x.at_h)) o.at_h = x.at_h;
    if (x.kind !== "price_shock" && x.duration_h) o.duration_h = x.duration_h;
    if (x.kind === "price_shock" || x.kind === "supplier_delay") o.magnitude = x.magnitude;
    return o;
  };
  const poll = (sid: number, jid: string) => {
    scnApi.job(sid, jid).then((r) => {
      setJob({ id: jid, res: r });
      if (r.status === "running") timer.current = window.setTimeout(() => poll(sid, jid), 1000); else { setBusy(false); onDone(); }
    }, (e) => { setErr(errText(e)); setBusy(false); });
  };
  const run = async () => {
    if (!Number.isInteger(days) || days < 1 || days > 90) { setErr("Days must be a whole number from 1 to 90."); return; }
    setBusy(true); setErr(undefined); setJob(null);
    try { const r = await scnApi.simulate(scenario.id, { days, seed, injections: inj.map(clean) }); setJob({ id: r.job_id }); poll(scenario.id, r.job_id); }
    catch (e) { setErr(errText(e)); setBusy(false); }
  };
  const res = job?.res;
  return (
    <section className="card"><h3>What-if simulation in "{scenario.name}"</h3>
      <p className="muted small">Runs the simulation forward from the scenario's own state with optional injected disruptions. The same fork, seed and injections always give the same digest.</p>
      <div className="form inline">
        <label><span>Days (1 to 90)</span><input type="number" min={1} max={90} value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ width: 80 }} /></label>
        <label><span>Seed</span><input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} style={{ width: 80 }} /></label>
      </div>
      <table className="diff"><thead><tr><th>Injection</th><th>Target (id, optional)</th><th>Starts at (h)</th><th>Duration (h)</th><th>Magnitude</th><th></th></tr></thead><tbody>
        {inj.map((x, i) => (
          <tr key={i}>
            <td><select aria-label="Kind" value={x.kind} onChange={(e) => set(i, { kind: e.target.value as Injection["kind"] })}>{INJECTION_KINDS.map((k) => <option key={k} value={k}>{k.replace(/_/g, " ")}</option>)}</select></td>
            <td><input aria-label="Target" value={x.target ?? ""} placeholder="(chosen by the simulation)" onChange={(e) => set(i, { target: e.target.value })} /></td>
            <td><input aria-label="Starts at hour" type="number" min={0} value={x.at_h ?? 0} onChange={(e) => set(i, { at_h: Number(e.target.value) })} style={{ width: 70 }} /></td>
            <td><input aria-label="Duration hours" type="number" min={1} value={x.duration_h ?? 0} disabled={x.kind === "price_shock"} onChange={(e) => set(i, { duration_h: Number(e.target.value) })} style={{ width: 70 }} /></td>
            <td><input aria-label="Magnitude" type="number" step="0.1" min={0} value={x.magnitude ?? 1} disabled={x.kind !== "price_shock" && x.kind !== "supplier_delay"} onChange={(e) => set(i, { magnitude: Number(e.target.value) })} style={{ width: 70 }} /></td>
            <td><button aria-label="Remove injection" onClick={() => setInj(inj.filter((_, k) => k !== i))}>✕</button></td></tr>))}
      </tbody></table>
      <p><button onClick={() => setInj([...inj, blank()])}>Add injection</button> <button className="primary" disabled={busy} onClick={run}>{busy ? "Simulating…" : "Run what-if"}</button></p>
      {err && <div className="err" role="alert">{err}</div>}
      {job && !res && <p className="muted">Queued…</p>}
      {res?.status === "running" && <p className="muted" role="status">Running… (sim time {res.sim_now ? fmtT(res.sim_now) : "…"})</p>}
      {res?.status === "error" && <div className="err" role="alert">Simulation failed: {res.error}</div>}
      {res?.status === "done" && <div role="status">
        <p><span className="chip ok">done</span> sim time now {res.sim_now ? fmtT(res.sim_now) : ""} · digest <code className="ref">{res.digest}</code>
          {res.actions && <> · {Object.entries(res.actions).map(([k, v]) => `${v} ${k}`).join(", ")}</>}</p>
        {res.kpis && <div className="kpigrid">{KPI_META.filter((m) => m.k in res.kpis!).map((m) => <div key={m.k} className="kpicard"><div className="k">{m.label}</div><div className="vals"><b>{fmtKpi(res.kpis![m.k], m.fmt)}</b></div></div>)}</div>}
        <p><a className="lnk" href={compareHref(scenario.id)}>Compare with main</a></p></div>}
    </section>
  );
}
