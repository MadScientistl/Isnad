/**
 * Mock of the Phase 6 admin API: /ingest/*, /workspaces, /ontology/* and /auth/login (contract: docs/contracts.md, "Phase 6 contracts").
 * Deterministic: jobs advance with elapsed wall time (no timers), ids are sequential. The "dataset" is always the bikeshare example;
 * file names steer failures: `nomodel` -> 503 agent_unavailable for mode "model", `big` -> truncated profile, `.exe` etc. -> 422.
 */
type Json = Record<string, any>;
type R = [number, unknown];
const ok = (d: unknown, s = 200): R => [s, d];
const fail = (s: number, code: string, detail: string, extra: Json = {}): R => [s, { code, detail, ...extra }];
const iso = (ms: number) => new Date(ms).toISOString();
let seq = 0;
const nid = (p: string) => `${p}${(++seq).toString(16).padStart(4, "0")}0000-0000-4000-8000-${String(seq).padStart(12, "0")}`;

/* ------------------------------------------------------------------ profile ------------------------------------------------------------------ */
const col = (t: string, i: number, name: string, type: string, o: Json = {}) => ({ id: `${t}.c${i}`, name, ident: name.toLowerCase().replace(/\W+/g, "_"), type, null_rate: 0, distinct: 100, unique: false, min: null, max: null, top: [], enum_candidate: null, format: null, geo: null, time: false, notes: [], ...o });
function profileFor(filename: string, kind: string, bytes: number | null): Json {
  const big = /big/i.test(filename);
  return {
    version: 1, source: { kind, name: filename, sha256: "9f2c" + "ab".repeat(30), bytes }, caps: { max_tables: 50, max_columns: 60, max_rows_profiled: 200000, max_sample: 5000, top_k: 5 }, truncated: big, seed: 1,
    tables: [
      { id: "t1", name: "stations", ident: "Stations", rows: 120, sampled_rows: 120, is_join_table: false, key_candidates: [["t1.c1"]], columns: [
        col("t1", 1, "id", "int", { unique: true, distinct: 120, min: "1", max: "120" }), col("t1", 2, "name", "string", { distinct: 118, top: [["Gare du Nord", 2], ["Bastille", 2], ["x".repeat(150), 1]] }),
        col("t1", 3, "lat", "float", { geo: { role: "lat", pair: "t1.c4" }, min: "48.80", max: "48.92" }), col("t1", 4, "lon", "float", { geo: { role: "lon", pair: "t1.c3" }, min: "2.20", max: "2.45" }),
        col("t1", 5, "capacity", "int", { distinct: 14, min: "8", max: "60" }), col("t1", 6, "status", "string", { distinct: 3, enum_candidate: ["active", "maintenance", "offline"], top: [["active", 101], ["maintenance", 12], ["offline", 7]] })] },
      { id: "t2", name: "bikes", ident: "Bikes", rows: 800, sampled_rows: 800, is_join_table: false, key_candidates: [["t2.c1"]], columns: [
        col("t2", 1, "id", "int", { unique: true, distinct: 800 }), col("t2", 2, "model", "string", { distinct: 2, enum_candidate: ["classic", "electric"] }),
        col("t2", 3, "status", "string", { distinct: 3, enum_candidate: ["available", "in_use", "repair"] }), col("t2", 4, "station_id", "int", { null_rate: 0.04, distinct: 118 })] },
      { id: "t3", name: "trips", ident: "Trips", rows: big ? 200000 : 15200, sampled_rows: big ? 5000 : 15200, is_join_table: false, key_candidates: [["t3.c1"]], columns: [
        col("t3", 1, "id", "int", { unique: true, distinct: 15200 }), col("t3", 2, "bike_id", "int"), col("t3", 3, "start_station", "int"), col("t3", 4, "end_station", "int", { null_rate: 0.07 }),
        col("t3", 5, "started_at", "timestamp", { format: "iso8601", time: true, min: "2026-01-01T00:02:00Z", max: "2026-06-30T23:58:00Z" }), col("t3", 6, "duration_min", "int", { min: "1", max: "212" }),
        col("t3", 7, "notes", "string", { null_rate: 0.81, notes: ["free text, skipped by the baseline"], top: [["<script>alert('x')</script>", 3]] })] },
    ],
    fks: [
      { id: "f1", from: "t2.c4", to: "t1.c1", declared: false, containment: 0.99, name_score: 0.8, confidence: 0.97, cardinality: "many-to-one", orphans: 3 },
      { id: "f2", from: "t3.c2", to: "t2.c1", declared: true, containment: 1, name_score: 0.9, confidence: 1, cardinality: "many-to-one", orphans: 0 },
      { id: "f3", from: "t3.c3", to: "t1.c1", declared: false, containment: 0.99, name_score: 0.5, confidence: 0.88, cardinality: "many-to-one", orphans: 12 },
      { id: "f4", from: "t3.c4", to: "t1.c1", declared: false, containment: 0.93, name_score: 0.4, confidence: 0.55, cardinality: "many-to-one", orphans: 1064 },
    ],
    warnings: ["Column trips.notes is free text with 81% nulls; the baseline skips it.", ...(big ? ["Table trips has 200000 rows; only the first 5000 were profiled."] : [])],
  };
}

