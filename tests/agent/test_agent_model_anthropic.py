"""AnthropicModel without network: key handling and response normalisation via a stub client."""
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from agent.model_anthropic import DEFAULT_MODEL, AnthropicModel


class _Stream:
    def __init__(self, msg):
        self.msg, self.text_stream = msg, ["hel", "lo"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class _Client:
    def __init__(self, msg):
        self.msg, self.kw = msg, None
        self.messages = self

    def with_options(self, **kw):
        return self

    def stream(self, **kw):
        self.kw = kw
        return _Stream(self.msg)


def test_requires_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicModel()


def test_default_and_env_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("ISNAD_AGENT_MODEL", raising=False)
    assert AnthropicModel().name == DEFAULT_MODEL == "claude-sonnet-5-5"
    monkeypatch.setenv("ISNAD_AGENT_MODEL", "claude-opus-5-5")
    assert AnthropicModel().name == "claude-opus-5-5"


def test_complete_normalises_response():
    msg = Message(id="m", type="message", role="assistant", model="claude-sonnet-5-5", stop_reason="tool_use",
                  content=[TextBlock(type="text", text="hello"),
                           ToolUseBlock(type="tool_use", id="tu1", name="query_objects", input={"type": "Port"})],
                  usage=Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=3))
    c, got = _Client(msg), []
    t = AnthropicModel(client=c).complete(system="s", messages=[{"role": "user", "content": "x"}], tools=[],
                                          max_tokens=100, on_text=got.append, timeout=5)
    assert got == ["hel", "lo"] and t.text == "hello" and t.stop_reason == "tool_use"
    assert [(u.id, u.name, u.input) for u in t.tool_uses] == [("tu1", "query_objects", {"type": "Port"})]
    assert t.usage["input_tokens"] == 10 and t.usage["cache_read_input_tokens"] == 3
    assert t.content[1] == {"type": "tool_use", "id": "tu1", "name": "query_objects", "input": {"type": "Port"}}
    assert c.kw["model"] == "claude-sonnet-5-5" and c.kw["max_tokens"] == 100 and "temperature" not in c.kw
