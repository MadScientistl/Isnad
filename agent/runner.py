"""Manual tool-use loop with caps, cancellation and an audit sink.

    ctx = AgentContext(make_principal(role, user, comp), comp, conn_factory)
    run = AgentRun(ctx, model_client, sink)
    result = run.run(prompt, on_event=callback)       # or: for ev in stream_events(run, prompt)

Event shape (also what the sink receives): {"seq": int, "kind": delta|message|tool_call|tool_result|denied|
proposal|finished, "payload": {...}, "tokens_in", "tokens_out", "cost_usd"}. Only `delta` is not persisted.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field

from .audit import MemorySink
from .policy import allowed_tools
from .pricing import cost_usd
from .prompt import build_system_prompt
from .tools import run_tool, safe_input, tool_schemas

MAX_TOOL_RESULT_CHARS = 24_000
PER_CALL_MAX_TOKENS = 16_000


@dataclass
class RunResult:
    run_id: str
    status: str                     # completed | capped | error | cancelled
    summary: str = ""
    turns: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    proposals: list = field(default_factory=list)   # proposal ids (strings)
    cap: str | None = None          # which cap ended a capped run
    error: str | None = None


def _bound(env: dict) -> str:
    s = json.dumps(env, default=str)
    if len(s) <= MAX_TOOL_RESULT_CHARS:
        return s
    e = dict(env)
    e["provenance"] = {"asserted": env["provenance"]["asserted"][:100], "inferred": env["provenance"]["inferred"][:50]}
    e["truncated"] = True
    s = json.dumps(e, default=str)
    if len(s) <= MAX_TOOL_RESULT_CHARS:
        return s
    e["data"], e["error"] = None, "result too large; narrow the query"
    return json.dumps(e, default=str)


class AgentRun:
    def __init__(self, ctx, model, sink=None, *, system: str | None = None):
        self.ctx, self.model, self.sink = ctx, model, sink or MemorySink()
        role = ctx.principal.role
        self.tools = allowed_tools(ctx.model, role)
        self.system = system or build_system_prompt(ctx.model, role, now=ctx.now().strftime("%Y-%m-%d %H:%M"))
        self._seq = 0
        self._on_event = None
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0

    # ---- events
    def _emit(self, kind, payload, **usage):
        self._seq += 1
        ev = {"seq": self._seq, "kind": kind, "payload": payload, "tokens_in": usage.get("tokens_in", 0),
              "tokens_out": usage.get("tokens_out", 0), "cost_usd": usage.get("cost_usd", 0.0)}
        self.sink.emit(ev)
        if self._on_event:
            self._on_event(ev)

    def _delta(self, text):
        self._seq += 1
        if self._on_event:
            self._on_event({"seq": self._seq, "kind": "delta", "payload": {"text": text}, "tokens_in": 0,
                            "tokens_out": 0, "cost_usd": 0.0})

    # ---- caps
    def _cap(self, *, before_call=False, before_tool=False):
        c, caps = self.ctx, self.ctx.caps
        if c.elapsed() >= caps.max_wall_s:
            return "wall_clock"
        if self.tokens_out >= caps.max_output_tokens:
            return "output_tokens"
        if self.cost >= caps.max_cost_usd:
            return "cost"
        if before_tool and c.tool_calls >= caps.max_tool_calls:
            return "tool_calls"
        return None

    def run(self, prompt: str, on_event=None) -> RunResult:
        ctx = self.ctx
        self._on_event = on_event
        res = RunResult(ctx.run_id, "running")
        last_text = ""
        try:
            self.sink.run_started(ctx.run_id, ctx.principal, prompt, self.model.name, ctx.caps.as_dict())
            messages = [{"role": "user", "content": prompt}]
            schemas = tool_schemas(self.tools)
            status, cap = None, None
            for turn_no in range(1, ctx.caps.max_turns + 1):
                if ctx.cancel.is_set():
                    status = "cancelled"; break
                cap = self._cap()
                if cap:
                    status = "capped"; break
                t = self.model.complete(
                    system=self.system, messages=messages, tools=schemas,
                    max_tokens=max(1, min(PER_CALL_MAX_TOKENS, ctx.caps.max_output_tokens - self.tokens_out)),
                    on_text=self._delta, timeout=max(1.0, ctx.remaining_s()))
                res.turns = turn_no
                tc = cost_usd(self.model.name, t.usage)
                self.tokens_in += sum(t.usage.get(k, 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                self.tokens_out += t.usage.get("output_tokens", 0)
                self.cost += tc
                last_text = t.text or last_text
                self._emit("message", {"turn": turn_no, "text": t.text, "stop_reason": t.stop_reason,
                                       "tool_uses": [u.name for u in t.tool_uses]},
                           tokens_in=t.usage.get("input_tokens", 0) + t.usage.get("cache_read_input_tokens", 0)
                           + t.usage.get("cache_creation_input_tokens", 0),
                           tokens_out=t.usage.get("output_tokens", 0), cost_usd=tc)
                messages.append({"role": "assistant", "content": t.content})
                if t.stop_reason == "refusal":
                    status, res.error = "error", "the model declined this request"; break
                if not t.tool_uses:
                    status, cap = ("capped", "max_tokens") if t.stop_reason == "max_tokens" else ("completed", None)
                    break
                cap = self._cap()
                if cap:
                    status = "capped"; break
                results, stop = [], None
                for tu in t.tool_uses:
                    if ctx.cancel.is_set():
                        stop = ("cancelled", None); break
                    cap = self._cap(before_tool=True)
                    if cap:
                        stop = ("capped", cap); break
                    ctx.tool_calls += 1
                    results.append(self._tool(tu, res))
                if stop:
                    status, cap = stop; break
                messages.append({"role": "user", "content": results})
            else:
                status, cap = "capped", "turns"
            res.status, res.cap = status, cap
        except Exception as e:  # noqa: BLE001 - model/sink failure: end the run, never crash the caller
            res.status, res.error = "error", f"{type(e).__name__}: {str(e)[:300]}"
        res.tool_calls, res.tokens_in, res.tokens_out, res.cost_usd = ctx.tool_calls, self.tokens_in, self.tokens_out, round(self.cost, 6)
        if res.status == "completed":
            res.summary = last_text
        else:
            why = res.cap or res.error or res.status
            res.summary = (f"[run {res.status}: {why}; {res.tool_calls} tool calls, {len(res.proposals)} proposals] "
                           + last_text).strip()
        try:
            self._emit("finished", {"status": res.status, "summary": res.summary[:4000], "turns": res.turns,
                                    "tool_calls": res.tool_calls, "proposals": res.proposals, "cap": res.cap,
                                    "error": res.error}, tokens_in=0, tokens_out=0, cost_usd=0.0)
        except Exception as e:  # noqa: BLE001
            res.error = res.error or f"audit sink failed: {type(e).__name__}"
            res.status = "error"
        finally:
            ctx.close()
        return res

    def _tool(self, tu, res: RunResult) -> dict:
        ctx = self.ctx
        self._emit("tool_call", {"tool_call_id": tu.id, "name": tu.name, "input": safe_input(ctx, tu.name, tu.input),
                                 "flags": []})
        out = run_tool(ctx, tu.name, tu.input, tu.id)
        if out.denied:
            self._emit("denied", {"tool_call_id": tu.id, "name": tu.name, **out.denied})
        self._emit("tool_result", {"tool_call_id": tu.id, "name": tu.name, "ok": out.ok, "result": out.envelope,
                                   "flags": out.flags})
        if out.proposal:
            res.proposals.append(out.proposal["proposal_id"])
            self._emit("proposal", {"tool_call_id": tu.id, **out.proposal})
        return {"type": "tool_result", "tool_use_id": tu.id, "content": _bound(out.envelope), "is_error": not out.ok}


class EventStream:
    """Iterable of events; `.result` (RunResult) is set once the iteration has finished."""

    def __init__(self, q, holder):
        self._q, self._h, self.result = q, holder, None

    def __iter__(self):
        while (e := self._q.get()) is not None:
            yield e
        self.result = self._h.get("result")


def stream_events(run: AgentRun, prompt: str) -> EventStream:
    """Generator form for A2 (SSE): runs the loop in a daemon thread and yields events as they happen."""
    q: queue.Queue = queue.Queue()
    holder: dict = {}

    def work():
        try:
            holder["result"] = run.run(prompt, on_event=q.put)
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()
    return EventStream(q, holder)
