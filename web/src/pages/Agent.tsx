import { useEffect, useRef, useState } from "react";
import { api, isNotFound, type Proposal } from "../api";
import { agentApi, applyEvent, emptyTranscript, money, startError, streamRun, type AgentConfig, type Run, type Transcript } from "../agent";
import { fmtT, usePolled, useApp } from "../store";
import TranscriptView, { StatusChip } from "../Transcript";
import { compareHref } from "../scenarios";

const EXAMPLES: Record<string, string[]> = {
  planner: ["Which shipments are exposed if the closed port stays closed? Propose a reroute or delay where it is justified.", "Which open events could affect shipments in transit?"],
  finance_analyst: ["Find suspicious invoices and flag the ones with clear anomalies.", "Which open invoices are unflagged but score high on anomalies?"],
  regional_ops_lead: ["What are the risks at EMEA ports right now?", "Which EMEA ports have open events that justify a closure?"],
  admin: ["What changed since yesterday?", "Summarise open risks across ports, shipments and invoices."],
};

/** Subscribe to a run: replays stored events then live, reconnecting from the last seq. */
function useRunStream(runId: string | null) {
  const [t, setT] = useState<Transcript>(emptyTranscript());
  const [conn, setConn] = useState<"idle" | "open" | "retry">("idle");
  const [error, setError] = useState<string>();
  useEffect(() => {
    setT(emptyTranscript()); setError(undefined);
    if (!runId) { setConn("idle"); return; }
    const ac = new AbortController();
    streamRun(runId, (e) => setT((p) => applyEvent(p, e)), ac.signal, setConn).then(() => setConn("idle"), (e) => { if (!ac.signal.aborted) setError(isNotFound(e) || e?.status === 404 ? "Run not found." : String(e.message ?? e)); });
    return () => ac.abort();
  }, [runId]);
  return { t, conn, error };
}

function StatusBar({ t, cfg, caps, running, onCancel, cancelling, startedAt }: { t: Transcript; cfg?: AgentConfig; caps?: AgentConfig["caps"]; running: boolean; onCancel?: () => void; cancelling?: boolean; startedAt?: number }) {
  const c = caps ?? cfg?.caps;
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (!running) return; const h = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(h); }, [running]);
  const el = startedAt ? Math.max(0, Math.round((now - startedAt) / 1000)) : null;
  const over = (v: number, max?: number) => (max && v >= max * 0.9 ? "bad-t" : "");
  return (
    <div className="statusbar" role="status" aria-label="Run status">
      <span className={over(t.turns, c?.max_turns)}>turns <b>{t.turns}</b>{c && `/${c.max_turns}`}</span>
      <span className={over(t.toolCalls, c?.max_tool_calls)}>tool calls <b>{t.toolCalls}</b>{c && `/${c.max_tool_calls}`}</span>
      <span>proposals <b>{t.proposals}</b></span>
      <span className={over(t.tokens_out, c?.max_output_tokens)}>tokens in <b>{t.tokens_in}</b> out <b>{t.tokens_out}</b>{c && `/${c.max_output_tokens}`}</span>
      <span className={over(t.cost, c?.max_cost_usd)}>cost <b>{money(t.cost)}</b>{c && `/${money(c.max_cost_usd)}`}</span>
      {el !== null && c && <span>time <b>{el}s</b>/{c.max_wall_s}s</span>}
      {running && onCancel && <button className="danger" disabled={cancelling} onClick={onCancel}>{cancelling ? "Cancelling…" : "Cancel"}</button>}
    </div>
  );
}

function ConfigPanel({ cfg }: { cfg: AgentConfig }) {
  return (
    <details className="card agentcfg">
      <summary><b>Agent for {cfg.role}</b> acting as {cfg.user} · {cfg.tools.length} tools · {cfg.actions.length} proposable actions</summary>
      <h4>Tools</h4>
      <div className="actions">{cfg.tools.map((x) => <code key={x} className="ref">{x}</code>)}</div>
      <h4>May propose</h4>
      {cfg.actions.length ? <table><thead><tr><th>Action</th><th>Params</th><th>Approval</th></tr></thead><tbody>
        {cfg.actions.map((a) => <tr key={a.name}><td><b>{a.name}</b><div className="muted small">{a.description}</div></td>
          <td className="small">{Object.entries(a.params).map(([k, v]) => `${k}: ${v}`).join(", ")}</td>
          <td>{a.requires_approval ? <span className="chip warn">approval by {a.approvers.join(", ") || "admin"}</span> : <span className="chip ok">none</span>}</td></tr>)}
      </tbody></table> : <p className="muted">No proposable actions.</p>}
      <h4>Limits</h4>
      <p className="small muted">Per run: {cfg.caps.max_turns} turns, {cfg.caps.max_tool_calls} tool calls, {cfg.caps.max_output_tokens} output tokens, {money(cfg.caps.max_cost_usd)}, {cfg.caps.max_wall_s}s.
        Per user: {cfg.limits.max_concurrent} concurrent, {cfg.limits.runs_per_minute} runs per minute, {money(cfg.limits.daily_usd)} per day.</p>
    </details>
  );
}

