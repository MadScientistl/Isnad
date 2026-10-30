import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { api, type GraphResp, type Metric } from "../api";
import { toast, useApp, usePolled } from "../store";
import { DiffView } from "./Audit";
import { MapView } from "./MapPage";
import { MergeDialog } from "../ScenarioBanner";
import { KPI_META, deltaCls, fmtDelta, fmtKpi, compareHref, parseCompareHash, scnApi, type Scenario } from "../scenarios";

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

const label = (x: Scenario | { id: number | "main" }) => ("name" in x ? `#${x.id} ${x.name}` : x.id === "main" ? "main" : `#${x.id}`);

export default function ComparePage() {
  const { setScenario, scenario } = useApp();
  const { id, withS } = parseCompareHash();
  const [merging, setMerging] = useState(false);
  const cmp = usePolled(() => (id ? scnApi.compare(id, withS) : Promise.reject(new Error("API 404: no scenario"))), [id, withS]);
  const scn = usePolled(() => (id ? scnApi.get(id) : Promise.reject(new Error("API 404"))), [id]);
  const others = usePolled(() => scnApi.list("open"), []);
  const c = cmp.data;
  // contract: a = the path scenario, b = the `with` side (main by default). Delta shown here is always scenario minus baseline.
  const changed = useMemoSet(c?.diff.filter((d) => d.kind === "object").map((d) => d.key));
  if (!id) return <div className="page"><div className="err">No scenario selected.</div><a className="lnk" href="#/scenarios">Scenarios</a></div>;
  return (
    <div className="page cmppage">
      <h2>Compare scenario {scn.data ? `"${scn.data.name}"` : `#${id}`} <a className="lnk small" href="#/scenarios">all scenarios</a></h2>
      <div className="toolbar">
        <label>Baseline <select value={String(withS)} onChange={(e) => { location.hash = compareHref(id, e.target.value === "main" ? "main" : Number(e.target.value)); }}>
          <option value="main">main</option>
          {(others.data ?? []).filter((s) => s.id !== id).map((s) => <option key={s.id} value={s.id}>#{s.id} {s.name}</option>)}</select></label>
        {scn.data && scenario !== id && scn.data.status === "open" && <button onClick={() => { setScenario(id); toast(`Switched to scenario "${scn.data!.name}"`); }}>Switch to this scenario</button>}
        {scn.data?.status === "open" && <button className="primary" onClick={() => setMerging(true)}>Merge into main…</button>}
        <button onClick={() => { cmp.reload(); scn.reload(); }}>Refresh</button>
        <span className="muted small">Differences are computed under your role, so objects you cannot see are not listed.</span>
      </div>
      {cmp.error && <div className="err">{/API 404/.test(cmp.error) ? "Scenario not found, or not visible to your role." : cmp.error}</div>}
      {cmp.loading && !c && <p className="muted">Comparing…</p>}
      {c && <>
        {c.truncated && <div className="warnbox">The diff is truncated; counts and KPIs are complete but the table lists only the first rows.</div>}
        <div className="kpigrid" aria-label="KPI comparison">
          {KPI_META.map((m) => {
            const base = c.kpis.b[m.k], cur = c.kpis.a[m.k], d = (cur ?? 0) - (base ?? 0);
            return <div key={m.k} className={`kpicard ${deltaCls(d, m.good)}`}>
              <div className="k">{m.label}</div>
              <div className="vals"><span title={`${label(c.b)}`}>{fmtKpi(base, m.fmt)}</span><span className="arrow" aria-hidden>→</span><b title={`${label(c.a)}`}>{fmtKpi(cur, m.fmt)}</b></div>
              <div className={`delta ${deltaCls(d, m.good)}`}>{fmtDelta(d, m.fmt)}{d !== 0 && m.good && <span className="sr"> {deltaCls(d, m.good) === "good" ? "(better)" : "(worse)"}</span>}</div>
            </div>;
          })}
        </div>
        <p className="muted small kpilegend"><span className="swatch good" /> better <span className="swatch bad" /> worse <span className="swatch neutral" /> neutral. Left value is {label(c.b)}, right value is {label(c.a)}.</p>
        <section className="card"><h3>Changed objects ({c.diff.length})</h3><DiffView diff={c.diff} /></section>
        <section className="card"><h3>Map</h3>
          <div className="mapsplit">
            <div><MapView scn={withS} embedded changed={changed} title={`Baseline: ${label(c.b)}`} /></div>
            <div><MapView scn={id} embedded changed={changed} title={`Scenario: ${label(c.a)}`} /></div>
          </div>
          <p className="muted small">Magenta rings mark objects that differ between the two sides.</p>
        </section>
        <GraphDiff id={id} withS={withS} diffKeys={c.diff.filter((d) => d.kind === "object")} />
        <SeriesPanel id={id} withS={withS} />
      </>}
      {merging && scn.data && <MergeDialog scenario={scn.data} onClose={() => { setMerging(false); cmp.reload(); scn.reload(); }} />}
    </div>
  );
}

function useMemoSet(keys: string[] | undefined) { return useMemo(() => new Set(keys ?? []), [keys?.join("|")]); } // eslint-disable-line

/* ---------------- graph diff ---------------- */
const ek = (e: { src: string; dst: string; link: string; inferred: boolean }) => `${e.src}|${e.link}|${e.dst}|${e.inferred ? "i" : "a"}`;
const nlab = (n: { label: string; status: string | null }) => n.label + (n.status ? ` [${n.status}]` : "");

function GraphDiff({ id, withS, diffKeys }: { id: number; withS: "main" | number; diffKeys: { type: string; key: string }[] }) {
  const [centre, setCentre] = useState("");
  const [depth, setDepth] = useState(2);
  useEffect(() => { if (!centre && diffKeys.length) setCentre(diffKeys[0].key); }, [diffKeys, centre]);
  const g = usePolled(async () => {
    if (!centre) return null;
    const [l, r] = await Promise.allSettled([api.graph(centre, depth, undefined, true, withS), api.graph(centre, depth, undefined, true, id)]);
    return { l: l.status === "fulfilled" ? l.value : null, r: r.status === "fulfilled" ? r.value : null, le: l.status === "rejected" ? String(l.reason?.message ?? l.reason) : null, re: r.status === "rejected" ? String(r.reason?.message ?? r.reason) : null };
  }, [centre, depth, id, withS]);
  const ref = useRef<HTMLDivElement>(null);
  const stats = useMemo(() => {
    const d = g.data; if (!d?.l && !d?.r) return null;
    const ln = new Map((d.l?.nodes ?? []).map((n) => [n.id, n])), rn = new Map((d.r?.nodes ?? []).map((n) => [n.id, n]));
    const le = new Map((d.l?.edges ?? []).map((e) => [ek(e), e])), re = new Map((d.r?.edges ?? []).map((e) => [ek(e), e]));
    return { ln, rn, le, re };
  }, [g.data]);
  useEffect(() => {
    if (!ref.current || !stats || !g.data) return;
    const { ln, rn, le, re } = stats;
    const cls = (a?: GraphResp["nodes"][0], b?: GraphResp["nodes"][0]) => (!a ? "added" : !b ? "removed" : a.status !== b.status || a.label !== b.label ? "changed" : "same");
    const ids = new Set([...ln.keys(), ...rn.keys()]);
    const els: cytoscape.ElementDefinition[] = [
      ...[...ids].map((nid) => { const a = ln.get(nid), b = rn.get(nid), n = (b ?? a)!; const c = cls(a, b);
        return { data: { id: nid, label: nlab(n) + (c === "changed" && a ? ` (was ${a.status ?? "-"})` : ""), type: n.type, centre: nid === centre ? 1 : 0 }, classes: c }; }),
      ...[...new Set([...le.keys(), ...re.keys()])].map((k, i) => { const e = (re.get(k) ?? le.get(k))!; const c = !le.has(k) ? "added" : !re.has(k) ? "removed" : "same";
        return { data: { id: `e${i}`, source: e.src, target: e.dst, label: e.link + (e.inferred ? " (inferred)" : "") }, classes: `${c}${e.inferred ? " inferred" : ""}` }; }),
    ].filter((x) => !("source" in x.data) || (ids.has((x.data as any).source) && ids.has((x.data as any).target)));
    const cy = cytoscape({ container: ref.current, elements: els, layout: { name: "cose", animate: false, nodeRepulsion: () => 40000, idealEdgeLength: () => 120, padding: 24 } as any,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, "background-color": "#8a94a6", color: "#1c2024", "text-valign": "bottom", "text-margin-y": 3, "border-width": 2, "border-color": "#fff", "text-background-color": "#fff", "text-background-opacity": 0.8, "text-background-padding": "1px" } },
        { selector: "node[centre = 1]", style: { width: 34, height: 34, "border-color": "#0f172a", "border-width": 4 } },
        { selector: "node.added", style: { "background-color": "#009E73", "border-color": "#009E73", shape: "round-rectangle", "border-width": 4 } },
        { selector: "node.removed", style: { "background-color": "#D55E00", "border-color": "#D55E00", shape: "diamond", "border-style": "dashed", "border-width": 4 } },
        { selector: "node.changed", style: { "background-color": "#E69F00", "border-color": "#CC1F8A", "border-width": 5, shape: "star", width: 30, height: 30 } },
        { selector: "edge", style: { label: "data(label)", "font-size": 8, color: "#334155", width: 1.4, "line-color": "#9aa3b2", "target-arrow-color": "#9aa3b2", "target-arrow-shape": "triangle", "curve-style": "bezier", "text-background-color": "#fff", "text-background-opacity": 0.8, "text-background-padding": "1px" } },
        { selector: "edge.inferred", style: { "line-style": "dashed" } },
        { selector: "edge.added", style: { "line-color": "#009E73", "target-arrow-color": "#009E73", width: 3.5, color: "#00573f" } },
        { selector: "edge.removed", style: { "line-color": "#D55E00", "target-arrow-color": "#D55E00", width: 3.5, "line-style": "dotted", color: "#8a3200" } },
      ] });
    return () => cy.destroy();
  }, [stats, g.data, centre]);
  const counts = useMemo(() => {
    if (!stats) return null;
    const { ln, rn, le, re } = stats;
    const nodes = [...new Set([...ln.keys(), ...rn.keys()])];
    return { na: nodes.filter((n) => !ln.has(n)).length, nr: nodes.filter((n) => !rn.has(n)).length, nc: nodes.filter((n) => ln.has(n) && rn.has(n) && (ln.get(n)!.status !== rn.get(n)!.status)).length,
      ea: [...re.keys()].filter((k) => !le.has(k)).length, er: [...le.keys()].filter((k) => !re.has(k)).length };
  }, [stats]);
  return (
    <section className="card"><h3>Graph diff</h3>
      <div className="toolbar">
        <label>Centre <select value={centre} onChange={(e) => setCentre(e.target.value)}>
          {!diffKeys.length && <option value="">(no changed objects)</option>}
          {diffKeys.map((d) => <option key={d.key} value={d.key}>{d.type} {d.key.slice(0, 8)}</option>)}</select></label>
        <label>Depth <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>{[1, 2, 3].map((d) => <option key={d}>{d}</option>)}</select></label>
        {counts && <span className="muted small">{counts.na} node(s) added, {counts.nr} removed, {counts.nc} changed; {counts.ea} edge(s) added, {counts.er} removed</span>}
      </div>
      {g.data && (g.data.le || g.data.re) && <div className="warnbox">{g.data.le && <div>Baseline graph: {g.data.le}</div>}{g.data.re && <div>Scenario graph: {g.data.re}</div>}</div>}
      {!diffKeys.length && <p className="muted">Nothing differs, so there is no graph to compare.</p>}
      <div className="cy" ref={ref} role="img" aria-label="Graph difference between the two sides" style={{ height: 380 }} />
      <div className="legend">
        <span><i className="sw circle" style={{ background: "#009E73" }} />added (rounded square, thick border)</span>
        <span><i className="sw circle" style={{ background: "#D55E00" }} />removed (diamond, dashed border)</span>
        <span><i className="sw circle" style={{ background: "#E69F00", border: "3px solid #CC1F8A" }} />status changed (star)</span>
        <span><i />unchanged edge; <i className="dash" />inferred</span>
      </div>
    </section>
  );
}