/* ------------------------------------------------------------------ proposals ------------------------------------------------------------------ */
const ENUM_S = { enum: ["active", "maintenance", "offline"] }, ENUM_B = { enum: ["available", "in_use", "repair"] };
function baseDoc(mode: "baseline" | "model", hash: string): Json {
  const model = mode === "model";
  return {
    version: 1, profile_hash: hash, namespace: "https://example.org/ws/bikeshare#", classes: [{ name: "Asset", extends: null }],
    objectTypes: [
      { name: "Station", label: "Station", extends: model ? "Asset" : null, key: ["t1.c1"], source_table: "t1", properties: [
        { name: "name", type: "string", required: true, column: "t1.c2", transform: "none", label: "name" }, { name: "capacity", type: "int", required: false, column: "t1.c5", transform: "int", label: "capacity" },
        { name: "status", type: ENUM_S, required: true, column: "t1.c6", transform: "enum_map:{}", label: "status" }, { name: "location", type: "point", required: false, column: null, transform: "point(t1.c3,t1.c4)", label: "location" }] },
      { name: "Bike", label: "Bike", extends: model ? "Asset" : null, key: ["t2.c1"], source_table: "t2", properties: [
        { name: "model", type: { enum: ["classic", "electric"] }, required: true, column: "t2.c2", transform: "enum_map:{}", label: "model" }, { name: "status", type: ENUM_B, required: true, column: "t2.c3", transform: "enum_map:{}", label: "status" }] },
      { name: "Trip", label: "Trip", extends: null, key: ["t3.c1"], source_table: "t3", properties: [
        { name: "started_at", type: "timestamp", required: true, column: "t3.c5", transform: "timestamp:iso8601", label: "started at" }, { name: "duration_min", type: "int", required: false, column: "t3.c6", transform: "int", label: "duration (min)" },
        ...(model ? [{ name: "notes", type: "string", required: false, column: "t3.c7", transform: "none", label: "notes" }] : [])] },
    ],
    linkTypes: [
      { name: "bike_at", from: "Bike", to: "Station", cardinality: "many-to-one", inverse: "holds", transitive: false, required: false, source: { kind: "fk", from_col: "t2.c4", to_type_key: "t1.c1" } },
      { name: "trip_bike", from: "Trip", to: "Bike", cardinality: "many-to-one", inverse: null, transitive: false, required: true, source: { kind: "fk", from_col: "t3.c2", to_type_key: "t2.c1" } },
      { name: "trip_start", from: "Trip", to: "Station", cardinality: "many-to-one", inverse: null, transitive: false, required: true, source: { kind: "fk", from_col: "t3.c3", to_type_key: "t1.c1" } },
      { name: "trip_end", from: "Trip", to: "Station", cardinality: "many-to-one", inverse: null, transitive: false, required: false, source: { kind: "fk", from_col: "t3.c4", to_type_key: "t1.c1" } },
    ],
    actions: [
      { name: "set_station_status", description: "Change a station's status", kind: "set_status", type: "Station", params: { value: "enum" } },
      { name: "set_bike_status", description: "Change a bike's status", kind: "set_status", type: "Bike", params: { value: "enum" } },
      ...(model ? [{ name: "move_bike", description: "Move a bike to another station", kind: "link", type: "Bike", params: { link: "bike_at" } }] : []),
    ],
    roles: [{ name: "admin", description: "Full access, approves everything", access: "admin" }, { name: "operator", description: "Reads everything and runs the listed actions", access: "write" }, { name: "viewer", description: "Read-only", access: "read" }, ...(model ? [{ name: "analyst", description: "Read-only for reporting", access: "read" }] : [])],
    rationale: model ? "Stations, bikes and trips map one-to-one to tables. Trips link a bike and two stations; the end station is optional because 7% of trips are still open. Free-text notes are kept as an optional string." : "Heuristic baseline: one type per table, foreign keys become links, columns with at most 20 distinct values become enums.",
    notes: model ? ["Marked Trip.end_station optional because of 7% nulls."] : [],
  };
}
const NAME_RE = { type: /^[A-Z][A-Za-z0-9]{0,40}$/, prop: /^[a-z][a-z0-9_]{0,40}$/ };
const nullOf = (colId: string | null) => (colId === "t2.c4" ? 0.04 : colId === "t3.c4" ? 0.07 : colId === "t3.c7" ? 0.81 : 0);
const colType = (colId: string) => ({ "t1.c3": "float", "t1.c4": "float", "t1.c5": "int", "t3.c5": "timestamp", "t3.c6": "int", "t1.c1": "int", "t2.c1": "int", "t3.c1": "int" } as Record<string, string>)[colId] ?? "string";
const typeName = (t: unknown) => (typeof t === "string" ? t : "enum");
function validate(doc: Json): Json {
  const checks: Json[] = [];
  const add = (name: string, okv: boolean, value: unknown, threshold: unknown, detail: string, examples: string[] = []) => checks.push({ name, ok: okv, value, threshold, detail, examples });
  add("schema_ok", true, true, true, "Proposal validates against the schema.");
  add("ddl_ok", true, true, true, "DDL applies in a scratch schema.");
  add("key_violations", true, 0, 0, "Keys are unique and non-null.");
  let worst = 0; const nullEx: string[] = [];
  for (const t of doc.objectTypes) for (const p of t.properties) if (p.required) { const n = nullOf(p.column); if (n > worst) worst = n; if (n > 0) nullEx.push(`${t.name}.${p.name}: ${Math.round(n * 100)}% of ${p.column} are null`); }
  add("required_null_rate", worst <= 0, worst, 0, worst > 0 ? "A required property has null values; make it optional." : "Required properties have no nulls.", nullEx.slice(0, 3));
  let bad = 0; const coEx: string[] = [];
  for (const t of doc.objectTypes) for (const p of t.properties) if (p.column) {
    const pt = colType(p.column), want = typeName(p.type);
    const compat = want === pt || want === "string" || (pt === "int" && want === "float") || want === "enum" || want === "point";
    if (!compat) { bad = 0.31; coEx.push(`${t.name}.${p.name}: cannot read '${p.column}' (${pt}) as ${want}`); }
  }
  add("type_coercion_fail_rate", bad <= 0.02, bad || 0.004, 0.02, bad ? "Some columns cannot be coerced to the declared type." : "Values coerce cleanly.", coEx.slice(0, 3));
  add("enum_violation_rate", true, 0, 0.02, "All values are inside the declared enums.");
  const repaired = (doc.notes as string[]).some((n) => n.startsWith("repaired:"));
  const RES: Record<string, number> = { bike_at: 0.99, trip_bike: 1, trip_start: 0.99, trip_end: repaired ? 0.97 : 0.93 };
  for (const l of doc.linkTypes) {
    const v = RES[l.name] ?? 0.99, good = v >= 0.95;
    add(`link_resolution:${l.name}`, good, v, 0.95, good ? "Endpoints resolve." : `${Math.round((1 - v) * 100)}% of ${l.name} rows point at missing rows.`,
      good ? [] : [`row 17: end_station "<img src=x onerror=alert(1)>" has no matching station`, `row 902: end_station "9999" has no matching station`, "row 4410: end_station \"‮evil\" has no matching station"]);
  }
  add("shacl_conforms", true, true, true, "Generated shapes accept the loaded data.");
  add("reasoner_consistent", true, "412 inferred / 380 asserted", "50x", "Materialisation completes.");
  return { ok: checks.every((c) => c.ok), checks, stats: { rows_loaded: { Station: 120, Bike: 800, Trip: 15200 }, links_loaded: { bike_at: 776, trip_bike: 15200, trip_start: 15188, trip_end: 14136 } }, seconds: 2.4 };
}
const yamlOf = (d: Json) => [`name: ${d.objectTypes[0]?.name ?? "ontology"}`, `namespace: ${d.namespace}`, "objectTypes:",
  ...d.objectTypes.flatMap((t: Json) => [`  ${t.name}:`, "    properties:", ...t.properties.map((p: Json) => `      ${p.name}: {type: ${typeName(p.type)}${p.required ? ", required: true" : ""}}`)]), "linkTypes:",
  ...d.linkTypes.map((l: Json) => `  ${l.name}: {from: ${l.from}, to: ${l.to}, cardinality: ${l.cardinality}}`), "roles:", ...d.roles.map((r: Json) => `  ${r.name}: {access: ${r.access}}`)].join("\n");