function Console() {
  const { role } = useApp();
  const cfg = usePolled(agentApi.config, [role]);
  const [prompt, setPrompt] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [startedAt, setStartedAt] = useState<number>();
  const { t, conn, error } = useRunStream(runId);
  const running = !!runId && !t.finished && !error;
  const max = cfg.data?.limits.prompt_max_chars ?? 4000;
  const start = async () => {
    setBusy(true); setErr(undefined);
    try { const r = await agentApi.start(prompt.trim()); setStartedAt(Date.now()); setCancelling(false); setRunId(r.run_id); }
    catch (e) { setErr(startError(e)); } finally { setBusy(false); }
  };
  const cancel = async () => { if (!runId) return; setCancelling(true); try { await agentApi.cancel(runId); } catch (e) { setErr(startError(e)); setCancelling(false); } };
  const unavailable = cfg.data && !cfg.data.available;
  const noAgent = cfg.error && /no_agent|403/.test(cfg.error);
  return (
    <div className="page agentpage">
      <h2>Agent console <a className="lnk small" href="#/agent/runs">run history</a></h2>
      {cfg.error && <div className="err">{noAgent ? `The ${role} role has no agent.` : cfg.error}</div>}
      {cfg.data && <ConfigPanel cfg={cfg.data} />}
      {unavailable && <div className="warnbox" role="alert">The agent is disabled: no model API key is configured on the server, so runs cannot start. Reading history still works.</div>}
      <form className="promptbox" onSubmit={(e) => { e.preventDefault(); if (prompt.trim() && !running) start(); }}>
        <label htmlFor="agent-prompt">Ask the {role} agent</label>
        <textarea id="agent-prompt" rows={3} maxLength={max} value={prompt} onChange={(e) => setPrompt(e.target.value)} disabled={!!unavailable || running}
          placeholder="Describe what to investigate. The agent reads as you and can only propose actions for approval." />
        <div className="promptfoot">
          <span className="muted small">{prompt.length}/{max}</span>
          <button type="submit" className="primary" disabled={busy || running || !prompt.trim() || !!unavailable || !cfg.data}>{busy ? "Starting…" : "Run agent"}</button>
        </div>
        <div className="examples"><span className="muted small">Examples:</span> {(EXAMPLES[role] ?? []).map((x) => <button type="button" key={x} className="ex" disabled={running || !!unavailable} onClick={() => setPrompt(x)}>{x}</button>)}</div>
      </form>
      {err && <div className="err" role="alert">{err}</div>}
      {error && <div className="err" role="alert">{error}</div>}
      {runId && <>
        <StatusBar t={t} cfg={cfg.data} running={running} onCancel={cancel} cancelling={cancelling} startedAt={startedAt} />
        {conn === "retry" && <div className="warnbox">Connection lost, reconnecting…</div>}
        <TranscriptView t={t} running={running} />
        <p className="small"><a className="lnk" href={`#/agent/${runId}`}>Open run detail</a></p>
      </>}
    </div>
  );
}

export function History() {
  const { role } = useApp();
  const q = usePolled(() => agentApi.runs(50), [role], 5000);
  return (
    <div className="page">
      <h2>Agent runs <a className="lnk small" href="#/agent">console</a></h2>
      <p className="muted small">Runs started by you as {role}.</p>
      {q.error && <div className="err">{q.error}</div>}
      {q.data && (q.data.length ? (
        <table><thead><tr><th>Started</th><th>Role</th><th>Prompt</th><th>Status</th><th>Turns</th><th>Tools</th><th>Tokens</th><th>Cost</th><th>Proposals</th></tr></thead><tbody>
          {q.data.map((r) => (
            <tr key={r.id}><td>{fmtT(r.started_at)}</td><td>{r.role}</td>
              <td className="prompt"><a className="lnk" href={`#/agent/${r.id}`}>{r.prompt.length > 90 ? r.prompt.slice(0, 90) + "…" : r.prompt}</a></td>
              <td><StatusChip s={r.status} /></td><td>{r.turns}</td><td>{r.tool_calls}</td><td>{r.tokens_in + r.tokens_out}</td><td>{money(r.cost_usd)}</td><td>{r.proposals}</td></tr>
          ))}</tbody></table>
      ) : <p className="muted">No runs yet.</p>)}
    </div>
  );
}

