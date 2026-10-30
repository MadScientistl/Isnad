/**
 * Mock of a second, non-supply-chain workspace ("bikeshare": Station, Bike, Trip) served under `<base>/__ws/<slug>`.
 * It exists to prove the UI has no supply-chain constants: no sim (404 sim_unavailable), generic /stats/summary,
 * a geo point type (Station) and a geo line type (Trip). Type names follow the imported proposal when one was approved.
 */
type Json = Record<string, any>;
const uid = (k: string, i: number) => `${{ s: "51", b: "b1", t: "7a" }[k]}000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
const STATUS_S = ["active", "active", "active", "maintenance", "active", "offline"];
const STATUS_B = ["available", "in_use", "repair", "available"];
const SPOTS: [string, number, number][] = [["Gare du Nord", 2.355, 48.881], ["Bastille", 2.369, 48.853], ["Louvre", 2.336, 48.861], ["Montparnasse", 2.322, 48.842], ["Trocadero", 2.287, 48.862], ["La Defense", 2.238, 48.892]];

export interface Names { Station: string; Bike: string; Trip: string }
export const DEFAULT_NAMES: Names = { Station: "Station", Bike: "Bike", Trip: "Trip" };
export interface WsRoles { role: string; description: string }

export function makeWorld(names: Names, roles: WsRoles[]) {
  const N = names;
  const stations = SPOTS.map((p, i) => ({ id: uid("s", i), name: p[0], capacity: 12 + i * 4, status: STATUS_S[i], location: { type: "Point", coordinates: [p[1], p[2]] } }));
  const bikes = Array.from({ length: 24 }, (_, i) => ({ id: uid("b", i), model: i % 3 === 0 ? "electric" : "classic", status: STATUS_B[i % 4], station_id: uid("s", i % 6) }));
  const trips = Array.from({ length: 40 }, (_, i) => {
    const a = i % 6, b = (i * 5 + 2) % 6;
    return { id: uid("t", i), started_at: new Date(Date.UTC(2026, 5, 1 + (i % 28), 8 + (i % 10))).toISOString(), duration_min: 6 + ((i * 7) % 35),
      bike_id: uid("b", (i * 3) % 24), start_station: uid("s", a), end_station: uid("s", b),
      route: { type: "LineString", coordinates: [[SPOTS[a][1], SPOTS[a][2]], [SPOTS[b][1], SPOTS[b][2]]] } };
  });
  const table: Record<string, Json[]> = { [N.Station]: stations, [N.Bike]: bikes, [N.Trip]: trips };
  const enumsOf = (vals: string[]) => [...new Set(vals)];
  const types = () => ({
    namespace: "https://example.org/bikeshare#", classes: {}, role: "",
    objectTypes: [
      { name: N.Station, geo: true, properties: [{ name: "name", type: "string", required: true, enum: [] }, { name: "capacity", type: "int", required: false, enum: [] }, { name: "status", type: "enum", required: true, enum: enumsOf(STATUS_S) }, { name: "location", type: "point", required: false, enum: [] }] },
      { name: N.Bike, geo: false, properties: [{ name: "model", type: "enum", required: true, enum: ["classic", "electric"] }, { name: "status", type: "enum", required: true, enum: enumsOf(STATUS_B) }] },
      { name: N.Trip, geo: true, properties: [{ name: "started_at", type: "timestamp", required: true, enum: [] }, { name: "duration_min", type: "int", required: false, enum: [] }, { name: "route", type: "linestring", required: false, enum: [] }] },
    ],
    linkTypes: [
      { name: "bike_at", from: N.Bike, to: N.Station, cardinality: "many-to-one", required: false, transitive: false, inverse: null },
      { name: "trip_bike", from: N.Trip, to: N.Bike, cardinality: "many-to-one", required: true, transitive: false, inverse: null },
      { name: "trip_start", from: N.Trip, to: N.Station, cardinality: "many-to-one", required: true, transitive: false, inverse: null },
      { name: "trip_end", from: N.Trip, to: N.Station, cardinality: "many-to-one", required: false, transitive: false, inverse: null },
    ],
  });
  const typeOf = (id: string) => (id.startsWith("51") ? N.Station : id.startsWith("b1") ? N.Bike : N.Trip);
  const label = (o: Json) => String(o.name ?? o.model ?? o.started_at?.slice(0, 10) ?? o.id.slice(0, 6));
  const edges = () => {
    const e: { src: string; dst: string; link: string; inferred: boolean }[] = [];
    for (const b of bikes) e.push({ src: b.id, dst: b.station_id, link: "bike_at", inferred: false });
    for (const t of trips) { e.push({ src: t.id, dst: t.bike_id, link: "trip_bike", inferred: false }); e.push({ src: t.id, dst: t.start_station, link: "trip_start", inferred: false }); e.push({ src: t.id, dst: t.end_station, link: "trip_end", inferred: false }); }
    return e;
  };
  const all = (): Json[] => [...stations, ...bikes, ...trips];
  const count = (rows: Json[], k: string) => rows.reduce<Record<string, number>>((m, r) => ((m[r[k]] = (m[r[k]] ?? 0) + 1), m), {});
  const mkRoles = () => roles.map((r) => ({ role: r.role, description: r.description, region: null, user: `demo-${r.role}` }));

  function route(method: string, path: string, q: URLSearchParams, role: string): [number, unknown] | null {
    let m: RegExpMatchArray | null;
    if (path === "/sim/state" || path === "/sim/control" || path === "/sim/inject") return [404, { detail: { code: "sim_unavailable", detail: "this ontology has no simulation" } }];
    if (path === "/me") return [200, { role, user: `user:${role}`, region: null, types: Object.fromEntries(Object.keys(table).map((t) => [t, { read: true, write: role !== "viewer", masked: [] }])), actions: [], approves: [] }];
    if (path === "/roles") return [200, mkRoles()];
    if (path === "/types") return [200, { ...types(), role }];
    if (path === "/events") return [200, []];
    if (path === "/proposals" || path === "/executions" || path === "/audit" || path === "/scenarios") return [200, []];
    if (path === "/actions") return [200, []];
    if (path.startsWith("/agent")) return [404, { detail: { code: "no_agent", detail: "no agent configured" } }];
    if (path === "/stats/summary") {
      const bs = count(bikes, "status");
      return [200, { as_of: new Date().toISOString(), counts: { [N.Station]: stations.length, [N.Bike]: bikes.length, [N.Trip]: trips.length },
        enums: { [N.Station]: { status: count(stations, "status") }, [N.Bike]: { status: bs, model: count(bikes, "model") } },
        kpis: { bikes_available: { value: bs.available ?? 0, label: "Bikes available", description: "status = available" }, avg_trip_min: { value: Math.round(trips.reduce((a, t) => a + t.duration_min, 0) / trips.length), label: "Average trip (min)" } } }];
    }
    if ((m = path.match(/^\/geo\/(\w+)$/))) {
      const rows = table[m[1]]; if (!rows) return [404, { detail: "unknown type" }];
      const feats = rows.filter((o) => o.location ?? o.route).map((o) => ({ type: "Feature", id: o.id, geometry: o.location ?? o.route, properties: { id: o.id, type: m![1], name: o.name, status: o.status } }));
      return [200, { type: "FeatureCollection", features: feats }];
    }
    if ((m = path.match(/^\/objects\/(\w+)$/)) && method === "GET") {
      const rows = table[m[1]]; if (!rows) return [404, { detail: "unknown type" }];
      const off = Number(q.get("offset") ?? 0), lim = Number(q.get("limit") ?? 100);
      return [200, rows.slice(off, off + lim).map(({ location, route: _r, ...r }) => ({ ...r, ...(location ? { location } : {}), valid_from: "2026-06-01T00:00:00Z", valid_to: null, changed_by: "import" }))];
    }
    if ((m = path.match(/^\/objects\/(\w+)\/([\w-]+)\/history$/))) return [200, (table[m[1]] ?? []).filter((o) => o.id === m![2]).map((o) => ({ ...o, valid_from: "2026-06-01T00:00:00Z", valid_to: null, recorded_from: "2026-06-01T00:00:00Z", recorded_to: null }))];
    if ((m = path.match(/^\/objects\/(\w+)\/([\w-]+)$/)) && method === "GET") { const o = (table[m[1]] ?? []).find((x) => x.id === m![2]); return o ? [200, { ...o, valid_from: "2026-06-01T00:00:00Z", valid_to: null, changed_by: "import" }] : [404, { detail: "not found" }]; }
    if ((m = path.match(/^\/neighbours\/(\w+)\/([\w-]+)$/))) {
      const id = m[2], out: Json[] = [];
      for (const e of edges()) { const other = e.src === id ? e.dst : e.dst === id ? e.src : null; if (other) out.push({ link: e.link, direction: e.src === id ? "out" : "in", type: typeOf(other), id: other, object: all().find((o) => o.id === other) ?? null }); }
      return [200, out];
    }
    if (path === "/graph") {
      const c = q.get("center")!, depth = Math.min(3, Number(q.get("depth") ?? 1));
      if (!all().some((o) => o.id === c)) return [404, { detail: "not found" }];
      const es = edges(); const seen = new Set([c]); let fr = [c];
      for (let d = 0; d < depth; d++) { const nx: string[] = []; for (const e of es) for (const [x, y] of [[e.src, e.dst], [e.dst, e.src]]) if (fr.includes(x) && !seen.has(y)) { seen.add(y); nx.push(y); } fr = nx; }
      const ids = [...seen].slice(0, 120), set = new Set(ids);
      return [200, { center: c, as_of: null, inferred_available: false, truncated: seen.size > 120, nodes: ids.map((id) => { const o = all().find((x) => x.id === id)!; return { id, type: typeOf(id), label: label(o), status: o.status ?? null }; }), edges: es.filter((e) => set.has(e.src) && set.has(e.dst)) }];
    }
    return null;
  }
  return { route, types: () => types() };
}