/* ---------------- KPI series ---------------- */
const SERIES: { m: Metric; t: string }[] = [
  { m: "value_at_risk", t: "Value at risk" }, { m: "shipments_delayed", t: "Shipments delayed" }, { m: "shipments_in_transit", t: "Shipments in transit" },
  { m: "ports_closed", t: "Ports closed" }, { m: "invoices_flagged", t: "Invoices flagged" },
];
function SeriesPanel({ id, withS }: { id: number; withS: "main" | number }) {
  const [metric, setMetric] = useState<Metric>("value_at_risk");
  const { sim } = useApp();
  const d = usePolled(async () => {
    const sc = await scnApi.series(id, metric);
    if (!sc.points.length) return { sc, base: null };
    const from = sc.points[0].t, last = sc.points[sc.points.length - 1].t;
    const base = withS === "main" ? await api.series(metric, from, sim && Date.parse(sim.sim_now) < Date.parse(last) ? sim.sim_now : last, 24, "main") : await scnApi.series(withS, metric);
    return { sc, base };
  }, [id, withS, metric, sim?.sim_now ? 1 : 0]);
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current); chart.current = c;
    const ro = new ResizeObserver(() => c.resize()); ro.observe(el.current);
    return () => { ro.disconnect(); c.dispose(); chart.current = null; };
  }, []);
  useEffect(() => {
    if (!d.data) return;
    chart.current?.setOption({
      animation: false, color: ["#8a94a6", "#CC1F8A"], grid: { left: 52, right: 16, top: 30, bottom: 28 },
      legend: { top: 0, data: [withS === "main" ? "main" : `scenario #${withS}`, `scenario #${id}`] },
      tooltip: { trigger: "axis" },
      xAxis: { type: "time", axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: { type: "value", axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: "#eceef2" } } },
      series: [
        { name: withS === "main" ? "main" : `scenario #${withS}`, type: "line", showSymbol: false, lineStyle: { width: 2, type: "dashed" }, data: (d.data.base?.points ?? []).map((p) => [p.t, p.v]) },
        { name: `scenario #${id}`, type: "line", showSymbol: false, lineStyle: { width: 2.5 }, data: d.data.sc.points.map((p) => [p.t, p.v]) },
      ],
    }, true);
  }, [d.data, id, withS]);
  return (
    <section className="card"><h3>KPI over time</h3>
      <div className="toolbar"><label>Metric <select value={metric} onChange={(e) => setMetric(e.target.value as Metric)}>{SERIES.map((s) => <option key={s.m} value={s.m}>{s.t}</option>)}</select></label>
        <span className="muted small">Main is shown up to its current sim time; the scenario continues on its own what-if clock.</span></div>
      {d.error && <div className="err">{d.error}</div>}
      <div className="chart" style={{ height: 240 }} ref={el} role="img" aria-label={`${metric} over time, main versus scenario`} />
    </section>
  );
}