function diffOf(d: Json): Json {
  const props = d.objectTypes.flatMap((t: Json) => t.properties.map((p: Json) => `${t.name}.${p.name}`));
  const added: Json = { objectTypes: d.objectTypes.map((t: Json) => t.name), properties: props, linkTypes: d.linkTypes.map((l: Json) => l.name), actions: d.actions.map((a: Json) => a.name), roles: d.roles.map((r: Json) => r.name) };
  const sensitive = d.roles.filter((r: Json) => r.access !== "read").map((r: Json) => `role ${r.name}: ${r.access === "admin" ? "all_actions and all_approves granted" : "write access on all types"}`);
  return { added, removed: {}, changed: {}, breaking: [], sensitive };
}

interface Sess { id: string; info: Json; profile: Json; props: string[] }
interface Prop { id: string; session_id: string; seq: number; parent_id: string | null; kind: string; model: string | null; cost_usd: number | null; created_at: string; doc: Json; validation: Json }
const sessions = new Map<string, Sess>(), props = new Map<string, Prop>();
const decisions = new Map<string, Json>();
interface Job { id: string; t0: number; steps: string[]; per: number; cost: number | null; proposal_id?: string; result?: Json; fn?: () => Json }
const jobs = new Map<string, Job>();
let nowMs = () => Date.now();
const WS: Json[] = [
  { slug: "bikeshare", schema: "ws_bikeshare", ontology_hash: "c3a1" + "0f".repeat(30), imported_at: "2026-10-01T09:12:00Z", rows: 16120, api_base: "@WS/bikeshare" },
  { slug: "legacy_orders", schema: "ws_legacy_orders", ontology_hash: "77be" + "d1".repeat(30), imported_at: "2026-09-12T14:00:00Z", rows: 5400, api_base: null },
];
let HOST = "";
export const wsNames: Record<string, Record<string, string>> = {};
export function setHost(h: string) { HOST = h; }
export const workspaceRoles: Record<string, { role: string; description: string }[]> = {};

