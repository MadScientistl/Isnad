import type { Impact, Kpis } from "./api";
import { KPI_META, deltaCls, fmtDelta, fmtKpi } from "./scenarios";

/** Small chip shown on rows (audit, events, proposals) that carry a scenario_id > 0. */
export const ScnChip = ({ id }: { id?: number | null }) =>
  id ? <a className="chip scn" href="#/scenarios" title={`Recorded inside scenario ${id}`} onClick={(e) => e.stopPropagation()}>scenario {id}</a> : null;

/** Before / after / delta KPI table (proposal dry_run.impact). Deltas are coloured good or bad. */
export function ImpactTable({ impact }: { impact: Impact }) {
  const b = impact.before?.kpis ?? ({} as Kpis), a = impact.after?.kpis ?? ({} as Kpis), d = impact.delta?.kpis ?? ({} as Kpis);
  return (
    <table className="diff impact"><thead><tr><th>KPI</th><th>Before (main)</th><th>After</th><th>Delta</th></tr></thead><tbody>
      {KPI_META.filter((m) => m.k in b || m.k in a).map((m) => {
        const dv = d[m.k] ?? (a[m.k] ?? 0) - (b[m.k] ?? 0);
        return <tr key={m.k}><td>{m.label}</td><td>{fmtKpi(b[m.k], m.fmt)}</td><td>{fmtKpi(a[m.k], m.fmt)}</td><td className={`delta ${deltaCls(dv, m.good)}`}>{fmtDelta(dv, m.fmt)}</td></tr>;
      })}</tbody></table>
  );
}
