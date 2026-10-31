import { useMemo, useState } from "react";
import { ingestApi, type ItemEdit, type ProposalDetail, type ProposalProp } from "../api";
import { toast } from "../store";
import { ErrorBanner, Trunc } from "../ui";
import { errMsg } from "./common";

type Dec = { action: "accept" | "reject" | "edit"; value?: unknown };
const TYPE_RE = /^[A-Z][A-Za-z0-9]{0,40}$/, ID_RE = /^[a-z][a-z0-9_]{0,40}$/;
const PROP_TYPES = ["string", "int", "float", "bool", "timestamp", "point", "linestring", "geojson"];
const CARD = ["many-to-one", "one-to-one", "one-to-many", "many-to-many"];
const tname = (t: ProposalProp["type"]) => (typeof t === "string" ? t : `enum (${t.enum.length})`);

/** One accept/reject/edit control group for a row. */
function Row({ path, decs, setDec, label, meta, sensitive, locked, editor, children }: {
  path: string; decs: Map<string, Dec>; setDec: (p: string, d: Dec | null) => void; label: React.ReactNode; meta?: React.ReactNode;
  sensitive?: boolean; locked?: boolean; editor?: React.ReactNode; children?: React.ReactNode;
}) {
  const d = decs.get(path);
  const state = d?.action ?? "accept";
  const tip = locked ? "Add a comment for the sensitive items first" : undefined;
  return (
    <li className={`rv ${state}`}>
      <div className="rvrow">
        <span className="rvlabel">{label}{sensitive && <span className="chip restricted" title="Grants permissions: needs a comment">sensitive</span>}{meta && <span className="muted small"> {meta}</span>}</span>
        <span className="seg" role="group" aria-label={`Decision for ${path}`}>
          <button type="button" className={state === "accept" ? "on" : ""} aria-pressed={state === "accept"} disabled={locked} title={tip} onClick={() => setDec(path, null)}>Accept</button>
          <button type="button" className={state === "reject" ? "on danger" : ""} aria-pressed={state === "reject"} disabled={locked} title={tip} onClick={() => setDec(path, { action: "reject" })}>Reject</button>
          <button type="button" className={state === "edit" ? "on" : ""} aria-pressed={state === "edit"} disabled={locked || !editor} title={tip} onClick={() => setDec(path, d?.action === "edit" ? null : { action: "edit" })}>Edit</button>
        </span>
      </div>
      {state === "edit" && editor && <div className="rvedit">{editor}</div>}
      {state === "reject" && <div className="rvnote small muted">Will be removed from the proposal.</div>}
      {children}
    </li>
  );
}

function TextEdit({ label, path, current, decs, setDec, re, what }: { label: string; path: string; current: string; decs: Map<string, Dec>; setDec: (p: string, d: Dec | null) => void; re: RegExp; what: string }) {
  const v = (decs.get(path)?.value as string | undefined) ?? current;
  const bad = !re.test(v);
  return <label className="ed">{label}<input value={v} maxLength={41} aria-invalid={bad} onChange={(e) => setDec(path, { action: "edit", value: e.target.value })} />
    {bad && <small className="bad-t">Use {what}.</small>}</label>;
}

