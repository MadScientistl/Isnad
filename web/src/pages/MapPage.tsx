import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { ArcLayer, IconLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api, client, type GeoFeature } from "../api";
import { useAsOf, usePolled } from "../store";

type RGBA = [number, number, number, number];
const ATLAS = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="256" height="64" viewBox="0 0 256 64" fill="#fff">
<circle cx="32" cy="32" r="26"/><rect x="72" y="8" width="48" height="48" rx="3"/><polygon points="160,4 188,32 160,60 132,32"/><polygon points="224,6 252,56 196,56"/></svg>`);
const MAPPING = {
  circle: { x: 0, y: 0, width: 64, height: 64, mask: true }, square: { x: 64, y: 0, width: 64, height: 64, mask: true },
  diamond: { x: 128, y: 0, width: 64, height: 64, mask: true }, tri: { x: 192, y: 0, width: 64, height: 64, mask: true },
};
const ICON = { iconAtlas: ATLAS, iconMapping: MAPPING };
// Okabe-Ito based, colourblind-safe; status is also encoded by shape.
const COL: Record<string, RGBA> = {
  open: [0, 158, 115, 255], closed: [213, 94, 0, 255], warehouse: [0, 114, 178, 255],
  in_transit: [31, 42, 68, 255], delayed: [230, 159, 0, 255], planned: [138, 148, 166, 255], arrived: [86, 180, 233, 255],
};
const OSM_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "&copy; OpenStreetMap contributors" } },
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#dfe6ee" } }, { id: "osm", type: "raster", source: "osm" }],
};

interface Pt { id: string; type: string; name: string; pos: [number, number]; status: string; origin?: [number, number] | null; dest?: [number, number] | null; props: GeoFeature["properties"] }
const toPts = (fc?: { features: GeoFeature[] }): Pt[] => (fc?.features ?? []).filter((f) => f.geometry).map((f) => ({
  id: f.id ?? f.properties.id, type: f.properties.type, name: String(f.properties.name ?? f.properties.ref ?? f.properties.id.slice(0, 6)),
  pos: f.geometry.coordinates, status: String(f.properties.status ?? "open"), origin: f.properties.origin, dest: f.properties.destination, props: f.properties,
}));

export default function MapPage() {
  const { asOfQ, live } = useAsOf();
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlay = useRef<MapboxOverlay | null>(null);
  const [showArrived, setShowArrived] = useState(false);
  const [arcs, setArcs] = useState<"selected" | "all" | "off">("selected");
  const [sel, setSel] = useState<Pt | null>(null);
  const [pulse, setPulse] = useState(0);
  const [tilesFailed, setTilesFailed] = useState(false);
  const poll = live ? 2500 : undefined;
  const ports = usePolled(() => api.geo("Port", asOfQ), [asOfQ], poll);
  const whs = usePolled(() => api.geo("Warehouse", asOfQ), [asOfQ], poll);
  const ships = usePolled(() => api.geo("Shipment", asOfQ), [asOfQ], poll);
  const error = ports.error ?? whs.error ?? ships.error;

  useEffect(() => {
    if (!el.current) return;
    const map = new maplibregl.Map({ container: el.current, style: OSM_STYLE, center: [30, 25], zoom: 2, attributionControl: { compact: true } });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    const ov = new MapboxOverlay({ layers: [] });
    map.addControl(ov as unknown as maplibregl.IControl);
    map.on("error", (e: any) => { if (e?.sourceId === "osm") setTilesFailed(true); });
    map.on("data", (e: any) => { if (e?.sourceId === "osm" && e.tile) setTilesFailed(false); });
    mapRef.current = map; overlay.current = ov;
    return () => { map.remove(); mapRef.current = null; overlay.current = null; };
  }, []);

  const hasClosed = (ports.data?.features ?? []).some((f) => f.properties.status === "closed");
  useEffect(() => {
    if (!hasClosed) return;
    const h = setInterval(() => setPulse((p) => (p + 0.05) % 1), 70);
    return () => clearInterval(h);
  }, [hasClosed]);

  const P = useMemo(() => toPts(ports.data), [ports.data]);
  const W = useMemo(() => toPts(whs.data), [whs.data]);
  const S = useMemo(() => toPts(ships.data).filter((s) => showArrived || s.status !== "arrived"), [ships.data, showArrived]);

  useEffect(() => {
    const ov = overlay.current; if (!ov) return;
    const arcData = arcs === "off" ? [] : S.filter((s) => s.origin && s.dest && (arcs === "all" || (sel && sel.id === s.id)));
    const closed = P.filter((p) => p.status === "closed");
    ov.setProps({
      layers: [
        new ArcLayer<Pt>({ id: "arcs", data: arcData, getSourcePosition: (d) => d.origin!, getTargetPosition: (d) => d.dest!, getWidth: (d) => (sel?.id === d.id ? 3 : 1.5),
          getSourceColor: (d) => (COL[d.status] ?? COL.planned).map((v, i) => (i === 3 ? 160 : v)) as RGBA, getTargetColor: (d) => (COL[d.status] ?? COL.planned).map((v, i) => (i === 3 ? 160 : v)) as RGBA, updateTriggers: { getWidth: sel?.id } }),
        new ScatterplotLayer<Pt>({ id: "pulse", data: closed, getPosition: (d) => d.pos, stroked: true, filled: false, radiusUnits: "pixels", lineWidthUnits: "pixels", lineWidthMinPixels: 2,
          getRadius: 14 + pulse * 16, getLineColor: [213, 94, 0, Math.round((1 - pulse) * 220)], updateTriggers: { getRadius: pulse, getLineColor: pulse } }),
        new IconLayer<Pt>({ id: "ports", data: P, pickable: true, getPosition: (d) => d.pos, sizeUnits: "pixels", getSize: (d) => (d.status === "closed" ? 24 : 20),
          ...ICON, getIcon: (d) => (d.status === "closed" ? "diamond" : "circle"), getColor: (d) => (d.status === "closed" ? COL.closed : COL.open),
          onClick: (i) => i.object && setSel(i.object as Pt) }),
        new IconLayer<Pt>({ id: "warehouses", data: W, pickable: true, getPosition: (d) => d.pos, sizeUnits: "pixels", getSize: 16,
          ...ICON, getIcon: () => "square", getColor: COL.warehouse, onClick: (i) => i.object && setSel(i.object as Pt) }),
        new IconLayer<Pt>({ id: "ships", data: S, pickable: true, getPosition: (d) => d.pos, sizeUnits: "pixels", getSize: (d) => (sel?.id === d.id ? 20 : 14),
          ...ICON, getIcon: () => "tri", getColor: (d) => COL[d.status] ?? COL.planned, updateTriggers: { getSize: sel?.id },
          onClick: (i) => i.object && setSel(i.object as Pt) }),
        new TextLayer<Pt>({ id: "labels", data: [...P, ...W], getPosition: (d) => d.pos, getText: (d) => d.name + (d.status === "closed" ? " (closed)" : ""), getSize: 11, getPixelOffset: [0, 16],
          getColor: [28, 32, 36, 255], outlineWidth: 3, outlineColor: [255, 255, 255, 230], fontSettings: { sdf: true }, getTextAnchor: "middle", getAlignmentBaseline: "top" }),
      ],
    });
  }, [P, W, S, arcs, sel, pulse]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const s of toPts(ships.data)) c[s.status] = (c[s.status] ?? 0) + 1;
    return c;
  }, [ships.data]);

  return (
    <div className="mappage">
      <div className="mapcanvas" ref={el} aria-label="Map of ports, warehouses and shipments" />
      <div className="mapctl">
        <label><input type="checkbox" checked={showArrived} onChange={(e) => setShowArrived(e.target.checked)} /> Show arrived shipments</label>
        <label>Routes <select value={arcs} onChange={(e) => setArcs(e.target.value as any)}><option value="selected">Selected shipment</option><option value="all">All shipments</option><option value="off">Hidden</option></select></label>
        <span className="muted">{live ? "Live, refreshing every 2.5 s" : "Historical view"}</span>
        {error && <span className="err" style={{ margin: 0 }}>{error}</span>}
        {tilesFailed && <span className="warnbox" style={{ margin: 0 }}>Basemap tiles unavailable (offline?). Showing plain background.</span>}
      </div>
      <div className="maplegend" aria-label="Legend">
        <b>Legend</b>
        <span style={{ color: "#009E73" }}><i className="sw circle" /><span className="ink">Port, open</span></span>
        <span style={{ color: "#D55E00" }}><i className="sw diamond" /><span className="ink">Port, closed (pulsing ring)</span></span>
        <span style={{ color: "#0072B2" }}><i className="sw square" /><span className="ink">Warehouse</span></span>
        <span style={{ color: "#1f2a44" }}><i className="sw tri" /><span className="ink">Shipment in transit ({counts.in_transit ?? 0})</span></span>
        <span style={{ color: "#E69F00" }}><i className="sw tri" /><span className="ink">Shipment delayed ({counts.delayed ?? 0})</span></span>
        <span style={{ color: "#8a94a6" }}><i className="sw tri" /><span className="ink">Shipment planned ({counts.planned ?? 0})</span></span>
        {showArrived && <span style={{ color: "#56B4E9" }}><i className="sw tri" /><span className="ink">Shipment arrived ({counts.arrived ?? 0})</span></span>}
      </div>
      {sel && <Panel sel={sel} asOf={asOfQ} onClose={() => setSel(null)} />}
    </div>
  );
}

function Panel({ sel, asOf, onClose }: { sel: Pt; asOf?: string; onClose: () => void }) {
  const o = usePolled(() => client.get(sel.type as any, sel.id, { asOfValid: asOf }) as Promise<Record<string, unknown>>, [sel.id, asOf]);
  return (
    <aside className="sidepanel" aria-label="Selected object">
      <button style={{ float: "right" }} onClick={onClose} aria-label="Close panel">Close</button>
      <h3>{sel.type}: {sel.name}</h3>
      {o.error && <div className="err">{o.error}</div>}
      {o.loading && !o.data && <div className="muted">Loading...</div>}
      {o.data && <table><tbody>{Object.entries(o.data).map(([k, v]) => <tr key={k}><th>{k}</th><td>{v === null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table>}
      <p><a className="lnk" href={`#/${sel.type}/${sel.id}`}>Open 360 view</a> | <a className="lnk" href="#/graph" onClick={() => { try { sessionStorage.setItem("graphCenter", JSON.stringify({ type: sel.type, id: sel.id })); } catch { /* ignore */ } }}>Show in graph</a></p>
    </aside>
  );
}
