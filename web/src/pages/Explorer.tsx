import { useEffect, useMemo, useRef, useState } from "react";
import { EmptyState, ErrorBanner, Spinner } from "../ui";
import cytoscape from "cytoscape";
import { type TypesResponse, type ObjectTypeName, type Neighbour } from "@sdk/client";
import { client, isNotFound } from "../api";
import { useAsOf, useApp } from "../store";
import { ActionsPanel, Restricted } from "../Actions";

type Obj = Record<string, any>;
function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [s, set] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true; set((p) => ({ data: p.data, loading: true }));
    fn().then((data) => live && set({ data, loading: false }), (e) => live && set({ error: String(e.message ?? e), loading: false }));
    return () => { live = false; };
    // eslint-disable-next-line
  }, [...deps, n]);
  return { ...s, reload: () => setN((x) => x + 1) };
}
const label = (o: Obj | null | undefined, id: string) => (o && (o.name || o.ref)) || id.slice(0, 8);
const Err = ({ e, retry }: { e?: string; retry?: () => void }) => <ErrorBanner error={e} onRetry={retry} />;
const fmt = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const rng = (a: string, b: string | null) => `${a.slice(0, 19)} → ${b && !b.startsWith("infinity") && !b.startsWith("9999") ? b.slice(0, 19) : "∞"}`;
const go = (h: string) => { location.hash = "/" + h; };

export default function ExplorerPage({ parts }: { parts: string[] }) {
  const { asOfQ: asOfIso, role } = useAsOf();
  const [type, id] = parts as [ObjectTypeName | undefined, string | undefined];
  const types = useAsync<TypesResponse>(() => client.types(), [role]);
  const counts = useAsync(async () => {
    const r: Record<string, number> = {};
    for (const t of types.data?.objectTypes ?? []) r[t.name] = (await client.list(t.name as ObjectTypeName, { limit: 1000, asOfValid: asOfIso })).length;
    return r;
  }, [types.data, asOfIso, role]);
  const tdef = types.data?.objectTypes.find((t) => t.name === type);
  return (
    <div className="layout">
      <nav className="side" aria-label="Object types">
        {types.loading && !types.data && <Spinner label="Loading types" />}
        {types.data && !types.data.objectTypes.length && <EmptyState title="No object types">This ontology defines no object types yet.</EmptyState>}
        {types.data?.objectTypes.map((t) => (
          <a key={t.name} href={`#/${t.name}`} className={t.name === type ? "on" : ""}>{t.name}<span className="muted">{counts.data?.[t.name] ?? "..."}</span></a>
        ))}
      </nav>
      <main className="main">
        <Err e={types.error} retry={types.reload} />
        {!type && types.data?.objectTypes.length ? <p className="muted">Select an object type.</p> : null}
        {type && tdef && !id && <Explorer key={type + role + asOfIso} type={type} tdef={tdef} asOf={asOfIso} go={go} />}
        {type && tdef && id && <Object360 key={type + id + role + asOfIso} type={type} id={id} tdef={tdef} asOf={asOfIso} go={go} types={types.data!} onChanged={counts.reload} />}
      </main>
    </div>
  );
}

