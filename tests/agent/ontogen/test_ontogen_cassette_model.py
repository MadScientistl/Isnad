"""Cassette record/replay and the real AnthropicModel path against a stub client (NEVER run live)."""
import copy
import json

import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from ontogen_helpers import ok_report, profile_of, resolver, script, tid, widget_proposal

from agent.mock_model import MockModel, Turn
from agent.model_anthropic import AnthropicModel
from agent.ontogen import RecordingModel, generate, load_cassette
from agent.ontogen.cassette import Resolver


@pytest.fixture(scope="module")
def inj():
    return profile_of("injection")


def test_record_and_replay_round_trip(inj, tmp_path):
    inner = script(("get_profile", {"table_id": tid(inj, "widgets")}), ("submit_proposal", {"proposal": widget_proposal(inj)}))
    rec = RecordingModel(inner)
    r1 = generate(inj, model=rec, validate_fn=lambda p, pr: ok_report())
    path = tmp_path / "c.json"
    rec.save(path, name="t", description="d")
    data = json.loads(path.read_text())
    assert data["version"] == 1 and len(data["turns"]) == 2 and data["turns"][0]["tool_uses"][0]["name"] == "get_profile"
    r2 = generate(inj, model=load_cassette(path), validate_fn=lambda p, pr: ok_report())
    assert r2.ok and r2.yaml == r1.yaml and r2.cost_usd == r1.cost_usd and [e["kind"] for e in r2.events] == [e["kind"] for e in r1.events]


def test_placeholders_resolve_against_the_profile(inj):
    r = Resolver(inj, "h" * 8)
    t = tid(inj, "notes")
    assert r.sub("@tbl(notes.csv)") == t == r.sub("@tbl(NOTES)")
    long_header = next(c["name"] for tb in inj["tables"] if tb["name"] == "notes" for c in tb["columns"] if len(c["name"]) > 40)
    assert r.sub(f"@col(notes|{long_header})").startswith(t + ".c")
    assert r.walk({"a": ["@profile_hash", 3, {"b": "point(@col(widgets|price),@col(widgets|price))"}]})["a"][0] == "h" * 8
    with pytest.raises(KeyError):
        r.sub("@col(notes|no such column)")
    with pytest.raises(KeyError):
        r.sub("@tbl(nothing)")


def test_unsupported_version(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 2, "turns": []}))
    with pytest.raises(ValueError):
        load_cassette(p)


# ---- AnthropicModel with a stub client: key handling and the whole generate() loop. Never contacts the network.
class _Stream:
    def __init__(self, msg):
        self.msg, self.text_stream = msg, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class _Client:
    def __init__(self, msgs):
        self.msgs, self.kw = list(msgs), []
        self.messages = self

    def with_options(self, **kw):
        return self

    def stream(self, **kw):
        self.kw.append({**kw, "messages": copy.deepcopy(kw["messages"])})
        return _Stream(self.msgs.pop(0))


def _msg(*blocks, stop="tool_use"):
    return Message(id="m", type="message", role="assistant", model="claude-sonnet-5-5", stop_reason=stop, content=list(blocks),
                   usage=Usage(input_tokens=1000, output_tokens=200))


def test_anthropic_model_drives_generate_through_a_stub(inj, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-not-a-real-key")
    c = _Client([
        _msg(TextBlock(type="text", text="profiling"),
             ToolUseBlock(type="tool_use", id="u1", name="get_profile", input={"table_id": tid(inj, "widgets")})),
        _msg(ToolUseBlock(type="tool_use", id="u2", name="submit_proposal", input={"proposal": widget_proposal(inj)}))])
    m = AnthropicModel(client=c)
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    assert r.ok and r.turns == 2 and r.model == "claude-sonnet-5-5"
    assert r.cost_usd == pytest.approx(2 * (1000 * 2.0 + 200 * 10.0) / 1e6)        # priced from agent.pricing
    first = c.kw[0]
    assert {t["name"] for t in first["tools"]} == {"get_profile", "get_samples", "check_proposal", "submit_proposal"}
    assert first["max_tokens"] <= 16_000 and "UNTRUSTED" in first["system"]
    # the second request carries the tool_use / tool_result pair (valid alternation)
    msgs = c.kw[1]["messages"]
    assert [m_["role"] for m_ in msgs] == ["user", "assistant", "user"] and msgs[2]["content"][0]["type"] == "tool_result"


def test_anthropic_model_requires_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicModel()
