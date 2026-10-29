import { useState } from "react";
import { type CallItem, type Transcript, type Item, type Provenance } from "./agent";

/** Everything here renders agent/tool text as plain text: no HTML, no markdown, no auto-links. */
const json = (x: unknown) => { try { return JSON.stringify(x, null, 2) ?? ""; } catch { return String(x); } };

function Prov({ p }: { p?: Provenance }) {
  if (!p) return null;
  const a = p.asserted ?? [], i = p.inferred ?? [];
  if (!a.length && !i.length) return null;
  return (
    <div className="prov">
      {a.length > 0 && <div><span className="chip ev-asserted">asserted</span> {a.slice(0, 8).map((r) => <code key={r} className="ref">{r}</code>)}{a.length > 8 && <span className="muted"> +{a.length - 8} more</span>}</div>}
      {i.map((x, k) => <div key={k}><span className="chip ev-inferred">inferred</span> <code className="ref">{x.edge}</code> <span className="muted">rule {x.rule}, support {x.support.length}</span></div>)}
    </div>
  );
}

function Call({ c }: { c: CallItem }) {
  const [open, setOpen] = useState(!!c.denied);
  const state = c.denied ? "denied" : c.result ? (c.result.ok ? "ok" : "failed") : "running";
  const flags = [...c.flags, ...(c.result?.flags ?? [])];
  return (
    <div className={`call ${c.denied ? "denied" : state === "failed" ? "failed" : ""}`}>
      <button className="callhead" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="caret" aria-hidden>{open ? "▾" : "▸"}</span> <b>{c.name}</b>
        <span className={`chip ${state === "ok" ? "ok" : state === "running" ? "info" : "bad"}`}>{state}</span>
        {c.result?.truncated && <span className="chip warn">truncated</span>}
        {flags.length > 0 && <span className="chip warn" title={flags.join(", ")}>⚠ injection flag: {flags.join(", ")}</span>}
      </button>
      {c.denied && <div className="err" role="alert"><b>Denied ({c.denied.kind}):</b> {c.denied.reason}</div>}
      {open && (
        <div className="callbody">
          <h4>Input</h4><pre>{json(c.input)}</pre>
          {c.result && <>
            <h4>Result{c.result.untrusted ? " (untrusted data)" : ""}</h4>
            <Prov p={c.result.provenance} />
            {c.result.error && <div className="err">{c.result.error}</div>}
            <pre>{json(c.result.data)}</pre>
          </>}
        </div>
      )}
    </div>
  );
}

function Prop({ it }: { it: Extract<Item, { type: "proposal" }> }) {
  return (
    <div className="propcard">
      <b>Proposed: {it.action}</b> <span className="chip info">pending approval</span>
      <pre>{json(it.params)}</pre>
      <a className="lnk" href="#/approvals">Open the approvals inbox</a>
    </div>
  );
}

const STATUS_CLS: Record<string, string> = { completed: "ok", running: "info", capped: "warn", cancelled: "warn", interrupted: "warn", error: "bad" };
export const StatusChip = ({ s }: { s: string }) => <span className={`chip ${STATUS_CLS[s] ?? ""}`}>{s}</span>;

export default function TranscriptView({ t, running }: { t: Transcript; running: boolean }) {
  return (
    <div className="transcript">
      <div aria-live="polite" aria-atomic="false">
        {t.items.map((it) => {
          if (it.type === "text") return <div key={it.seq} className="msg"><span className="who">agent</span><p>{it.text}</p></div>;
          if (it.type === "call") return <Call key={it.seq} c={it} />;
          if (it.type === "proposal") return <Prop key={it.seq} it={it} />;
          return (
            <div key={it.seq} className={`fin ${it.status}`}>
              <StatusChip s={it.status} /> {it.summary && <span>{it.summary}</span>}
              {it.cap && <span className="muted"> cap reached: {it.cap}</span>}
              {it.error && <div className="err">{it.error}</div>}
            </div>
          );
        })}
        {t.live && <div className="msg live"><span className="who">agent</span><p>{t.live}<span className="cursor" aria-hidden>▍</span></p></div>}
      </div>
      {running && !t.live && !t.finished && <p className="muted small">Working…</p>}
    </div>
  );
}