function addProposal(sid: string, kind: string, parent: Prop | null, doc: Json, cost: number | null, model: string | null): Prop {
  const s = sessions.get(sid)!;
  const p: Prop = { id: nid("c0"), session_id: sid, seq: s.props.length + 1, parent_id: parent?.id ?? null, kind, model, cost_usd: cost, created_at: iso(nowMs()), doc, validation: validate(doc) };
  props.set(p.id, p); s.props.push(p.id); return p;
}
const sessionOut = (s: Sess) => ({ ...s.info, profile: s.profile, proposals: s.props.map((id) => { const p = props.get(id)!; return { id: p.id, seq: p.seq, parent_id: p.parent_id, kind: p.kind, model: p.model, cost_usd: p.cost_usd, created_at: p.created_at, ok: p.validation.ok }; }) });
const propOut = (p: Prop) => ({ id: p.id, kind: p.kind, session_id: p.session_id, seq: p.seq, cost_usd: p.cost_usd, model: p.model, proposal: p.doc, yaml: yamlOf(p.doc), mapping: { version: 1, profile_hash: p.doc.profile_hash, types: {} }, validation: p.validation, diff: diffOf(p.doc) });
function jobOut(j: Job): Json {
  const done = Math.min(j.steps.length, Math.floor((nowMs() - j.t0) / j.per));
  const finished = done >= j.steps.length;
  if (finished && j.fn && !j.result) j.result = j.fn();
  const events: Json[] = j.steps.slice(0, done).map((m, i) => ({ seq: i + 1, kind: "progress", payload: { message: m } }));
  if (finished) events.push({ seq: events.length + 1, kind: "finished", payload: { status: "done", error: null } });
  return { id: j.id, kind: "mock", status: finished ? "done" : done ? "running" : "queued", events, proposal_id: finished ? j.proposal_id ?? null : null, cost_usd: finished ? j.cost ?? 0 : 0, result: finished ? j.result ?? (j.proposal_id ? { proposal_id: j.proposal_id } : null) : null, error: null };
}

/* JSON-pointer edit of the proposal document (accept = no-op, reject = remove from its array / mark optional, edit = set). */
function apply(doc: Json, item: Json): string | null {
  const parts = String(item.path).split("/").filter(Boolean);
  if (!parts.length || parts.some((p) => p === "__proto__" || p === "constructor")) return `bad path ${item.path}`;
  let cur: any = doc;
  for (const k of parts.slice(0, -1)) { cur = cur?.[k]; if (cur === undefined || cur === null) return `unknown path ${item.path}`; }
  const last = parts[parts.length - 1];
  if (item.action === "accept") return null;
  if (item.action === "reject") { if (Array.isArray(cur)) { const i = Number(last); if (!(i in cur)) return `unknown path ${item.path}`; cur.splice(i, 1); } else delete cur[last]; return null; }
  if (item.action === "edit") {
    if (cur[last] === undefined) return `unknown path ${item.path}`;
    const v = item.value;
    if (last === "name") {
      const isType = parts.length === 3 && parts[0] === "objectTypes", isRole = parts[0] === "roles" || parts[0] === "actions";
      const re = isType ? NAME_RE.type : NAME_RE.prop;
      if (typeof v !== "string" || !re.test(v)) return `'${String(v).slice(0, 40)}' is not a valid ${isType ? "type" : "identifier"} name`;
      void isRole;
      if (isType) { const old = cur.name; for (const l of doc.linkTypes) { if (l.from === old) l.from = v; if (l.to === old) l.to = v; } for (const a of doc.actions) if (a.type === old) a.type = v; }
    }
    cur[last] = v; return null;
  }
  return `unknown action ${item.action}`;
}

