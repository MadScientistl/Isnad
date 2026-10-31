"""Small tool loop for the ontology generator (any `agent.types.ModelClient`).

Same cap/cost/cancel pattern as `agent.runner.AgentRun`, kept separate on purpose. Caps: 8 turns, 30000 output tokens,
USD 1.00, wall clock; repair rounds (3, max 5) are counted by `generate`. Budgets are shared across repair rounds.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from agent.pricing import cost_usd

MAX_RESULT_CHARS = 24_000
PER_CALL_MAX_TOKENS = 16_000
HARD_MAX_REPAIR = 5


@dataclass(frozen=True)
class GenCaps:
    max_turns: int = 8
    max_output_tokens: int = 30_000
    max_cost_usd: float = 1.00
    max_repair_rounds: int = 3
    max_wall_s: float = 300.0

    def __post_init__(self):
        if not (0 <= self.max_repair_rounds <= HARD_MAX_REPAIR):
            object.__setattr__(self, "max_repair_rounds", max(0, min(HARD_MAX_REPAIR, self.max_repair_rounds)))

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Budget:
    caps: GenCaps
    clock: object = time.monotonic
    cancel: threading.Event = field(default_factory=threading.Event)
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    tool_calls: int = 0
    started: float = 0.0

    def __post_init__(self):
        self.started = self.clock()

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining_s(self) -> float:
        return self.caps.max_wall_s - self.elapsed()

    def exhausted(self) -> str | None:
        if self.elapsed() >= self.caps.max_wall_s:
            return "wall_clock"
        if self.tokens_out >= self.caps.max_output_tokens:
            return "output_tokens"
        if self.cost >= self.caps.max_cost_usd:
            return "cost"
        return None


@dataclass
class LoopOutcome:
    status: str                      # submitted | completed | capped | cancelled | error
    proposal: object = None          # Proposal object when status == submitted
    pending_results: list = field(default_factory=list)   # tool_result blocks of the final user message (not yet appended)
    cap: str | None = None
    error: str | None = None
    text: str = ""


def _bound(env: dict) -> str:
    s = json.dumps(env, default=str)
    if len(s) <= MAX_RESULT_CHARS:
        return s
    e = dict(env)
    e["data"], e["truncated"], e["error"] = None, True, "result too large"
    return json.dumps(e, default=str)


def run_rounds(model, system: str, messages: list, toolbox, budget: Budget, emit, *, schemas=None) -> LoopOutcome:
    """Run model turns until a proposal is submitted or a cap/cancel/error ends the loop. Mutates `messages`."""
    caps = budget.caps
    schemas = schemas or toolbox.schemas()
    last_text = ""
    while True:
        if budget.cancel.is_set():
            return LoopOutcome("cancelled", text=last_text)
        cap = budget.exhausted()
        if cap or budget.turns >= caps.max_turns:
            return LoopOutcome("capped", cap=cap or "turns", text=last_text)
        budget.turns += 1
        max_tok = max(1, min(PER_CALL_MAX_TOKENS, caps.max_output_tokens - budget.tokens_out))
        try:
            t = model.complete(system=system, messages=messages, tools=schemas, max_tokens=max_tok,
                               on_text=None, timeout=max(1.0, budget.remaining_s()))
        except Exception as e:  # noqa: BLE001 - model failure ends the run, never crashes the caller
            return LoopOutcome("error", error=f"model_error: {type(e).__name__}", text=last_text)
        tc = cost_usd(model.name, t.usage)
        budget.tokens_in += sum(t.usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                            "cache_creation_input_tokens"))
        budget.tokens_out += t.usage.get("output_tokens", 0)
        budget.cost += tc
        last_text = t.text or last_text
        emit("message", {"turn": budget.turns, "text": t.text[:2000], "stop_reason": t.stop_reason,
                         "tool_uses": [u.name for u in t.tool_uses]}, tokens_in=t.usage.get("input_tokens", 0),
             tokens_out=t.usage.get("output_tokens", 0), cost_usd=tc)
        messages.append({"role": "assistant", "content": t.content})
        if t.stop_reason == "refusal":
            return LoopOutcome("error", error="the model declined this request", text=last_text)
        if not t.tool_uses:
            if t.stop_reason == "max_tokens":
                return LoopOutcome("capped", cap="max_tokens", text=last_text)
            return LoopOutcome("completed", text=last_text)
        cap = budget.exhausted()
        if cap:
            return LoopOutcome("capped", cap=cap, text=last_text)
        results, submitted = [], None
        for tu in t.tool_uses:
            if budget.cancel.is_set():
                return LoopOutcome("cancelled", text=last_text)
            if submitted is not None:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                "content": json.dumps({"ok": False, "error": "not executed: proposal already submitted"})})
                continue
            budget.tool_calls += 1
            emit("tool_call", {"tool_call_id": tu.id, "name": tu.name, "input": _safe_input(tu.name, tu.input)})
            out = toolbox.call(tu.name, tu.input, tu.id)
            emit("tool_result", {"tool_call_id": tu.id, "name": tu.name, "ok": out.ok, "flags": out.flags,
                                 "result": out.envelope if (tu.name != "submit_proposal" or not out.ok) else {"ok": True}})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": _bound(out.envelope),
                            "is_error": not out.ok})
            if out.terminal:
                submitted = out.proposal
        if submitted is not None:
            return LoopOutcome("submitted", proposal=submitted, pending_results=results, text=last_text)
        messages.append({"role": "user", "content": results})


def _safe_input(name: str, inp) -> dict:
    """Trace form of a tool input: proposals are summarised, never stored twice."""
    if name in ("check_proposal", "submit_proposal") and isinstance(inp, dict):
        p = inp.get("proposal")
        return {"proposal": {"objectTypes": len((p or {}).get("objectTypes") or []) if isinstance(p, dict) else None,
                             "linkTypes": len((p or {}).get("linkTypes") or []) if isinstance(p, dict) else None}}
    return inp if isinstance(inp, dict) else {}