const PAGE = 15;
function Explorer({ type, tdef, asOf, go }: { type: ObjectTypeName; tdef: TypesResponse["objectTypes"][0]; asOf?: string; go: (h: string) => void }) {
  const { me } = useApp();
  const masked = me?.types[type]?.masked ?? [];
  const [page, setPage] = useState(0);
  const r = useAsync(() => client.list(type, { limit: PAGE + 1, offset: page * PAGE, asOfValid: asOf }), [page]);
  const rows = (r.data ?? []) as Obj[];
  return (
    <div>
      <h2>{type}</h2><Err e={r.error} retry={r.reload} />
      {r.loading && !r.data && <Spinner label="Loading objects" />}
      <table><thead><tr>{tdef.properties.map((p) => <th key={p.name}>{p.name}</th>)}</tr></thead>
        <tbody>{rows.slice(0, PAGE).map((o) => (
          <tr key={o.id} className="row" onClick={() => go(`${type}/${o.id}`)}>{tdef.properties.map((p) => <td key={p.name}>{masked.includes(p.name) && o[p.name] == null ? <Restricted /> : fmt(o[p.name])}</td>)}</tr>))}
          {!r.loading && !r.error && !rows.length && <tr><td colSpan={99}><EmptyState title={`No ${type} objects`}>Nothing at the selected time, or your role cannot see any.</EmptyState></td></tr>}</tbody></table>
      <p><button disabled={page === 0} onClick={() => setPage(page - 1)}>Prev</button> Page {page + 1} <button disabled={rows.length <= PAGE} onClick={() => setPage(page + 1)}>Next</button></p>
    </div>
  );
}

function Object360({ type, id, tdef, asOf, go, types, onChanged }: { type: ObjectTypeName; id: string; tdef: TypesResponse["objectTypes"][0]; asOf?: string; go: (h: string) => void; types: TypesResponse; onChanged: () => void }) {
  const { me } = useApp();
  const masked = me?.types[type]?.masked ?? [];
  const obj = useAsync(() => client.get(type, id, { asOfValid: asOf }), []);
  const hist = useAsync(() => client.history(type, id), []);
  const nb = useAsync(() => client.neighbours(type, id, asOf), []);
  const [edit, setEdit] = useState<Obj | null>(null);
  const [msg, setMsg] = useState<string>();
  const o = obj.data as Obj | undefined;
  const groups = useMemo(() => {
    const g: Record<string, Neighbour[]> = {};
    for (const n of nb.data ?? []) (g[`${n.direction === "out" ? "→" : "←"} ${n.link}`] ??= []).push(n);
    return g;
  }, [nb.data]);
  const save = async () => {
    if (!edit) return;
    try {
      const props: Obj = {};
      for (const p of tdef.properties) { const v = edit[p.name]; if (v === "" || v === undefined || v === null) continue; props[p.name] = p.type === "int" || p.type === "float" ? Number(v) : v; }
      await client.replace(type, id, props as never); setEdit(null); setMsg(undefined); obj.reload(); hist.reload(); onChanged();
    } catch (e: any) { setMsg(e.message); }
  };
  const del = async () => {
    if (!confirm("Delete this object?")) return;
    try { await client.remove(type, id); onChanged(); go(type); } catch (e: any) { setMsg(e.message); }
  };
  if (obj.error && isNotFound(obj.error)) return <div><p><a className="lnk" href={`#/${type}`}>← {type}</a></p><h2>Not found</h2><p className="muted">This object does not exist.</p></div>;
  const refreshAll = () => { obj.reload(); hist.reload(); nb.reload(); onChanged(); };
  return (
    <div>
      <p><a className="lnk" href={`#/${type}`}>← {type}</a></p>
      <h2>{type}: {label(o, id)}</h2>
      <Err e={obj.error ?? msg} />
      {obj.loading && !o && <div className="muted">Loading…</div>}
      <div className="grid">
        <div>
          <div className="card"><h3>Properties</h3>
            {o && !edit && <table><tbody>
              <tr><th>id</th><td>{o.id}</td></tr>
              {tdef.properties.map((p) => <tr key={p.name}><th>{p.name}</th><td>{masked.includes(p.name) && o[p.name] == null ? <Restricted /> : fmt(o[p.name])}</td></tr>)}
              <tr><th>changed_by</th><td>{o.changed_by}</td></tr></tbody></table>}
            {o && edit && <div className="form">{tdef.properties.map((p) => (
              <label key={p.name}><span>{p.name}{p.required ? "*" : ""}</span>
                {p.enum.length ? <select value={edit[p.name] ?? ""} onChange={(e) => setEdit({ ...edit, [p.name]: e.target.value })}><option value="" />{p.enum.map((v) => <option key={v}>{v}</option>)}</select>
                  : <input value={edit[p.name] ?? ""} onChange={(e) => setEdit({ ...edit, [p.name]: e.target.value })} />}</label>))}
              <button onClick={save}>Save</button> <button onClick={() => setEdit(null)}>Cancel</button></div>}
            {o && !edit && me?.types[type]?.write !== false && <p><button onClick={() => setEdit({ ...o })}>Edit</button> <button className="danger" onClick={del}>Delete</button></p>}
          </div>
          {o && <ActionsPanel type={type} id={id} onDone={refreshAll} />}
          <div className="card"><h3>Links</h3><Err e={nb.error} />{nb.loading && <div className="muted">Loading…</div>}
            {Object.entries(groups).map(([g, ns]) => <div key={g}><b>{g}</b><ul>{ns.map((n) => (
              <li key={n.link + n.direction + n.id}><a className="lnk" href={`#/${n.type}/${n.id}`}>{n.type} {label(n.object as Obj, n.id)}</a></li>))}</ul></div>)}
            {!nb.loading && !nb.data?.length && <span className="muted">No links.</span>}
          </div>
          <div className="card"><h3>History (valid / recorded)</h3><Err e={hist.error} />{hist.loading && <div className="muted">Loading…</div>}
            <div className="tl">{[...(hist.data ?? [])].reverse().map((h: Obj, i) => (
              <div key={i}><b>{label(h, id)}{h.status ? ` · ${h.status}` : ""}</b> by {h.changed_by}<br />valid {rng(h.valid_from, h.valid_to)}<br />recorded {rng(h.recorded_from, h.recorded_to)}</div>))}</div>
          </div>
        </div>
        <div className="card"><h3>Neighbourhood</h3>
          <Graph centre={{ type, id, name: label(o, id) }} data={nb.data} go={go} />
          <div className="legend"><span><i />asserted</span><span className="muted"><i className="dash" />inferred edges: see the Graph page</span></div>
        </div>
      </div>
    </div>
  );
}

