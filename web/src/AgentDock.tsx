import { useCallback, useEffect, useRef, useState } from "react";
import { agentApi, money, startError } from "./agent";
import { useApp, usePolled } from "./store";
import { ConfigPanel, EXAMPLES, StatusBar, useRunStream } from "./pages/Agent";
import TranscriptView from "./Transcript";
import { Icon } from "./Icons";

/**
 * Agent chat docked beside every page. Each message starts one agent run (the backend has no conversation memory,
 * so follow-ups are independent runs); the thread is kept per role in sessionStorage and replayed from the stored run events.
 */
interface Turn { id: string; prompt: string; runId: string; at: number }
const key = (role: string) => `isnad.dock.${role}`;
const load = (role: string): Turn[] => { try { const v = JSON.parse(sessionStorage.getItem(key(role)) ?? "[]"); return Array.isArray(v) ? v : []; } catch { return []; } };
const save = (role: string, t: Turn[]) => { try { sessionStorage.setItem(key(role), JSON.stringify(t.slice(-20))); } catch { /* storage unavailable */ } };

function TurnView({ turn, caps, last }: { turn: Turn; caps?: Parameters<typeof StatusBar>[0]["caps"]; last: boolean }) {
  const { t, conn, error } = useRunStream(turn.runId);
  const running = !t.finished && !error;
  const [cancelling, setCancelling] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (last) ref.current?.scrollIntoView({ block: "end" }); }, [t.items.length, t.live, last]);
  return (
    <div className="turn" ref={ref}>
      <div className="bubble me">{turn.prompt}</div>
      <div className="bubble ai">
        {error && <div className="err" role="alert">{error}</div>}
        {conn === "retry" && <div className="warnbox">Connection lost, reconnecting…</div>}
        <TranscriptView t={t} running={running} />
        <div className="turnmeta">
          <span>{t.toolCalls} tool call{t.toolCalls === 1 ? "" : "s"}</span><span>{t.proposals} proposal{t.proposals === 1 ? "" : "s"}</span><span>{money(t.cost)}</span>
          {running && <button className="danger" disabled={cancelling} onClick={() => { setCancelling(true); agentApi.cancel(turn.runId).catch(() => setCancelling(false)); }}>{cancelling ? "Cancelling…" : "Stop"}</button>}
          {!running && <a className="lnk" href={`#/agent/${turn.runId}`}>Run detail</a>}
        </div>
        {caps && running && <StatusBar t={t} caps={caps} running={running} startedAt={turn.at} />}
      </div>
    </div>
  );
}

export function useDock() {
  const [open, setOpenS] = useState(() => { try { return localStorage.getItem("isnad.dock") === "1"; } catch { return false; } });
  const setOpen = useCallback((v: boolean) => { setOpenS(v); try { localStorage.setItem("isnad.dock", v ? "1" : "0"); } catch { /* ignore */ } setTimeout(() => dispatchEvent(new Event("resize")), 260); }, []);
  useEffect(() => {
    const o = () => setOpen(true);
    const k = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setOpenS((p) => { const n = !p; try { localStorage.setItem("isnad.dock", n ? "1" : "0"); } catch { /* ignore */ } setTimeout(() => dispatchEvent(new Event("resize")), 260); return n; }); } };
    addEventListener("isnad-dock-open", o); addEventListener("keydown", k);
    return () => { removeEventListener("isnad-dock-open", o); removeEventListener("keydown", k); };
  }, [setOpen]);
  return { open, setOpen };
}

export default function AgentDock({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { role, scenario } = useApp();
  const cfg = usePolled(agentApi.config, [role]);
  const [turns, setTurns] = useState<Turn[]>(() => load(role));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const end = useRef<HTMLDivElement>(null);
  const ta = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { setTurns(load(role)); setErr(undefined); }, [role]);
  useEffect(() => { if (open) setTimeout(() => ta.current?.focus(), 50); }, [open]);
  const max = cfg.data?.limits.prompt_max_chars ?? 4000;
  const unavailable = !!cfg.data && !cfg.data.available;
  const noAgent = !!cfg.error && /no_agent|403/.test(cfg.error);
  const send = async (p = text) => {
    const prompt = p.trim(); if (!prompt || busy) return;
    setBusy(true); setErr(undefined);
    try {
      const r = await agentApi.start(prompt);
      const next = [...turns, { id: r.run_id, prompt, runId: r.run_id, at: Date.now() }];
      setTurns(next); save(role, next); setText("");
    } catch (e) { setErr(startError(e)); } finally { setBusy(false); }
  };
  const clear = () => { setTurns([]); save(role, []); };
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [turns.length]);
  return (
    <aside className={`dock ${open ? "open" : ""}`} aria-label="Isnad agent chat" aria-hidden={!open} {...(!open ? { inert: "" as any } : {})}>
      <header className="dockhead">
        <span className="dockmark"><Icon name="spark" size={16} /></span>
        <div><b>Isnad agent</b><div className="small muted">acting as {role}{scenario ? ` · scenario #${scenario}` : ""}</div></div>
        <span className="sp" />
        {turns.length > 0 && <button className="ghost" onClick={clear} title="Clear this conversation from the panel (runs stay in the audit log)">Clear</button>}
        <button className="ghost icon" onClick={onClose} aria-label="Close agent chat"><Icon name="close" /></button>
      </header>
      <div className="dockbody">
        {noAgent && <div className="empty"><b>The {role} role has no agent</b><p>Switch to a role with an agent configured in the ontology.</p></div>}
        {cfg.error && !noAgent && <div className="err" role="alert">{cfg.error} <button onClick={cfg.reload}>Retry</button></div>}
        {unavailable && <div className="warnbox" role="alert">The agent is disabled: no model API key is configured on the server. History still works.</div>}
        {!turns.length && !noAgent && (
          <div className="dockhello">
            <h3>Ask about your data</h3>
            <p className="muted">The agent reads as you, shows every tool it uses, and can only <i>propose</i> actions. Nothing changes until a human approves.</p>
            <div className="chips">{(EXAMPLES[role] ?? []).map((x) => <button key={x} className="ex" disabled={unavailable || busy} onClick={() => send(x)}>{x}</button>)}</div>
          </div>
        )}
        {turns.map((t, i) => <TurnView key={t.id} turn={t} caps={cfg.data?.caps} last={i === turns.length - 1} />)}
        {cfg.data && <ConfigPanel cfg={cfg.data} />}
        <div ref={end} />
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
        {err && <div className="err" role="alert">{err}</div>}
        <div className="cbox">
          <textarea ref={ta} rows={1} maxLength={max} value={text} placeholder={noAgent ? "No agent for this role" : "Ask the agent…  (Enter to send, Shift+Enter for a new line)"} aria-label="Message the agent"
            disabled={unavailable || noAgent}
            onChange={(e) => { setText(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(160, e.target.scrollHeight) + "px"; }}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
          <button type="submit" className="send" disabled={busy || !text.trim() || unavailable || noAgent || !cfg.data} aria-label="Send"><Icon name="send" /></button>
        </div>
        <div className="small muted cfoot"><span>{text.length}/{max}</span><span>Ctrl/Cmd+K toggles this panel</span></div>
      </form>
    </aside>
  );
}
