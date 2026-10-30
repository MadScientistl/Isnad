import type { OntologyDiff } from "./api";
import { Trunc } from "./ui";

const KIND: Record<string, string> = { objectTypes: "Object types", properties: "Properties", linkTypes: "Links", actions: "Actions", roles: "Roles", functions: "Functions", webhooks: "Webhooks" };
const fmt = (v: unknown) => (v === null || v === undefined ? "none" : typeof v === "string" ? v : JSON.stringify(v));

/** Renders an engine.versioning diff: added / removed / changed, then breaking and sensitive findings. Text only. */
export default function OntDiff({ diff }: { diff: OntologyDiff }) {
  const add = Object.entries(diff.added ?? {}), rem = Object.entries(diff.removed ?? {}), chg = Object.entries(diff.changed ?? {});
  const empty = !add.length && !rem.length && !chg.length;
  return (
    <div className="ontdiff">
      {empty && <p className="muted">No structural differences.</p>}
      {add.map(([k, names]) => <div key={"a" + k} className="dgroup add"><b>{KIND[k] ?? k} added</b><ul className="plain">{names.map((n) => <li key={n}><span className="chip op-create">added</span> <Trunc max={60}>{n}</Trunc></li>)}</ul></div>)}
      {rem.map(([k, names]) => <div key={"r" + k} className="dgroup rem"><b>{KIND[k] ?? k} removed</b><ul className="plain">{names.map((n) => <li key={n}><span className="chip op-delete">removed</span> <Trunc max={60}>{n}</Trunc></li>)}</ul></div>)}
      {chg.map(([k, byName]) => <div key={"c" + k} className="dgroup chg"><b>{KIND[k] ?? k} changed</b><ul className="plain">
        {Object.entries(byName).map(([n, fields]) => <li key={n}><span className="chip op-modify">changed</span> <Trunc max={60}>{n}</Trunc>
          <ul className="plain sub">{Object.entries(fields).map(([f, [o, nw]]) => <li key={f} className="small"><code className="ref">{f}</code> <del><Trunc max={60}>{fmt(o)}</Trunc></del> → <ins><Trunc max={60}>{fmt(nw)}</Trunc></ins></li>)}</ul></li>)}</ul></div>)}
      {(diff.breaking?.length ?? 0) > 0 && <div className="dgroup brk" role="alert"><b>Breaking</b> <span className="small muted">rejected by the additive-only rule</span><ul className="plain">{diff.breaking.map((b, i) => <li key={i}><span className="chip bad">breaking</span> <Trunc max={200}>{b}</Trunc></li>)}</ul></div>}
      {(diff.sensitive?.length ?? 0) > 0 && <div className="dgroup sens"><b>Sensitive</b> <span className="small muted">widens permissions; approval needs a comment</span><ul className="plain">{diff.sensitive.map((b, i) => <li key={i}><span className="chip restricted">sensitive</span> <Trunc max={200}>{b}</Trunc></li>)}</ul></div>}
    </div>
  );
}