function Graph({ centre, data, go }: { centre: { type: string; id: string; name: string }; data?: Neighbour[]; go: (h: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const goRef = useRef(go); goRef.current = go;
  useEffect(() => {
    if (!ref.current || !data) return;
    const els: cytoscape.ElementDefinition[] = [{ data: { id: centre.id, label: centre.name, type: centre.type, centre: 1 } }];
    const seen = new Set([centre.id]);
    for (const n of data) {
      if (!seen.has(n.id)) { seen.add(n.id); els.push({ data: { id: n.id, label: label(n.object as Obj, n.id), type: n.type } }); }
      const [s, t] = n.direction === "out" ? [centre.id, n.id] : [n.id, centre.id];
      els.push({ data: { id: `${s}-${n.link}-${t}`, source: s, target: t, label: n.link } });
    }
    const cy = cytoscape({ container: ref.current, elements: els, layout: { name: "concentric", minNodeSpacing: 40 } as any,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, "background-color": "#6366f1", color: "#1c2024", "text-valign": "bottom", "text-margin-y": 3 } },
        { selector: "node[centre]", style: { "background-color": "#0f172a", width: 36, height: 36 } },
        { selector: "edge", style: { label: "data(label)", "font-size": 8, width: 1.5, "line-style": "solid", "line-color": "#475569", "target-arrow-shape": "triangle", "target-arrow-color": "#475569", "curve-style": "bezier" } },
      ] });
    cy.on("tap", "node", (e) => { const d = e.target.data(); if (!d.centre) goRef.current(`${d.type}/${d.id}`); });
    return () => cy.destroy();
  }, [data, centre.id, centre.name]);
  return <div ref={ref} style={{ height: 380, border: "1px solid #e2e5ea", borderRadius: 6 }} />;
}