export function route(method: string, path: string, q: URLSearchParams, body: any, role: string, blob?: Blob): R | null {
  if (!/^\/(ingest|workspaces)(\/|$)/.test(path)) return null;
  if (role !== "admin") return [404, { detail: "not found" }];
  let m: RegExpMatchArray | null;
  if (path === "/workspaces") return ok(WS.map((w) => ({ ...w, api_base: w.api_base ? w.api_base.replace("@WS", `${HOST}/__ws`) : null })));
  if (path === "/ingest/sources") return ok([{ name: "warehouse_pg", kind: "postgres" }, { name: "legacy_sqlite", kind: "sqlite" }]);
  if (path === "/ingest/uploads" && method === "POST") {
    const fn = q.get("filename") ?? "";
    if (!/\.(csv|tsv|zip|sqlite|db)$/i.test(fn)) return fail(422, "unsupported_type", "Unsupported file type. Use csv, tsv, zip or sqlite.");
    const size = blob?.size ?? 0;
    if (size > 20 * 1048576) return fail(413, "too_large", "Upload exceeds 20 MiB.");
    if (/empty/i.test(fn) || size === 0) return fail(422, "empty_source", "The file has no rows to profile.");
    const id = nid("5e");
    const kind = /\.tsv$/i.test(fn) ? "csv" : /\.(sqlite|db)$/i.test(fn) ? "sqlite" : "csv";
    const profile = profileFor(fn, kind, size);
    sessions.set(id, { id, info: { id, source_kind: kind, source_name: fn, source_sha256: profile.source.sha256, created_at: iso(nowMs()), created_by: "user:admin" }, profile, props: [] });
    return ok({ session_id: id, profile_summary: { tables: profile.tables.length, rows: 16120, truncated: profile.truncated } }, 201);
  }
  if ((m = path.match(/^\/ingest\/sources\/([\w-]+)\/profile$/)) && method === "POST") {
    if (!["warehouse_pg", "legacy_sqlite"].includes(m[1])) return fail(404, "unknown_source", "No such source.");
    const id = nid("5e"), profile = profileFor(m[1], m[1] === "legacy_sqlite" ? "sqlite" : "postgres", null);
    sessions.set(id, { id, info: { id, source_kind: profile.source.kind, source_name: m[1], source_sha256: profile.source.sha256, created_at: iso(nowMs()), created_by: "user:admin" }, profile, props: [] });
    return ok({ session_id: id }, 201);
  }
  if ((m = path.match(/^\/ingest\/sessions\/([\w-]+)$/))) { const s = sessions.get(m[1]); return s ? ok(sessionOut(s)) : fail(404, "not_found", "Unknown session."); }
  if ((m = path.match(/^\/ingest\/sessions\/([\w-]+)\/proposals$/)) && method === "POST") {
    const s = sessions.get(m[1]); if (!s) return fail(404, "not_found", "Unknown session.");
    const mode = body?.mode === "model" ? "model" : "baseline";
    if (mode === "model" && /nomodel/i.test(s.info.source_name)) return fail(503, "agent_unavailable", "No model API key is configured on the server.");
    const doc = baseDoc(mode, "h" + s.profile.source.sha256.slice(0, 12));
    const p = addProposal(s.id, mode, null, doc, mode === "model" ? 0.37 : 0, mode === "model" ? "claude-mock" : null);
    const j: Job = { id: nid("10"), t0: nowMs(), per: mode === "model" ? 700 : 250, steps: mode === "model" ? ["Reading the profile", "Sampling columns", "Checking the draft in a scratch schema", "Submitting the proposal"] : ["Applying heuristics", "Validating in a scratch schema"], cost: p.cost_usd, proposal_id: p.id };
    jobs.set(j.id, j); return ok({ job_id: j.id }, 202);
  }
  if ((m = path.match(/^\/ingest\/jobs\/([\w-]+)$/))) { const j = jobs.get(m[1]); return j ? ok(jobOut(j)) : fail(404, "not_found", "Unknown job."); }
  if ((m = path.match(/^\/ingest\/proposals\/([\w-]+)$/)) && method === "GET") { const p = props.get(m[1]); return p ? ok(propOut(p)) : fail(404, "not_found", "Unknown proposal."); }
  if ((m = path.match(/^\/ingest\/proposals\/([\w-]+)\/repair$/)) && method === "POST") {
    const p = props.get(m[1]); if (!p) return fail(404, "not_found", "Unknown proposal.");
    const s = sessions.get(p.session_id)!;
    let rounds = 0, c: Prop | undefined = p; while (c) { if (c.kind === "repair") rounds++; c = c.parent_id ? props.get(c.parent_id) : undefined; }
    if (rounds >= 3) return fail(409, "repair_limit", "Repair rounds exhausted (3).");
    const doc = JSON.parse(JSON.stringify(p.doc)); doc.notes.push("repaired: trip_end filtered to rows whose end station exists");
    const np = addProposal(s.id, "repair", p, doc, p.model ? 0.12 : 0, p.model);
    const j: Job = { id: nid("10"), t0: nowMs(), per: 500, steps: ["Reading the failed checks", "Adjusting the link source", "Re-validating"], cost: np.cost_usd, proposal_id: np.id };
    jobs.set(j.id, j); return ok({ job_id: j.id }, 202);
  }
  if ((m = path.match(/^\/ingest\/proposals\/([\w-]+)\/items$/)) && method === "PATCH") {
    const p = props.get(m[1]); if (!p) return fail(404, "not_found", "Unknown proposal.");
    const doc = JSON.parse(JSON.stringify(p.doc));
    const items: Json[] = Array.isArray(body?.items) ? body.items : [];
    // Apply rejects from the highest index down so earlier indexes stay valid.
    const ordered = [...items].sort((a, b) => (a.action === "reject" && b.action === "reject" ? String(b.path).localeCompare(String(a.path), undefined, { numeric: true }) : 0));
    for (const it of ordered) { const e = apply(doc, it); if (e) return fail(422, "invalid_item", e); }
    return ok({ id: addProposal(p.session_id, "edit", p, doc, 0, null).id }, 201);
  }
  if ((m = path.match(/^\/ingest\/proposals\/([\w-]+)\/approve$/)) && method === "POST") {
    const p = props.get(m[1]); if (!p) return fail(404, "not_found", "Unknown proposal.");
    if (!p.validation.ok) return fail(409, "validation_failed", "only a proposal that passes validation can be approved");
    const sens = diffOf(p.doc).sensitive;
    if (sens.length && !String(body?.comment ?? "").trim()) return fail(422, "comment_required", "This proposal grants permissions; a comment is required.", { sensitive: sens });
    const d = { decision_id: nid("d0"), proposal_id: p.id, decision: "approved", approver: "demo-admin", slug: "mock" };
    decisions.set(d.decision_id, { ...d, proposal: p }); return ok(d, 201);
  }
  if ((m = path.match(/^\/ingest\/decisions\/([\w-]+)\/import$/)) && method === "POST") {
    const d = decisions.get(m[1]); if (!d) return fail(404, "not_found", "Unknown decision.");
    const s = sessions.get(d.proposal.session_id)!;
    let slug = String(s.info.source_name).replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 30) || "import";
    if (!/^[a-z]/.test(slug)) slug = "w_" + slug;
    const taken = new Set(WS.map((w) => w.slug)); let u = slug, n = 2; while (taken.has(u)) u = `${slug}_${n++}`;
    const names: Record<string, string> = {}; for (const t of d.proposal.doc.objectTypes) names[t.source_table] = t.name;
    const j: Job = { id: nid("10"), t0: nowMs(), per: 600, cost: null, steps: ["Creating schema ws_" + u, "Applying DDL and registering the ontology", "Loading objects", "Resolving and loading links", "Writing the audit event", "Writing workspace files"],
      fn: () => {
        const exists = WS.find((w) => w.slug === u);
        if (!exists) {
          WS.push({ slug: u, schema: `ws_${u}`, ontology_hash: "ab12" + "5e".repeat(30), imported_at: iso(nowMs()), rows: 16120, api_base: `@WS/${u}` });
          wsNames[u] = { Station: names.t1 ?? "Station", Bike: names.t2 ?? "Bike", Trip: names.t3 ?? "Trip" };
          workspaceRoles[u] = d.proposal.doc.roles.map((r: Json) => ({ role: r.name, description: r.description }));
        }
        return { slug: u, schema: `ws_${u}`, rows: 16120, counts: { objects: { [names.t1 ?? "Station"]: 120, [names.t2 ?? "Bike"]: 800, [names.t3 ?? "Trip"]: 15200 }, links: { bike_at: 776, trip_bike: 15200, trip_start: 15188, trip_end: 14136 } },
          dropped_links: { bike_at: { unresolved: 24, duplicate: 0 }, trip_start: { unresolved: 12, duplicate: 0 }, trip_end: { unresolved: 1064, duplicate: 0 } }, dropped_objects: {}, already_imported: !!exists, api_base: `${HOST}/__ws/${u}`, seconds: 3.6 };
      } };
    jobs.set(j.id, j); return ok({ job_id: j.id }, 202);
  }
  return null;
}

