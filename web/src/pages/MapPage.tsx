import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { ArcLayer, GeoJsonLayer, IconLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api, client, type GeoCollection, type GeoFeature } from "../api";
import { useApp, useAsOf, usePolled, useTypeStyles } from "../store";
import { ALERT_STATUS, hexToRgba } from "../palette";
import { EmptyState, ErrorBanner, Spinner, Trunc } from "../ui";

type RGBA = [number, number, number, number];
const ATLAS = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="256" height="64" viewBox="0 0 256 64" fill="#fff">
<circle cx="32" cy="32" r="26"/><rect x="72" y="8" width="48" height="48" rx="3"/><polygon points="160,4 188,32 160,60 132,32"/><polygon points="224,6 252,56 196,56"/></svg>`);
const MAPPING = {
  circle: { x: 0, y: 0, width: 64, height: 64, mask: true }, square: { x: 64, y: 0, width: 64, height: 64, mask: true },
  diamond: { x: 128, y: 0, width: 64, height: 64, mask: true }, tri: { x: 192, y: 0, width: 64, height: 64, mask: true },
};
const ICON = { iconAtlas: ATLAS, iconMapping: MAPPING };
const ALERT: RGBA = [213, 94, 0, 255];
const DONE_STATUS = new Set(["arrived", "completed", "done", "delivered"]);
const OSM_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: { osm: { type: "raster", tiles: ["https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "&copy; OpenStreetMap contributors &copy; CARTO" } },
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#050d10" } }, { id: "osm", type: "raster", source: "osm" }],
};

interface Pt { id: string; type: string; name: string; pos: [number, number]; status: string; origin?: [number, number] | null; dest?: [number, number] | null; props: GeoFeature["properties"] }
const toPts = (fc?: { features: GeoFeature[] }): Pt[] => (fc?.features ?? []).filter((f) => f.geometry && f.geometry.type === "Point").map((f) => ({
  id: f.id ?? f.properties.id, type: f.properties.type, name: String(f.properties.name ?? f.properties.ref ?? f.properties.id.slice(0, 6)),
  pos: f.geometry.coordinates as [number, number], status: String(f.properties.status ?? ""), origin: f.properties.origin, dest: f.properties.destination, props: f.properties,
}));

export default function MapPage() { return <MapView />; }

/** The map. Layers come from the runtime type list (types with a geometry property). `scn` forces main ("main") or a scenario id for this map's own requests; `changed` ids get a highlight ring (compare view). */
export function MapView({ scn, changed, embedded, title }: { scn?: number | "main"; changed?: Set<string>; embedded?: boolean; title?: string } = {}) {
  const { asOfQ, live: liveAsOf } = useAsOf();
  const { types, typesError, reloadTypes } = useApp();
  const styles = useTypeStyles();
  const live = liveAsOf && !embedded;
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlay = useRef<MapboxOverlay | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [arcs, setArcs] = useState<"selected" | "all" | "off">("selected");
  const [sel, setSel] = useState<Pt | null>(null);
  const [pulse, setPulse] = useState(0);
  const [tilesFailed, setTilesFailed] = useState(false);
  const poll = live ? 2500 : undefined;
  const geoTypes = useMemo(() => (types?.objectTypes ?? []).filter((t) => t.geo).map((t) => t.name), [types]);
  const geoKey = geoTypes.join("|");
  const data = usePolled(async () => {
    const out: Record<string, GeoCollection> = {};
    await Promise.all(geoTypes.map(async (t) => { out[t] = await api.geo(t, asOfQ, scn); }));
    return out;
  }, [asOfQ, geoKey, scn], geoTypes.length ? poll : undefined);
  const error = typesError ?? data.error;

  useEffect(() => {
    if (!el.current) return;
    const map = new maplibregl.Map({ container: el.current, style: OSM_STYLE, center: [30, 20], zoom: embedded ? 0.3 : 2, attributionControl: { compact: true } });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    const ov = new MapboxOverlay({ layers: [] });
    map.addControl(ov as unknown as maplibregl.IControl);
    map.on("error", (e: any) => { if (e?.sourceId === "osm") setTilesFailed(true); });
    map.on("data", (e: any) => { if (e?.sourceId === "osm" && e.tile) setTilesFailed(false); });
    mapRef.current = map; overlay.current = ov;
    return () => { map.remove(); mapRef.current = null; overlay.current = null; };
  }, []);

  const byType = useMemo(() => {
    const m: Record<string, Pt[]> = {};
    for (const t of geoTypes) m[t] = toPts(data.data?.[t]);
    return m;
  }, [data.data, geoKey]);
  const shapes = useMemo(() => {
    const m: Record<string, GeoFeature[]> = {};
    for (const t of geoTypes) m[t] = (data.data?.[t]?.features ?? []).filter((f) => f.geometry && f.geometry.type !== "Point");
    return m;
  }, [data.data, geoKey]);
  const visible = useMemo(() => {
    const m: Record<string, Pt[]> = {};
    for (const t of geoTypes) m[t] = hiddenTypes.has(t) ? [] : (byType[t] ?? []).filter((p) => showDone || !DONE_STATUS.has(p.status));
    return m;
  }, [byType, hiddenTypes, showDone, geoKey]);
  const all = useMemo(() => Object.values(visible).flat(), [visible]);
  const alerts = useMemo(() => all.filter((p) => ALERT_STATUS.has(p.status)), [all]);
  useEffect(() => {
    if (!alerts.length) return;
    const h = setInterval(() => setPulse((p) => (p + 0.05) % 1), 70);
    return () => clearInterval(h);
  }, [alerts.length]);

  useEffect(() => {
    const ov = overlay.current; if (!ov) return;
    const arcData = arcs === "off" ? [] : all.filter((s) => s.origin && s.dest && (arcs === "all" || (sel && sel.id === s.id)));
    const hl = changed ? all.filter((p) => changed.has(p.id)) : [];
    const rgb = (t: string) => hexToRgba(styles[t]?.color ?? "#9ca3af");
    const typeLayers = geoTypes.map((t) => {
      const st = styles[t];
      return new IconLayer<Pt>({ id: `t-${t}`, data: visible[t] ?? [], pickable: true, getPosition: (d) => d.pos, sizeUnits: "pixels", getSize: (d) => (sel?.id === d.id ? 22 : ALERT_STATUS.has(d.status) ? 22 : 17),
        ...ICON, getIcon: () => st?.shape ?? "circle", getColor: (d) => (ALERT_STATUS.has(d.status) ? ALERT : rgb(t)), updateTriggers: { getSize: sel?.id, getColor: [t, geoKey] },
        onClick: (i) => i.object && setSel(i.object as Pt) });
    });
    const shapeFeatures = geoTypes.flatMap((t) => (hiddenTypes.has(t) ? [] : shapes[t] ?? []).map((f) => ({ ...f, properties: { ...f.properties, __t: t } })));
    // Static (non-moving) types with few objects get name labels.
    const labelled = geoTypes.flatMap((t) => ((visible[t]?.length ?? 0) <= 40 && !visible[t]?.some((p) => p.origin) ? visible[t] ?? [] : []));
    ov.setProps({
      layers: [
        new GeoJsonLayer({ id: "shapes", data: shapeFeatures as any, stroked: true, filled: true, lineWidthUnits: "pixels", lineWidthMinPixels: 2, getLineColor: (f: any) => rgb(f.properties.__t), getFillColor: (f: any) => hexToRgba(styles[f.properties.__t]?.color ?? "#9ca3af", 60) }),
        new ScatterplotLayer<Pt>({ id: "changed", data: hl, getPosition: (d) => d.pos, stroked: true, filled: true, radiusUnits: "pixels", lineWidthUnits: "pixels", getRadius: 17, lineWidthMinPixels: 3,
          getFillColor: [204, 31, 138, 50], getLineColor: [204, 31, 138, 255] }),
        new ArcLayer<Pt>({ id: "arcs", data: arcData, getSourcePosition: (d) => d.origin!, getTargetPosition: (d) => d.dest!, getWidth: (d) => (sel?.id === d.id ? 3 : 1.5),
          getSourceColor: (d) => hexToRgba(styles[d.type]?.color ?? "#9ca3af", 170), getTargetColor: (d) => hexToRgba(styles[d.type]?.color ?? "#9ca3af", 170), updateTriggers: { getWidth: sel?.id } }),
        new ScatterplotLayer<Pt>({ id: "pulse", data: alerts, getPosition: (d) => d.pos, stroked: true, filled: false, radiusUnits: "pixels", lineWidthUnits: "pixels", lineWidthMinPixels: 2,
          getRadius: 14 + pulse * 16, getLineColor: [213, 94, 0, Math.round((1 - pulse) * 220)], updateTriggers: { getRadius: pulse, getLineColor: pulse } }),
        ...typeLayers,
        new TextLayer<Pt>({ id: "labels", data: labelled, getPosition: (d) => d.pos, getText: (d) => d.name + (ALERT_STATUS.has(d.status) ? ` (${d.status})` : ""), getSize: 11, getPixelOffset: [0, 16],
          getColor: [28, 32, 36, 255], outlineWidth: 3, outlineColor: [255, 255, 255, 230], fontSettings: { sdf: true }, getTextAnchor: "middle", getAlignmentBaseline: "top" }),
      ],
    });
  }, [visible, shapes, all, alerts, arcs, sel, pulse, changed, styles, geoKey, hiddenTypes]);

  // Fit the view to the data once per set of layers (the first time it has any).
  const fitted = useRef("");
  useEffect(() => {
    const map = mapRef.current; if (!map || !data.data || fitted.current === geoKey) return;
    const pts: [number, number][] = Object.values(byType).flat().map((p) => p.pos);
    const walk = (c: any) => { if (typeof c?.[0] === "number") pts.push([c[0], c[1]]); else if (Array.isArray(c)) c.forEach(walk); };
    for (const fs of Object.values(shapes)) for (const f of fs) walk(f.geometry.coordinates);
    if (!pts.length) return;
    fitted.current = geoKey;
    const b = new maplibregl.LngLatBounds(pts[0], pts[0]); pts.forEach((p) => b.extend(p));
    map.fitBounds(b, { padding: 70, maxZoom: 12, duration: 0 });
  }, [data.data, byType, shapes, geoKey]);

  const statusCounts = useMemo(() => {
    const m: Record<string, Record<string, number>> = {};
    for (const t of geoTypes) for (const p of byType[t] ?? []) if (p.status) { (m[t] ??= {})[p.status] = (m[t][p.status] ?? 0) + 1; }
    return m;
  }, [byType, geoKey]);
  const toggle = (t: string) => setHiddenTypes((h) => { const n = new Set(h); n.has(t) ? n.delete(t) : n.add(t); return n; });
  const noGeo = !!types && !geoTypes.length;

  return (
    <div className={`mappage${embedded ? " embedded" : ""}`}>
      {title && <div className="maptitle">{title}</div>}
      <div className="mapcanvas" ref={el} aria-label={`Map of ${geoTypes.join(", ") || "objects"}`} />
      <div className="mapctl">
        <label><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show finished objects (arrived, completed)</label>
        <label>Routes <select value={arcs} onChange={(e) => setArcs(e.target.value as any)}><option value="selected">Selected object</option><option value="all">All objects</option><option value="off">Hidden</option></select></label>
        <span className="muted">{live ? "Live, refreshing every 2.5 s" : "Historical view"}</span>
        {data.loading && !data.data && geoTypes.length > 0 && <Spinner label="Loading map data" />}
        {error && <ErrorBanner error={error} onRetry={typesError ? reloadTypes : data.reload} />}
        {tilesFailed && <span className="warnbox" style={{ margin: 0 }}>Basemap tiles unavailable (offline?). Showing plain background.</span>}
      </div>
      {!types && !typesError && <div className="mapnote"><Spinner label="Loading types" /></div>}
      {noGeo && <div className="mapnote"><EmptyState title="Nothing to map">None of this ontology's object types has a point or line property. Add a geometry property to see objects here.</EmptyState></div>}
      {!embedded && geoTypes.length > 0 && <div className="maplegend" aria-label="Legend">
        <b>Legend</b>
        {geoTypes.map((t) => (
          <label key={t} className="legrow" style={{ color: styles[t]?.color }}>
            <input type="checkbox" checked={!hiddenTypes.has(t)} onChange={() => toggle(t)} aria-label={`Show ${t}`} />
            <i className={`sw ${styles[t]?.shape}`} /><span className="ink"><Trunc max={28}>{t}</Trunc> ({byType[t]?.length ?? 0}{shapes[t]?.length ? ` + ${shapes[t].length} shapes` : ""})</span>
            {statusCounts[t] && <span className="muted small">{Object.entries(statusCounts[t]).slice(0, 4).map(([k, n]) => `${k} ${n}`).join(", ")}</span>}
          </label>))}
        {alerts.length > 0 && <span style={{ color: "#D55E00" }}><i className="sw circle" /><span className="ink">Needs attention: pulsing orange ({alerts.length})</span></span>}
      </div>}
      {sel && !embedded && <Panel sel={sel} asOf={asOfQ} onClose={() => setSel(null)} />}
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