function Detail({ id }: { id: string }) {
  const { role } = useApp();
  const q = usePolled(() => agentApi.run(id), [id, role], 0);
  const running = q.data?.run.status === "running";
  const [t, setT] = useState<Transcript>(emptyTranscript());
  const [live, setLive] = useState(false);
  const [run, setRun] = useState<Run>();
  const [props, setProps] = useState<Proposal[]>([]);
  const gen = useRef(0);
  useEffect(() => {
    if (!q.data) return;
    setRun(q.data.run);
    if (q.data.run.status !== "running") { setT(q.data.events.reduce(applyEvent, emptyTranscript())); setLive(false); return; }
    const ac = new AbortController(); setT(emptyTranscript()); setLive(true);
    streamRun(id, (e) => setT((p) => applyEvent(p, e)), ac.signal).then(() => { if (!ac.signal.aborted) { setLive(false); agentApi.run(id).then((r) => setRun(r.run), () => {}); } }, () => setLive(false));
    return () => ac.abort();
    // eslint-disable-next-line
  }, [q.data?.run.id, running]);
  useEffect(() => { const g = ++gen.current; api.proposals("all").then((p) => g === gen.current && setProps(p), () => {}); }, [t.proposals, run?.status]);
  const execFor = (pid: string) => props.find((p) => p.id === pid)?.approval?.execution_id;
  const stateOf = (pid: string) => props.find((p) => p.id === pid)?.state ?? props.find((p) => p.id === pid)?.approval?.decision;
  const scnIds = [...new Set<number>([...(run?.scenario_ids ?? []), ...(run?.scenario_id ? [run.scenario_id] : []),
    ...t.items.flatMap((it) => (it.type === "call" && it.name === "scenario_open" && (it.result?.data as any)?.scenario_id ? [Number((it.result!.data as any).scenario_id)] : []))])];
  if (q.error && isNotFound(new Error(q.error))) return <div className="page"><div className="err">Run not found.</div><a className="lnk" href="#/agent/runs">Back to history</a></div>;
  return (
    <div className="page agentpage">
      <h2>Agent run <a className="lnk small" href="#/agent/runs">history</a></h2>
      {q.error && <div className="err">{q.error}</div>}
      {run && <>
        <div className="card">
          <p><StatusChip s={run.status} /> started {fmtT(run.started_at)} by {run.user} as {run.role} · model {run.model}</p>
          <p><b>Prompt:</b> {run.prompt}</p>
          {scnIds.length > 0 && <p><b>Scenario:</b> {scnIds.map((id) => <span key={id} style={{ marginRight: 10 }}><span className="chip scn">#{id}</span> <a className="lnk" href={compareHref(id)}>compare with main</a> <a className="lnk" href="#/scenarios">scenarios</a></span>)}
            <span className="muted small"> The agent worked inside this sandbox; main is only changed through approved proposals.</span></p>}
          {run.summary && <p><b>Summary:</b> {run.summary}</p>}
          {run.cap && <p className="muted">Stopped by cap: {run.cap}</p>}
          {run.error && <div className="err">{run.error}</div>}
          {run.proposal_ids.length > 0 && <div><b>Proposals:</b> <ul className="plain">{run.proposal_ids.map((pid) => {
            const ex = execFor(pid), st = stateOf(pid);
            return <li key={pid}><code className="ref">{pid.slice(0, 8)}</code> {st && <span className={`chip ${st === "approved" ? "ok" : st === "rejected" ? "bad" : "info"}`}>{st}</span>} {!st || st === "pending" ? <a className="lnk" href="#/approvals">approvals inbox</a> : null} {ex && <a className="lnk" href={`#/audit/${ex}`}>execution in audit</a>}</li>;
          })}</ul></div>}
        </div>
        <StatusBar t={{ ...t, tokens_in: run.status === "running" ? t.tokens_in : run.tokens_in, tokens_out: run.status === "running" ? t.tokens_out : run.tokens_out, cost: run.status === "running" ? t.cost : run.cost_usd, turns: run.status === "running" ? t.turns : run.turns, toolCalls: run.status === "running" ? t.toolCalls : run.tool_calls, proposals: run.status === "running" ? t.proposals : run.proposals }}
          caps={run.caps} running={live} onCancel={() => agentApi.cancel(id).catch(() => {})} />
        <TranscriptView t={t} running={live} />
      </>}
      {q.loading && !run && <p className="muted">Loading…</p>}
    </div>
  );
}

export default function AgentPage({ parts }: { parts: string[] }) {
  if (parts[1] === "runs") return <History />;
  if (parts[1]) return <Detail id={parts[1]} />;
  return <Console />;
}
