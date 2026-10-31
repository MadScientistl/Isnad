import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent, MarkLineComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { api, type Metric, type EventRow } from "../api";
import { useAsOf, usePolled, fmtT } from "../store";
import { ScnChip } from "../ScnUi";
import { Bar, EmptyState, ErrorBanner, Spinner, Trunc } from "../ui";

echarts.use([LineChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

const METRICS: { m: Metric; title: string; color: string; money?: boolean }[] = [
  { m: "shipments_in_transit", title: "Shipments in transit", color: "#6cc4ff" },
  { m: "shipments_delayed", title: "Shipments delayed", color: "#B07A00" },
  { m: "ports_closed", title: "Ports closed", color: "#D55E00" },
  { m: "invoices_flagged", title: "Invoices flagged", color: "#e79ac4" },
  { m: "value_at_risk", title: "Value at risk", color: "#c79bff", money: true },
];
const money = (v: number) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(Math.round(v)));

const num = (v: unknown) => (typeof v === "number" ? v : typeof v === "object" && v && "value" in v ? Number((v as { value: number }).value) : NaN);
const humanize = (k: string) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export default function Dashboard() {
  const { asOfQ, asOf, live, sim } = useAsOf();
  const poll = live ? 3000 : undefined;
  const sum = usePolled(() => api.summary(asOfQ), [asOfQ], poll);
  const ev = usePolled(() => api.events(asOfQ, 60), [asOfQ], poll);
  const epoch = sim?.epoch;
  const end = asOf ? asOf.toISOString() : sim?.sim_now;
  const marker = end;
  const s = sum.data;
  // Legacy supply-chain keys (still served for that ontology): headline tiles and time-series charts.
  const legacy = !!(s && s.shipments && s.ports && s.invoices);
  const tiles: { k: string; v: string; cls?: string; sub?: string }[] = legacy && s ? [
    { k: "Shipments in transit", v: String(s.shipments!.in_transit), sub: `${s.shipments!.planned} planned, ${s.shipments!.arrived} arrived` },
    { k: "Shipments delayed", v: String(s.shipments!.delayed), cls: s.shipments!.delayed ? "warn" : "", sub: `${Math.round(s.delay_hours_total ?? 0)} delay hours` },
    { k: "Ports closed", v: String(s.ports!.closed), cls: s.ports!.closed ? "bad" : "", sub: `${s.ports!.open} open` },
    { k: "Invoices flagged", v: String(s.invoices!.flagged), cls: s.invoices!.flagged ? "warn" : "", sub: `${s.invoices!.open} open, ${s.invoices!.paid} paid` },
    { k: "Value at risk", v: money(s.value_at_risk ?? 0), cls: s.value_at_risk ? "bad" : "", sub: "cargo via closed ports or delayed" },
    { k: "Open events", v: String(s.open_events ?? 0) },
  ] : [];
  const counts = Object.entries(s?.counts ?? {});
  const kpis = Object.entries(s?.kpis ?? {}).map(([k, v]) => ({ k, label: typeof v === "object" && v?.label ? v.label : humanize(k), v: num(v), sub: typeof v === "object" ? v?.description : undefined })).filter((x) => !Number.isNaN(x.v));
  const enums = Object.entries(s?.enums ?? {}).flatMap(([t, props]) => Object.entries(props).map(([p, vals]) => ({ t, p, vals: Object.entries(vals).sort((a, b) => b[1] - a[1]) }))).filter((e) => e.vals.length);
  const empty = !!s && !tiles.length && !counts.length && !kpis.length;
  return (
    <div className="pagebody">
      <ErrorBanner error={sum.error} title="Summary" onRetry={sum.reload} />
      {!s && !sum.error && <Spinner label="Loading summary" />}
      {empty && <EmptyState title="Nothing to summarise yet">This ontology has no objects or KPIs. Import data or add objects in the Explorer.</EmptyState>}
      {tiles.length > 0 && <div className="tiles" aria-label="Key figures">
        {tiles.map((t) => <div key={t.k} className={`tile ${t.cls ?? ""}`}><div className="k">{t.k}</div><div className="v">{t.v}</div><div className="s">{t.sub ?? " "}</div></div>)}
      </div>}
      {!legacy && kpis.length > 0 && <div className="tiles" aria-label="KPIs">
        {kpis.map((t) => <div key={t.k} className="tile" title={t.sub}><div className="k"><Trunc max={36}>{t.label}</Trunc></div><div className="v">{money(t.v)}</div><div className="s"><Trunc max={40}>{t.sub ?? " "}</Trunc></div></div>)}
      </div>}
      {counts.length > 0 && <section className="card" aria-label="Objects by type"><h3>Objects by type</h3>
        <div className="tiles">{counts.map(([t, n]) => <a key={t} className="tile linktile" href={`#/${t}`}><div className="k"><Trunc max={30}>{t}</Trunc></div><div className="v">{n}</div></a>)}</div></section>}
      {enums.length > 0 && <div className="enumgrid">{enums.map((e) => {
        const mx = Math.max(...e.vals.map((x) => x[1]), 1);
        return <section key={`${e.t}.${e.p}`} className="card"><h3><Trunc max={40}>{e.t}</Trunc> by {e.p}</h3>
          <table className="enumtable"><tbody>{e.vals.slice(0, 8).map(([val, n]) => <tr key={val}><td><Trunc max={24}>{val}</Trunc></td><td className="num">{n}</td><td className="barcell"><Bar value={n} max={mx} label={`${val}: ${n}`} /></td></tr>)}</tbody></table>
          {e.vals.length > 8 && <p className="muted small">and {e.vals.length - 8} more values</p>}</section>;
      })}</div>}
      <div className="dashgrid">
        {legacy && <div className="charts">
          {epoch && end && METRICS.map((m) => <MetricChart key={m.m} {...m} from={epoch} to={end} marker={marker!} live={live} poll={poll} asOfQ={asOfQ} />)}
        </div>}
        {!ev.error || ev.data ? <section className="card feed-card" style={legacy ? undefined : { gridColumn: "1 / -1" }}><h3>Events {live ? "(live)" : `(up to ${fmtT(asOf)})`}</h3>
          <ul className="feed">
            {(ev.data ?? []).map((e: EventRow) => <EventItem key={e.seq} e={e} />)}
            {ev.loading && !ev.data && <li><Spinner label="Loading events" /></li>}
            {ev.data && !ev.data.length && <li className="muted">No events recorded yet.</li>}
          </ul></section> : <ErrorBanner error={ev.error} title="Events" onRetry={ev.reload} />}
      </div>
    </div>
  );
}

function EventItem({ e }: { e: EventRow }) {
  const id = e.params?.id ?? e.params?.target ?? e.params?.shipment_id ?? e.params?.port_id;
  const type = (e.params?.type as string) ?? (e.params?.object_type as string);
  const ok = typeof id === "string" && !!type;
  const open = () => { if (ok) location.hash = `/${type}/${id}`; };
  return (
    <li className={ok ? "click" : ""} onClick={open} tabIndex={ok ? 0 : undefined} role={ok ? "link" : undefined} onKeyDown={(k) => k.key === "Enter" && open()}>
      <div><span className="t">{fmtT(e.sim_time)}</span> <span className="n">{e.name}</span> <ScnChip id={e.scenario_id} /></div><div>{e.summary}</div>
    </li>
  );
}

function MetricChart({ m, title, color, money: isMoney, from, to, marker, live, poll, asOfQ }: { m: Metric; title: string; color: string; money?: boolean; from: string; to: string; marker: string; live: boolean; poll?: number; asOfQ?: string }) {
  const toRef = useRef(to); toRef.current = to;
  const r = usePolled(() => api.series(m, from, toRef.current, 24), [m, from, asOfQ], poll);
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current); chart.current = c;
    const ro = new ResizeObserver(() => c.resize()); ro.observe(el.current);
    return () => { ro.disconnect(); c.dispose(); chart.current = null; };
  }, []);
  const pts = r.data?.points ?? [];
  const last = pts.length ? pts[pts.length - 1].v : null;
  useEffect(() => {
    chart.current?.setOption({
      animation: false, color: [color], textStyle: { color: "#a7b3b1" }, grid: { left: 46, right: 16, top: 14, bottom: 28 },
      tooltip: { trigger: "axis", backgroundColor: "#0c1a1c", borderColor: "#2a3736", textStyle: { color: "#fff" }, valueFormatter: (v: number) => (isMoney ? money(v) : String(v)) },
      xAxis: { type: "time", axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: { type: "value", minInterval: isMoney ? undefined : 1, axisLabel: { fontSize: 10, formatter: (v: number) => (isMoney ? money(v) : String(v)) }, splitLine: { lineStyle: { color: "#1a2a2c" } } },
      series: [{ type: "line", step: isMoney ? false : "end", showSymbol: false, lineStyle: { width: 2 }, areaStyle: { opacity: 0.15 }, data: pts.map((p) => [p.t, p.v]),
        markLine: { symbol: "none", silent: true, animation: false, lineStyle: { color: "#d0f94a", type: "dashed", width: 1.5 }, label: { formatter: live ? "now" : "as of", position: "insideEndTop", fontSize: 10 }, data: [{ xAxis: marker }] } }],
    }, true);
  }, [pts, color, isMoney, marker, live]);
  return (
    <section className="card" style={{ margin: 0 }}>
      <h3>{title}{last !== null && <span style={{ color, marginLeft: 8, fontSize: 15 }}>{isMoney ? money(last) : last}</span>}</h3>
      {r.error && <div className="err">{r.error}</div>}
      <div className="chart" ref={el} role="img" aria-label={`${title} over time`} />
    </section>
  );
}
