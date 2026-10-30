"""Deterministic scripted model for CI (no network)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from .types import ModelTurn, ToolUse


@dataclass
class Turn:
    """One scripted assistant turn. tool_uses: [(tool_use_id, name, input_dict)]."""
    text: str = ""
    tool_uses: list = field(default_factory=list)
    usage: tuple = (100, 50)           # (input_tokens, output_tokens)
    stop_reason: str | None = None     # default: tool_use if tool_uses else end_turn
    hook: Callable | None = None       # called when the turn is requested (e.g. advance a fake clock)


class MockModel:
    name = "mock-model"

    def __init__(self, script):
        self.script = list(script)       # Turn objects, or callables (system, messages, tools) -> Turn
        self.calls: list[dict] = []      # what the "model" was sent, for assertions

    def complete(self, *, system, messages, tools, max_tokens, on_text=None, timeout=None) -> ModelTurn:
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools,
                           "max_tokens": max_tokens})
        if not self.script:
            raise RuntimeError("MockModel script exhausted")
        t = self.script.pop(0)
        if callable(t) and not isinstance(t, Turn):
            t = t(system, messages, tools)
        if t.hook:
            t.hook()
        if t.text and on_text:
            on_text(t.text)
        content = ([{"type": "text", "text": t.text}] if t.text else []) + [
            {"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in t.tool_uses]
        return ModelTurn(content, t.text, [ToolUse(i, n, inp) for i, n, inp in t.tool_uses],
                         t.stop_reason or ("tool_use" if t.tool_uses else "end_turn"),
                         {"input_tokens": t.usage[0], "output_tokens": t.usage[1]})