export default function ReviewStep({ det, comment, setComment, onApplied, onNext }: {
  det: ProposalDetail; comment: string; setComment: (c: string) => void; onApplied: (newPid: string) => void; onNext: () => void;
}) {
  const doc = det.proposal;
  const [sub, setSub] = useState<Map<string, Dec>>(new Map());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const decs = sub;
  const sensitive = det.diff.sensitive ?? [];
  const needComment = sensitive.length > 0;
  const commentOk = comment.trim().length > 0;
  const setDec = (p: string, d: Dec | null) => setSub((m) => {
    const n = new Map(m);
    // a rejected parent supersedes decisions on its children
    if (d) n.set(p, d); else n.delete(p);
    if (d?.action === "reject") for (const k of [...n.keys()]) if (k.startsWith(p + "/")) n.delete(k);
    return n;
  });
  const changes = useMemo(() => [...decs.entries()].filter(([p, d]) => !(d.action === "edit" && d.value === undefined) && ![...decs.entries()].some(([q, e]) => e.action === "reject" && p.startsWith(q + "/"))), [decs]);
  const invalid = useMemo(() => changes.some(([p, d]) => d.action === "edit" && p.endsWith("/name") && !(/^\/objectTypes\/\d+\/name$/.test(p) ? TYPE_RE : ID_RE).test(String(d.value))), [changes]);
  const sensitiveTouched = changes.some(([p]) => p.startsWith("/roles/"));
  const apply = async () => {
    setBusy(true); setErr(undefined);
    // The server applies items in order and list elements are addressed by index: edits first (they never shift indices),
    // then rejects from the highest index down, so one reject cannot move the target of another.
    const nums = (p: string) => p.split("/").filter(Boolean).map((x) => (/^\d+$/.test(x) ? Number(x) : -1));
    const cmp = (a: string, b: string) => { const x = nums(a), y = nums(b); for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return y[i] - x[i]; return y.length - x.length; };
    const items: ItemEdit[] = [
      ...changes.filter(([, d]) => d.action === "edit").map(([path, d]) => ({ path, action: d.action, value: d.value }) as ItemEdit),
      ...changes.filter(([, d]) => d.action === "reject").sort((a, b) => cmp(a[0], b[0])).map(([path]) => ({ path, action: "reject" }) as ItemEdit),
    ];
    try {
      const r = await ingestApi.patchItems(det.id, items);
      const nid = r.id;
      if (!nid) throw new Error("The server did not return the new proposal.");
      toast(`Applied ${items.length} change${items.length === 1 ? "" : "s"}; re-validated`); setSub(new Map()); onApplied(nid);
    } catch (e) { setErr(errMsg(e)); } finally { setBusy(false); }
  };
  const lockRoles = needComment && !commentOk;
  const ed = (path: string, key: string, cur: string, re: RegExp, what: string, label = "Name") => <TextEdit label={label} path={`${path}/${key}`} current={cur} decs={decs} setDec={setDec} re={re} what={what} />;
  const editVal = (path: string, cur: unknown) => (decs.get(path)?.value ?? cur);

  return (
    <div>
      {doc.rationale && <section className="card"><h3>Rationale</h3><p className="rationale"><Trunc max={1200}>{doc.rationale}</Trunc></p>{doc.notes.map((n, i) => <p key={i} className="small muted"><Trunc max={300}>{n}</Trunc></p>)}</section>}
      {(det.diff.breaking?.length ?? 0) > 0 && <div className="err" role="alert">Breaking changes against the existing ontology: {det.diff.breaking.join("; ")}</div>}

      <section className="card"><h3>Object types ({doc.objectTypes.length})</h3>
        {!doc.objectTypes.length && <p className="muted">No object types in this proposal.</p>}
        <ul className="plain rvlist">
          {doc.objectTypes.map((t, i) => { const p = `/objectTypes/${i}`; return (
            <Row key={i} path={p} decs={decs} setDec={setDec} label={<b><Trunc max={40}>{(decs.get(p + "/name")?.value as string) ?? t.name}</Trunc></b>} meta={<>from {t.source_table}, key {t.key.join(" + ")}{t.extends ? `, extends ${t.extends}` : ""}</>}
              editor={ed(p, "name", t.name, TYPE_RE, "letters and digits, starting with a capital")}>
              <ul className="plain rvlist sub">
                {t.properties.map((pr, j) => { const pp = `${p}/properties/${j}`; return (
                  <Row key={j} path={pp} decs={decs} setDec={setDec} label={<span><Trunc max={36}>{pr.name}</Trunc> <span className="chip">{tname(pr.type)}</span>{pr.required && <span className="chip info">required</span>}</span>}
                    meta={pr.column ? `column ${pr.column}` : pr.transform !== "none" ? pr.transform : ""}
                    editor={<div className="edgrid">
                      {ed(pp, "name", pr.name, ID_RE, "lowercase letters, digits and _")}
                      <label className="ed">Type<select disabled={typeof pr.type !== "string"} value={String(editVal(pp + "/type", typeof pr.type === "string" ? pr.type : "enum"))} onChange={(e) => setDec(pp + "/type", { action: "edit", value: e.target.value })}>
                        {typeof pr.type !== "string" && <option value="enum">enum</option>}{PROP_TYPES.map((x) => <option key={x}>{x}</option>)}</select></label>
                      <label className="ed chk"><input type="checkbox" checked={!!editVal(pp + "/required", pr.required)} onChange={(e) => setDec(pp + "/required", { action: "edit", value: e.target.checked })} /> Required</label>
                    </div>} />); })}
              </ul>
            </Row>); })}
        </ul>
      </section>

      <section className="card"><h3>Links ({doc.linkTypes.length})</h3>
        {!doc.linkTypes.length ? <p className="muted">No links.</p> : <ul className="plain rvlist">
          {doc.linkTypes.map((l, i) => { const p = `/linkTypes/${i}`; return (
            <Row key={i} path={p} decs={decs} setDec={setDec} label={<span><b><Trunc max={36}>{l.name}</Trunc></b> <span className="muted">{l.from} → {l.to}</span> <span className="chip">{String(editVal(p + "/cardinality", l.cardinality))}</span>{l.required && <span className="chip info">required</span>}{l.transitive && <span className="chip">transitive</span>}</span>}
              meta={l.source.kind === "fk" ? `from ${String(l.source.from_col)}` : "join table"}
              editor={<div className="edgrid">{ed(p, "name", l.name, ID_RE, "lowercase letters, digits and _")}
                <label className="ed">Cardinality<select value={String(editVal(p + "/cardinality", l.cardinality))} onChange={(e) => setDec(p + "/cardinality", { action: "edit", value: e.target.value })}>{CARD.map((x) => <option key={x}>{x}</option>)}</select></label>
                <label className="ed chk"><input type="checkbox" checked={!!editVal(p + "/required", l.required)} onChange={(e) => setDec(p + "/required", { action: "edit", value: e.target.checked })} /> Required</label></div>} />); })}
        </ul>}
      </section>

      <section className="card"><h3>Actions ({doc.actions.length})</h3>
        {!doc.actions.length ? <p className="muted">No custom actions. The five generic actions are always available.</p> : <ul className="plain rvlist">
          {doc.actions.map((a, i) => { const p = `/actions/${i}`; return (
            <Row key={i} path={p} decs={decs} setDec={setDec} label={<span><b><Trunc max={36}>{a.name}</Trunc></b> <span className="chip">{a.kind}</span> <span className="muted">on {a.type}</span></span>} meta={<Trunc max={80}>{a.description}</Trunc>}
              editor={ed(p, "name", a.name, ID_RE, "lowercase letters, digits and _")} />); })}
        </ul>}
      </section>

      <section className="card"><h3>Roles ({doc.roles.length}) <span className="chip restricted">sensitive</span></h3>
        <p className="small muted">Roles decide who can see and do what. Generated roles never use row filters or masks. Every decision here needs a comment, which is stored with the approval.</p>
        {needComment && <div className="warnbox"><b>This proposal grants permissions:</b><ul className="plain small">{sensitive.map((s, i) => <li key={i}><Trunc max={160}>{s}</Trunc></li>)}</ul></div>}
        <label className="ed wide">Comment (required for sensitive items)<textarea rows={2} value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} placeholder="Why these roles and permissions are right" aria-invalid={needComment && !commentOk} /></label>
        <ul className="plain rvlist">
          {doc.roles.map((r, i) => { const p = `/roles/${i}`; return (
            <Row key={i} path={p} decs={decs} setDec={setDec} sensitive locked={lockRoles} label={<span><b><Trunc max={36}>{r.name}</Trunc></b> <span className={`chip ${r.access === "read" ? "" : "warn"}`}>{String(editVal(p + "/access", r.access))}</span></span>} meta={<Trunc max={90}>{r.description}</Trunc>}
              editor={<div className="edgrid">{ed(p, "name", r.name, ID_RE, "lowercase letters, digits and _")}
                <label className="ed">Access<select value={String(editVal(p + "/access", r.access))} onChange={(e) => setDec(p + "/access", { action: "edit", value: e.target.value })}>{["read", "write", "admin"].map((x) => <option key={x}>{x}</option>)}</select></label></div>} />); })}
        </ul>
      </section>

      <ErrorBanner error={err} onRetry={apply} />
      <div className="stepfoot sticky">
        <span className="small muted">{changes.length ? `${changes.length} pending change${changes.length === 1 ? "" : "s"}` : "No changes pending"}</span>
        <button disabled={!changes.length || busy || invalid || (sensitiveTouched && !commentOk)} onClick={apply} title={invalid ? "Fix the invalid names first" : sensitiveTouched && !commentOk ? "Add a comment for the role changes" : undefined}>{busy ? "Applying…" : "Apply changes and re-validate"}</button>
        <button className="primary" onClick={onNext} disabled={changes.length > 0}>Next: validation</button>
        {changes.length > 0 && <span className="small muted">Apply or clear your changes to continue.</span>}
      </div>
    </div>
  );
}

