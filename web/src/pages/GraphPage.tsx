import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import { api, client } from "../api";
import { useAsOf, usePolled, fmtT } from "../store";

const TYPES = ["Shipment", "Port", "Warehouse", "Invoice", "Supplier", "Carrier", "Event", "PurchaseOrder"];
const TCOL: Record<string, string> = {
  Shipment: "#0072B2", Port: "#009E73", Warehouse: "#56B4E9", Invoice: "#CC79A7", Supplier: "#8c6d1f", Carrier: "#4b5563", Event: "#E69F00", PurchaseOrder: "#6b7280",
};
const INFERRED = "#7b3fa0";
const nm = (o: any, id: string) => (o && (o.name || o.ref || o.kind)) || id.slice(0, 8);
const emph = (s: string | null) => s === "closed" || s === "delayed" || s === "flagged";

export default function GraphPage() {
  const { asOfQ, asOf, live } = useAsOf();
  const saved = (() => { try { return JSON.parse(sessionStorage.getItem("graphCenter") ?? "null"); } catch { return null; } })();
  const [type, setType] = useState<string>(saved?.type ?? "Shipment");
  const [centre, setCentre] = useState<string>(saved?.id ?? "");
  const [filter, setFilter] = useState("");
  const [depth, setDepth] = useState(2);
  const [inferred, setInferred] = useState(true);
  const list = usePolled(async () => ({ t: type, rows: (await client.list(type as any, { limit: 300, asOfValid: asOfQ })) as any[] }), [type, asOfQ]);
  const rows = list.data?.t === type ? list.data.rows : undefined;
  useEffect(() => { if (!centre && rows?.length) setCentre(rows[0].id); }, [rows, centre]);
  const g = usePolled(() => (centre ? api.graph(centre, depth, asOfQ, inferred) : Promise.resolve(null)), [centre, depth, asOfQ, inferred]);
  const opts = useMemo(() => (rows ?? []).filter((o) => nm(o, o.id).toLowerCase().includes(filter.toLowerCase())), [rows, filter]);
  const ref = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const recentre = (t: string, id: string) => { setType(t); setCentre(id); setFilter(""); try { sessionStorage.setItem("graphCenter", JSON.stringify({ type: t, id })); } catch { /* ignore */ } };
  const recRef = useRef(recentre); recRef.current = recentre;

  useEffect(() => {
    const d = g.data; if (!ref.current || !d) return;
    const els: cytoscape.ElementDefinition[] = [
      ...d.nodes.map((n) => ({ data: { id: n.id, label: n.label + (emph(n.status) ? ` [${n.status}]` : ""), type: n.type, emph: emph(n.status) ? 1 : 0, status: n.status ?? "", centre: n.id === d.center ? 1 : 0, color: TCOL[n.type] ?? "#6366f1" } })),
      ...d.edges.map((e, i) => ({ data: { id: `e${i}`, source: e.src, target: e.dst, label: e.link + (e.inferred ? " (inferred)" : ""), inferred: e.inferred ? 1 : 0 } })),
    ];
    const cy = cytoscape({ container: ref.current, elements: els, layout: { name: "cose", animate: false, nodeRepulsion: () => 40000, idealEdgeLength: () => 130, nodeOverlap: 30, padding: 30 } as any,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, "background-color": "data(color)", color: "#1c2024", "text-valign": "bottom", "text-margin-y": 3, "border-width": 1, "border-color": "#fff", "text-background-color": "#fff", "text-background-opacity": 0.75, "text-background-padding": "1px" } },
        { selector: "node[type = 'Warehouse']", style: { shape: "round-rectangle" } },
        { selector: "node[type = 'Port']", style: { shape: "ellipse" } },
        { selector: "node[type = 'Shipment']", style: { shape: "triangle" } },
        { selector: "node[type = 'Invoice']", style: { shape: "rectangle" } },
        { selector: "node[type = 'Event']", style: { shape: "star" } },
        { selector: "node[emph = 1]", style: { "border-width": 4, "border-color": "#D55E00", width: 30, height: 30, "font-weight": "bold" } },
        { selector: "node[status = 'delayed']", style: { "border-color": "#E69F00" } },
        { selector: "node[status = 'flagged']", style: { "border-color": "#CC1F8A" } },
        { selector: "node[centre = 1]", style: { width: 38, height: 38, "border-width": 4, "border-color": "#0f172a" } },
        { selector: "edge", style: { label: "data(label)", "font-size": 8, color: "#334155", width: 1.6, "line-style": "solid", "line-color": "#334155", "target-arrow-shape": "triangle", "target-arrow-color": "#334155", "curve-style": "bezier", "text-background-color": "#fff", "text-background-opacity": 0.8, "text-background-padding": "1px" } },
        { selector: "edge[inferred = 1]", style: { "line-style": "dashed", "line-dash-pattern": [6, 4], "line-color": INFERRED, "target-arrow-color": INFERRED, color: INFERRED, width: 2 } },
      ] });
    cy.on("tap", "node", (e) => { const d2 = e.target.data(); if (!d2.centre) recRef.current(d2.type, d2.id); });
    cyRef.current = cy;
    return () => { cy.destroy(); cyRef.current = null; };
  }, [g.data]);

  const nInf = g.data?.edges.filter((e) => e.inferred).length ?? 0;
  const wantInferredButNone = inferred && g.data && !g.data.inferred_available;
  return (
    <div className="pagebody">
      <div className="toolbar">
        <label>Type <select value={type} onChange={(e) => { setType(e.target.value); setCentre(""); }}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select></label>
        <label>Search <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter by name" /></label>
        <label>Centre object <select value={centre} onChange={(e) => recentre(type, e.target.value)} style={{ minWidth: 200 }}>
          {centre && !opts.some((o) => o.id === centre) && <option value={centre}>{g.data?.nodes.find((n) => n.id === centre)?.label ?? centre.slice(0, 8)}</option>}
          {opts.map((o) => <option key={o.id} value={o.id}>{nm(o, o.id)}</option>)}</select></label>
        <label>Depth <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>{[1, 2, 3].map((d) => <option key={d}>{d}</option>)}</select></label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: 6, color: "var(--ink)" }}><input type="checkbox" checked={inferred} onChange={(e) => setInferred(e.target.checked)} /> Show inferred edges</label>
        <span className="muted">{live ? "Live state" : `As of ${fmtT(asOf)}`}</span>
      </div>
      {g.error && <div className="err">{g.error}</div>}
      {g.data?.truncated && <div className="warnbox">Result truncated at 300 nodes. Reduce the depth or pick a more specific centre.</div>}
      {wantInferredButNone && <div className="warnbox">Inferred edges exist for the live state only. Switch to Live to see them.</div>}
      <div className="cy" ref={ref} role="img" aria-label="Graph neighbourhood" data-testid="graph" />
      {g.loading && !g.data && <div className="muted">Loading...</div>}
      <div className="legend">
        <span><i />asserted edge</span>
        <span><i className="dash" />inferred (reasoner, current state only){g.data ? `: ${nInf} shown` : ""}</span>
        {Object.entries(TCOL).slice(0, 7).map(([t, c]) => <span key={t}><i className="sw circle" style={{ background: c }} />{t}</span>)}
        <span><i className="sw circle" style={{ background: "#fff", border: "3px solid #D55E00" }} />closed / delayed / flagged (thick border, status in label)</span>
        <span className="muted">Click a node to re-centre.</span>
      </div>
    </div>
  );
}
