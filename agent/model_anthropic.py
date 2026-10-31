"""Anthropic Messages API client (streaming). The key comes only from ANTHROPIC_API_KEY and is never logged.

Model: ISNAD_AGENT_MODEL (default claude-sonnet-5-5). Sonnet 5.5: thinking is adaptive by default (parameter
omitted), no sampling parameters, no forced tool_choice, effort set via output_config.
"""
from __future__ import annotations

import os

from .types import ModelTurn, ToolUse

DEFAULT_MODEL = "claude-sonnet-5-5"


class AnthropicModel:
    def __init__(self, model: str | None = None, *, effort: str = "medium", client=None):
        import anthropic
        self.name = model or os.environ.get("ISNAD_AGENT_MODEL") or DEFAULT_MODEL
        self.effort = effort
        if client is None:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=key)
        self.client = client

    def complete(self, *, system, messages, tools, max_tokens, on_text=None, timeout=None) -> ModelTurn:
        c = self.client.with_options(timeout=timeout) if timeout else self.client
        with c.messages.stream(model=self.name, max_tokens=max(1, int(max_tokens)), system=system, tools=tools,
                               messages=messages, output_config={"effort": self.effort},
                               cache_control={"type": "ephemeral"}) as stream:
            for t in stream.text_stream:
                if on_text:
                    on_text(t)
            final = stream.get_final_message()
        content = [b.model_dump(mode="json", exclude_none=True) for b in final.content]
        u = final.usage
        usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        return ModelTurn(
            content, "".join(b["text"] for b in content if b["type"] == "text"),
            [ToolUse(b["id"], b["name"], b.get("input") or {}) for b in content if b["type"] == "tool_use"],
            final.stop_reason or "end_turn", usage)
