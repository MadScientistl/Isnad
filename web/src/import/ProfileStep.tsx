import { useState } from "react";
import { ingestApi, type ProfColumn, type ProfTable, type Profile } from "../api";
import { usePolled } from "../store";
import { Bar, EmptyState, ErrorBanner, Spinner, Trunc, pct } from "../ui";

const colFlags = (c: ProfColumn) => [c.unique && "unique", c.enum_candidate && `enum (${c.enum_candidate.length})`, c.geo && `geo: ${c.geo.role}`, c.time && "time", c.format && c.format].filter(Boolean) as string[];

function TableCard({ t, name }: { t: ProfTable; name: (id: string) => string }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="card">
      <h3><button className="linkbtn" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "▾" : "▸"} <Trunc max={50}>{t.name}</Trunc></button>
        <span className="muted"> {t.rows.toLocaleString()} rows{t.sampled_rows < t.rows ? `, ${t.sampled_rows.toLocaleString()} profiled` : ""}, {t.columns.length} columns</span>
        {t.is_join_table && <span className="chip info"> join table</span>}</h3>
      {t.key_candidates.length > 0 && <p className="small">Key candidates: {t.key_candidates.map((k, i) => <code key={i} className="ref">{k.map(name).join(" + ")}</code>)}</p>}
      {!t.key_candidates.length && <p className="small bad-t">No unique, non-null column found: objects cannot be keyed.</p>}
      {open && <div className="tablewrap"><table><thead><tr><th>Column</th><th>Type</th><th>Null</th><th>Distinct</th><th>Flags</th><th>Top values</th></tr></thead><tbody>
        {t.columns.map((c) => <tr key={c.id}><td><Trunc max={36}>{c.name}</Trunc></td><td>{c.type}</td>
          <td><span className="nullcell">{pct(c.null_rate, 0)}<Bar value={c.null_rate} tone={c.null_rate > 0.5 ? "warn" : undefined} label={`${pct(c.null_rate)} null`} /></span></td><td>{c.distinct.toLocaleString()}</td>
          <td>{colFlags(c).map((f) => <span key={f} className="chip">{f}</span>)}{c.notes.map((n, i) => <span key={i} className="chip warn" title={n}>note</span>)}</td>
          <td className="small">{c.top.slice(0, 3).map(([v, n], i) => <span key={i} className="topval"><Trunc max={24}>{v}</Trunc> <span className="muted">{n}</span></span>)}</td></tr>)}
      </tbody></table></div>}
    </section>
  );
}

export function ProfileView({ profile }: { profile: Profile }) {
  const colName = new Map<string, string>(); const tabName = new Map<string, string>();
  for (const t of profile.tables) { tabName.set(t.id, t.name); for (const c of t.columns) colName.set(c.id, c.name); }
  const label = (id: string) => `${tabName.get(id.split(".")[0]) ?? "?"}.${colName.get(id) ?? id}`;
  return (
    <>
      {profile.truncated && <div className="warnbox" role="status">The profile is truncated: caps on tables, columns or rows were reached, so only part of the source was profiled. Counts below describe the sample.</div>}
      {profile.warnings.map((w, i) => <div className="warnbox" key={i}><Trunc max={200}>{w}</Trunc></div>)}
      <p className="muted small">Source <b><Trunc max={50}>{profile.source.name}</Trunc></b> ({profile.source.kind}), checksum <code className="ref">{profile.source.sha256.slice(0, 12)}</code>, seed {profile.seed}.</p>
      {profile.tables.length === 0 && <EmptyState title="No tables found">The source contains nothing that can be profiled.</EmptyState>}
      {profile.tables.map((t) => <TableCard key={t.id} t={t} name={(id) => colName.get(id) ?? id} />)}
      <section className="card"><h3>Relationships ({profile.fks.length})</h3>
        {!profile.fks.length ? <EmptyState title="No relationships found">Tables will import as unconnected types. You can add links in the review step.</EmptyState> :
          <table><thead><tr><th>From</th><th>To</th><th>Kind</th><th>Confidence</th><th>Containment</th><th>Orphans</th></tr></thead><tbody>
            {profile.fks.map((f) => <tr key={f.id}><td><Trunc max={40}>{label(f.from)}</Trunc></td><td><Trunc max={40}>{label(f.to)}</Trunc></td>
              <td><span className={`chip ${f.declared ? "ok" : ""}`}>{f.declared ? "declared" : "inferred"}</span> <span className="small muted">{f.cardinality}</span></td>
              <td><span className="nullcell">{pct(f.confidence, 0)}<Bar value={f.confidence} tone={f.confidence < 0.6 ? "bad" : f.confidence < 0.9 ? "warn" : "ok"} label={`confidence ${pct(f.confidence)}`} /></span></td>
              <td>{pct(f.containment)}</td><td className={f.orphans ? "bad-t" : ""}>{f.orphans.toLocaleString()}</td></tr>)}</tbody></table>}
      </section>
    </>
  );
}

export default function ProfileStep({ sid, onNext }: { sid: string; onNext: () => void }) {
  const s = usePolled(() => ingestApi.session(sid), [sid]);
  return (
    <div>
      {s.loading && !s.data && <Spinner label="Loading profile" />}
      <ErrorBanner error={s.error} onRetry={s.reload} title="Profile" />
      {s.data?.profile && <ProfileView profile={s.data.profile} />}
      {s.data && !s.data.profile && <EmptyState title="No profile yet">The server has not finished profiling this source.</EmptyState>}
      <p className="stepfoot"><button className="primary" disabled={!s.data?.profile?.tables.length} onClick={onNext}>Next: propose an ontology</button></p>
    </div>
  );
}
