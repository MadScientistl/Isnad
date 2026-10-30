import { useState } from "react";
import { api, type Speed, type GeoFeature } from "./api";
import { fmtT, useApp, usePolled } from "./store";

const SPEEDS: { v: Speed; l: string }[] = [{ v: "paused", l: "Paused" }, { v: "1", l: "1 h/s" }, { v: "6", l: "6 h/s" }, { v: "24", l: "24 h/s" }];
const H = 3600000;

export default function TimeBar() {
  const { sim, simError, setSim, asOf, setAsOf, scenario } = useApp();
  const lock = scenario !== null;
  const lockTip = lock ? "Not available inside a scenario: main's simulation is not touched. Use the scenario's what-if panel instead." : undefined;
  const [err, setErr] = useState<string>();
  const ctl = async (b: { speed?: Speed; step?: boolean; reset?: boolean }) => {
    try { setSim(await api.simControl(b)); setErr(undefined); } catch (e: any) { setErr(String(e.message ?? e)); }
  };
  const reset = () => { if (confirm("Reset the simulation to its start? All simulated state is re-seeded.")) { setAsOf(null); ctl({ reset: true }); } };
  const min = sim ? Date.parse(sim.epoch) : 0;
  const max = sim ? Date.parse(sim.sim_now) : 0;
  const value = asOf ? Math.min(Math.max(asOf.getTime(), min), max) : max;
  const live = asOf === null;
  return (
    <div className="timebar" role="region" aria-label="Simulation time controls">
      <div className="tb-clock" title="Simulation clock">
        <span className="tb-k">{lock ? "Main sim time" : "Sim time"}</span><b data-testid="sim-now">{sim ? fmtT(sim.sim_now) : "..."}</b>
      </div>
      <div className="seg" role="group" aria-label="Simulation speed" title={lockTip}>
        {SPEEDS.map((s) => (
          <button key={s.v} className={sim?.speed === s.v ? "on" : ""} aria-pressed={sim?.speed === s.v} disabled={lock} onClick={() => ctl({ speed: s.v })}>{s.l}</button>))}
      </div>
      <button disabled={lock} onClick={() => ctl({ step: true })} title={lockTip ?? "Advance one simulated hour"}>Step +1 h</button>
      <button className="danger" disabled={lock} title={lockTip} onClick={reset}>Reset</button>
      <div className="tb-slider">
        <label htmlFor="asof" className="tb-k">As of</label>
        <input id="asof" type="range" min={min} max={Math.max(max, min + H)} step={H} value={value} disabled={!sim}
          onChange={(e) => setAsOf(new Date(Number(e.target.value)))} aria-valuetext={live ? "live" : fmtT(asOf)} />
        <b className="tb-asof">{live ? "Live" : fmtT(asOf)}</b>
        <button className={live ? "on" : ""} aria-pressed={live} onClick={() => setAsOf(null)}>Live</button>
      </div>
      <Inject lock={lock} tip={lockTip} />
      <div className="tb-stat" aria-live="polite">
        {sim?.achieved_hours_per_s != null && <span className="muted">{sim.achieved_hours_per_s.toFixed(1)} h/s achieved</span>}
        {(sim?.last_error || err || simError) && <span className="tb-err" role="alert">{sim?.last_error ?? err ?? simError}</span>}
      </div>
    </div>
  );
}

function Inject({ lock, tip }: { lock: boolean; tip?: string }) {
  const [kind, setKind] = useState("port_closure");
  const [target, setTarget] = useState("");
  const [dur, setDur] = useState(48);
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState<string>();
  const ports = usePolled(() => (open && kind === "port_closure" ? api.geo("Port") : Promise.resolve(null)), [open, kind]);
  const go = async () => {
    try {
      const r = await api.inject({ kind, target: target || undefined, duration_h: dur });
      setMsg(`Injected ${r.injected.kind}`);
    } catch (e: any) { setMsg(String(e.message ?? e)); }
  };
  return (
    <details className="inject" open={open && !lock} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} title={tip}>
      <summary style={lock ? { opacity: 0.5, pointerEvents: "none" } : undefined} aria-disabled={lock}>Inject disruption</summary>
      <div className="inject-body">
        <label>Kind <select value={kind} onChange={(e) => { setKind(e.target.value); setTarget(""); }}>
          <option value="port_closure">Port closure</option><option value="shipment_delay">Shipment delay</option><option value="invoice_anomaly">Invoice anomaly</option></select></label>
        {kind === "port_closure"
          ? <label>Port <select value={target} onChange={(e) => setTarget(e.target.value)}><option value="">(any)</option>
            {ports.data?.features.map((f: GeoFeature) => <option key={f.id} value={f.id}>{String(f.properties.name ?? f.id)}</option>)}</select></label>
          : <span className="muted">Target chosen by the simulation</span>}
        <label>Duration (h) <input type="number" min={1} value={dur} onChange={(e) => setDur(Number(e.target.value))} style={{ width: 70 }} /></label>
        <button onClick={go}>Inject</button>
        {msg && <span className="muted" role="status">{msg}</span>}
      </div>
    </details>
  );
}