/* ------------------------------------------------------------------ ontology versions (per workspace) ------------------------------------------------------------------ */
interface Snap { types: string[]; props: string[]; links: string[]; actions: string[]; roles: string[] }
interface Ver { id: number; hash: string; parent_hash: string | null; created_by: string; created_at: string; source: string; note: string | null; snap: Snap }
interface OProp { id: string; base_hash: string; breaking: boolean; rationale: string | null; proposer: string; created_at: string; diff: Json; decision: string | null; approver: string | null; version_id: number | null; decision_comment: string | null; add?: Partial<Snap> }
const hx = (n: number) => (n * 2654435761 >>> 0).toString(16).padStart(8, "0").repeat(8);
const stores = new Map<string, { versions: Ver[]; proposals: OProp[] }>();
function store(ws: string) {
  let s = stores.get(ws);
  if (s) return s;
  if (ws === "default") {
    const v1: Snap = { types: ["Port", "Warehouse", "Shipment", "Invoice", "Supplier"], props: ["Port.name", "Port.status", "Shipment.ref", "Shipment.status", "Invoice.amount", "Supplier.name"], links: ["ships_from", "ships_to"], actions: ["close_port"], roles: ["admin", "planner"] };
    const v2: Snap = { types: [...v1.types, "Carrier"], props: [...v1.props, "Carrier.name"], links: [...v1.links, "operated_by"], actions: [...v1.actions, "reroute_shipment"], roles: v1.roles };
    const v3: Snap = { ...v2, roles: [...v2.roles, "finance_analyst"] };
    const mk = (id: number, snap: Snap, src: string, note: string | null, by: string): Ver => ({ id, hash: hx(id), parent_hash: id > 1 ? hx(id - 1) : null, created_by: by, created_at: iso(Date.parse("2026-09-01T10:00:00Z") + id * 86400000 * 9), source: src, note, snap });
    s = { versions: [mk(1, v1, "startup", null, "startup"), mk(2, v2, "proposal", "Add carriers and rerouting", "user:admin"), mk(3, v3, "proposal", "Finance analyst role", "user:admin")], proposals: [
      { id: "aa000000-0000-4000-8000-000000000001", base_hash: hx(3), breaking: false, rationale: "Track contracts with suppliers.", proposer: "user:admin", created_at: "2026-10-20T08:00:00Z", decision: null, approver: null, version_id: null, decision_comment: null,
        diff: { added: { objectTypes: ["Contract"], properties: ["Contract.ref", "Contract.value"], linkTypes: ["contract_with"] }, removed: {}, changed: {}, breaking: [], sensitive: [] }, add: { types: ["Contract"], props: ["Contract.ref", "Contract.value"], links: ["contract_with"] } },
      { id: "aa000000-0000-4000-8000-000000000002", base_hash: hx(3), breaking: true, rationale: "Simplify the model.", proposer: "agent:admin", created_at: "2026-10-22T08:00:00Z", decision: null, approver: null, version_id: null, decision_comment: null,
        diff: { added: {}, removed: { objectTypes: ["Supplier"], properties: ["Supplier.name"] }, changed: { properties: { "Invoice.amount": { type: ["float", "string"] } } }, breaking: ["objectType Supplier removed", "property Invoice.amount: type changed float -> string"], sensitive: [] } },
      { id: "aa000000-0000-4000-8000-000000000003", base_hash: hx(3), breaking: false, rationale: "Planners need to see invoices to plan payments.", proposer: "user:admin", created_at: "2026-10-25T08:00:00Z", decision: null, approver: null, version_id: null, decision_comment: null,
        diff: { added: {}, removed: {}, changed: { roles: { planner: { types: [{ Port: "r" }, { Port: "r", Invoice: "r" }] } } }, breaking: [], sensitive: ["role planner: gains access to existing type Invoice", "role planner: property Invoice.amount no longer masked"] } },
      { id: "aa000000-0000-4000-8000-000000000004", base_hash: hx(2), breaking: false, rationale: "Finance analyst role.", proposer: "user:admin", created_at: "2026-09-19T08:00:00Z", decision: "approved", approver: "user:admin", version_id: 3, decision_comment: "ok",
        diff: { added: { roles: ["finance_analyst"] }, removed: {}, changed: {}, breaking: [], sensitive: [] } },
    ] };
  } else {
    const n = wsNames[ws] ?? { Station: "Station", Bike: "Bike", Trip: "Trip" };
    const snap: Snap = { types: Object.values(n), props: [`${n.Station}.name`, `${n.Bike}.status`], links: ["bike_at", "trip_bike"], actions: ["set_bike_status"], roles: (workspaceRoles[ws] ?? [{ role: "admin" }]).map((r) => r.role) };
    s = { versions: [{ id: 1, hash: hx(100 + ws.length), parent_hash: null, created_by: "user:admin", created_at: iso(nowMs()), source: "import", note: "Imported from the import wizard", snap }], proposals: [] };
  }
  stores.set(ws, s); return s;
}
const yamlSnap = (s: Snap, id: number) => `# version ${id}\nobjectTypes: [${s.types.join(", ")}]\nlinkTypes: [${s.links.join(", ")}]\nactions: [${s.actions.join(", ")}]\nroles: [${s.roles.join(", ")}]\n`;
function snapDiff(a: Snap, b: Snap): Json {
  const added: Json = {}, removed: Json = {}, breaking: string[] = [], sensitive: string[] = [];
  const kinds: [keyof Snap, string][] = [["types", "objectTypes"], ["props", "properties"], ["links", "linkTypes"], ["actions", "actions"], ["roles", "roles"]];
  for (const [k, kind] of kinds) {
    const ad = b[k].filter((x) => !a[k].includes(x)), rm = a[k].filter((x) => !b[k].includes(x));
    if (ad.length) added[kind] = ad; if (rm.length) { removed[kind] = rm; breaking.push(...rm.map((x) => `${kind.replace(/s$/, "")} ${x} removed`)); }
  }
  return { added, removed, changed: {}, breaking, sensitive };
}
export function ontRoute(method: string, path: string, q: URLSearchParams, body: any, role: string, ws: string): R | null {
  if (!/^\/ontology(\/|$)/.test(path)) return null;
  if (role !== "admin") return [403, { detail: "admin only" }];
  const st = store(ws);
  let m: RegExpMatchArray | null;
  const pub = (v: Ver) => { const { snap: _s, ...r } = v; return r; };
  if (path === "/ontology/versions") return ok([...st.versions].reverse().map(pub));
  if ((m = path.match(/^\/ontology\/versions\/(\d+)$/))) { const v = st.versions.find((x) => x.id === Number(m![1])); return v ? ok({ ...pub(v), yaml: yamlSnap(v.snap, v.id) }) : [404, { detail: "unknown version" }]; }
  if (path === "/ontology/diff") {
    const a = st.versions.find((x) => x.id === Number(q.get("from"))), b = st.versions.find((x) => x.id === Number(q.get("to")));
    return a && b ? ok(snapDiff(a.snap, b.snap)) : [404, { detail: "unknown version" }];
  }
  if (path === "/ontology/proposals" && method === "GET") return ok([...st.proposals].reverse().map(({ add: _a, ...r }) => r));
  if ((m = path.match(/^\/ontology\/proposals\/([\w-]+)\/(approve|reject)$/)) && method === "POST") {
    const p = st.proposals.find((x) => x.id === m![1]); if (!p) return [404, { detail: "unknown proposal" }];
    if (p.decision) return [409, { detail: "proposal already decided" }];
    const comment = String(body?.comment ?? "").trim();
    if (m[2] === "reject") { p.decision = "rejected"; p.approver = "user:admin"; p.decision_comment = comment; return ok({ decision: "rejected", version: null, yaml: null, notes: [] }); }
    if (p.diff.sensitive?.length && !comment) return [422, { detail: `this change widens permissions (${p.diff.sensitive.slice(0, 2).join("; ")}); a comment is required` }];
    if (p.breaking) return [422, { detail: "breaking changes cannot be applied (additive only): " + p.diff.breaking.join("; ") }];
    const last = st.versions[st.versions.length - 1], a = p.add ?? {};
    const snap: Snap = { types: [...last.snap.types, ...(a.types ?? [])], props: [...last.snap.props, ...(a.props ?? [])], links: [...last.snap.links, ...(a.links ?? [])], actions: [...last.snap.actions, ...(a.actions ?? [])], roles: [...last.snap.roles, ...(a.roles ?? [])] };
    const v: Ver = { id: last.id + 1, hash: hx(last.id + 1), parent_hash: last.hash, created_by: "user:admin", created_at: iso(nowMs()), source: "proposal", note: p.rationale, snap };
    st.versions.push(v); p.decision = "approved"; p.approver = "user:admin"; p.version_id = v.id; p.decision_comment = comment;
    return ok({ decision: "approved", version: pub(v), yaml: yamlSnap(snap, v.id), notes: ["Recorded. Restart the API to apply the new definition."] });
  }
  if ((m = path.match(/^\/ontology\/versions\/(\d+)\/rollback$/)) && method === "POST") {
    const t = st.versions.find((x) => x.id === Number(m![1])); if (!t) return [404, { detail: "unknown version" }];
    const last = st.versions[st.versions.length - 1];
    if (t.id === last.id) return [409, { detail: "that is already the current version" }];
    const v: Ver = { id: last.id + 1, hash: hx(last.id + 1), parent_hash: last.hash, created_by: "user:admin", created_at: iso(nowMs()), source: "rollback", note: `Rollback to version ${t.id}`, snap: t.snap };
    st.versions.push(v); return ok({ version: pub(v), yaml: yamlSnap(t.snap, v.id), notes: ["Columns and data remain (non-destructive). Restart the API to apply."] });
  }
  return null;
}

/* ------------------------------------------------------------------ auth ------------------------------------------------------------------ */
const USERS: Record<string, { pw: string; role: string }> = { alice: { pw: "pass", role: "planner" }, root: { pw: "admin", role: "admin" } };
export function login(body: any): R {
  const u = USERS[String(body?.user)];
  if (!u || u.pw !== body?.password) return fail(401, "invalid_credentials", "Wrong user or password.");
  return ok({ token: `v1.mock.${u.role}.${body.user}`, user: body.user, role: u.role, expires_at: iso(Date.now() + 12 * 3600000) });
}
export const tokenRole = (auth: string | undefined): { role: string; user: string } | null => { const m = /^Bearer v1\.mock\.(\w+)\.(\w+)$/.exec(auth ?? ""); return m ? { role: m[1], user: m[2] } : null; };
