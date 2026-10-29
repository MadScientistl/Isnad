"""Model-client protocol and the value types shared by the runner and the model implementations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class ModelTurn:
    """One assistant turn. `content` is the list of content blocks (dicts) to replay in the next request."""
    content: list
    text: str = ""
    tool_uses: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)  # input_tokens, output_tokens, cache_read_input_tokens, ...


class ModelClient(Protocol):
    name: str

    def complete(self, *, system: str, messages: list, tools: list, max_tokens: int,
                 on_text: Callable[[str], None] | None = None, timeout: float | None = None) -> ModelTurn: ...
