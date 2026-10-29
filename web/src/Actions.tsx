import { useEffect, useState } from "react";
import { api, ApiError, paramList, type ActionInfo, type ParamMeta } from "./api";
import { toast, usePolled } from "./store";

type Obj = Record<string, any>;

/** Prefill a param from the object being viewed: a param named like the type (port/shipment/invoice), or `id`/`type`. */
function prefill(p: ParamMeta, type: string, id: string): string {
  const n = p.name.replace(/^\$/, "").toLowerCase();
  if (n === type.toLowerCase() || n === "id" || n === "object_id" || n === `${type.toLowerCase()}_id`) return id;
  if (n === "type" || n === "object_type") return type;
  if (p.ref && p.ref === type) return id;
  return "";
}

function initial(params: ParamMeta[], type: string, id: string): Record<string, string> {
  const v: Record<string, string> = {};
  for (const p of params) v[p.name] = p.type === "bool" ? "false" : p.type === "point" ? "" : prefill(p, type, id);
  return v;
}

/** Convert the form's strings to typed JSON params; throws a message for invalid input. */
function coerce(params: ParamMeta[], vals: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of params) {
    const raw = (vals[p.name] ?? "").trim();
    if (raw === "" && p.type !== "bool") { if (p.required) throw new Error(`${p.name} is required`); continue; }
    if (p.type === "int" || p.type === "float") { const n = Number(raw); if (Number.isNaN(n) || (p.type === "int" && !Number.isInteger(n))) throw new Error(`${p.name} must be a${p.type === "int" ? "n integer" : " number"}`); out[p.name] = n; }
    else if (p.type === "bool") out[p.name] = raw === "true";
    else if (p.type === "point") {
      const [lon, lat] = raw.split(",").map(Number);
      if ([lon, lat].some((x) => Number.isNaN(x) || x === undefined)) throw new Error(`${p.name} must be "lon, lat"`);
      out[p.name] = { type: "Point", coordinates: [lon, lat] };
    } else if (p.type === "timestamp") { const d = new Date(raw); if (Number.isNaN(d.getTime())) throw new Error(`${p.name} must be a valid timestamp`); out[p.name] = d.toISOString(); }
    else out[p.name] = raw;
  }
  return out;
}

function Field({ p, v, set }: { p: ParamMeta; v: string; set: (s: string) => void }) {
  const id = `f-${p.name}`;
  const hint = p.type === "point" ? "lon, lat" : p.type === "timestamp" ? "2026-01-20T12:00:00Z" : p.type;
  return (
    <label htmlFor={id}><span>{p.name}{p.required ? "*" : ""}</span>
      {p.type === "bool" ? <input id={id} type="checkbox" checked={v === "true"} onChange={(e) => set(String(e.target.checked))} />
        : <input id={id} value={v} placeholder={hint} inputMode={p.type === "int" || p.type === "float" ? "decimal" : undefined} onChange={(e) => set(e.target.value)} />}
      <small className="muted">{p.type}</small></label>
  );
}

export function ActionsPanel({ type, id, onDone }: { type: string; id: string; onDone: () => void }) {
  const acts = usePolled(() => api.actions(type, id), [type, id]);
  const [open, setOpen] = useState<ActionInfo | null>(null);
  return (
    <div className="card"><h3>Actions</h3>
      {acts.error && <div className="err">{acts.error}</div>}
      {acts.loading && !acts.data && <div className="muted">Loading…</div>}
      <div className="actions">
        {acts.data?.map((a) => (
          <button key={a.name} disabled={!a.allowed} title={a.allowed ? a.description + (a.requires_approval ? " (requires approval)" : "") : (a.reason ?? "Not allowed for this role")}
            className={a.requires_approval ? "approval" : ""} onClick={() => setOpen(a)}>
            {a.name}{a.requires_approval && <small> ⏳</small>}
          </button>))}
      </div>
      {acts.data && !acts.data.length && <span className="muted">No actions apply to this object.</span>}
      {acts.data?.some((a) => !a.allowed) && <p className="muted small">Greyed-out actions are not available to this role; hover for the reason.</p>}
      {open && <ActionDialog act={open} type={type} id={id} onClose={() => setOpen(null)} onDone={() => { setOpen(null); acts.reload(); onDone(); }} />}
    </div>
  );
}

function ActionDialog({ act, type, id, onClose, onDone }: { act: ActionInfo; type: string; id: string; onClose: () => void; onDone: () => void }) {
  const params = paramList(act.params, act.name);
  const [vals, setVals] = useState(() => initial(params, type, id));
  const [rationale, setRationale] = useState("");
  const [err, setErr] = useState<string>();
  const [failures, setFailures] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { const f = (e: KeyboardEvent) => e.key === "Escape" && onClose(); addEventListener("keydown", f); return () => removeEventListener("keydown", f); }, [onClose]);
  const submit = async () => {
    setErr(undefined); setFailures([]);
    let p: Record<string, unknown>;
    try { p = coerce(params, vals); } catch (e: any) { setErr(e.message); return; }
    if (act.requires_approval && !rationale.trim()) { setErr("A rationale is required to propose this action."); return; }
    setBusy(true);
    try {
      const r = act.requires_approval ? await api.propose(act.name, p, rationale) : await api.execute(act.name, p);
      if (r.status === 200 && r.data.action_id) toast(`${act.name} executed`, `#/audit/${r.data.action_id}`);
      else toast(`${act.name}: proposal pending approval`, "#/approvals", "warn");
      onDone();
    } catch (e: any) {
      if (e instanceof ApiError) {
        if (e.status === 403) setErr(`Permission denied: ${e.text}`);
        else if (e.status === 404) setErr("Not found.");
        else if (e.status === 422 && e.code === "criteria_failed") { setErr("Criteria failed:"); setFailures((e.payload.failures ?? []).map((f: any) => (typeof f === "string" ? f : f.message))); }
        else setErr(e.status === 422 ? `Invalid input: ${e.text}` : e.message);
      } else setErr(String(e.message ?? e));
    } finally { setBusy(false); }
  };
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label={act.name} onClick={onClose}>
      <div className="dlg" onClick={(e) => e.stopPropagation()}>
        <h3>{act.name}</h3><p className="muted">{act.description}</p>
        {act.requires_approval && <div className="warnbox">This action needs approval: it will be proposed and a different user with an approver role must approve it.</div>}
        <div className="form">{params.map((p) => <Field key={p.name} p={p} v={vals[p.name] ?? ""} set={(s) => setVals({ ...vals, [p.name]: s })} />)}
          {act.requires_approval && <label><span>Rationale*</span><textarea value={rationale} onChange={(e) => setRationale(e.target.value)} rows={2} /></label>}</div>
        {err && <div className="err" role="alert">{err}{failures.length > 0 && <ul>{failures.map((f, i) => <li key={i}>{f}</li>)}</ul>}</div>}
        <p><button className="primary" disabled={busy} onClick={submit}>{act.requires_approval ? "Propose for approval" : "Execute"}</button> <button onClick={onClose}>Cancel</button></p>
      </div>
    </div>
  );
}

export const Restricted = () => <span className="chip restricted" title="Hidden from your role">restricted</span>;
export type { Obj };
