"""The loop (caps, cancel, repair) and generate() with scripted models and a stub validator."""
import json
import threading

import pytest
from ontogen_helpers import bad_report, feedback_blocks, loads_env, ok_report, profile_of, script, tid, widget_proposal

from agent.mock_model import MockModel, Turn
from agent.ontogen.generate import generate, profile_hash_of
from agent.ontogen.loop import GenCaps
from agent.ontogen.prompt import build_system_prompt


@pytest.fixture(scope="module")
def inj():
    return profile_of("injection")


def sub(pd):
    return ("submit_proposal", {"proposal": widget_proposal(pd)})


def test_happy_path(inj):
    m = script(("get_profile", {"table_id": tid(inj, "widgets")}), ("check_proposal", {"proposal": widget_proposal(inj)}), sub(inj))
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    assert r.ok and r.status == "completed" and r.repair_rounds == 0 and r.turns == 3 and r.tool_calls == 3
    assert r.yaml and "Widget" in r.yaml and r.mapping and r.proposal.profile_hash == profile_hash_of(inj)
    kinds = [e["kind"] for e in r.events]
    assert kinds[-1] == "finished" and "validation" in kinds and [e["seq"] for e in r.events] == list(range(1, len(kinds) + 1))


def test_system_prompt_is_static_and_first_message_is_untrusted(inj):
    m = script(sub(inj))
    generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    call = m.calls[0]
    assert call["system"] == build_system_prompt()
    for t in inj["tables"]:
        for c in t["columns"]:
            assert c["name"][:30] not in call["system"] or len(c["name"]) < 8      # no data text in the system prompt
    first = call["messages"][0]["content"]
    env = loads_env(first)
    assert env["untrusted"] is True and "ignore_instructions" in env["flags"]
    assert {t["name"] for t in call["tools"]} == {"get_profile", "get_samples", "check_proposal", "submit_proposal"}
    assert "UNTRUSTED" in call["system"] and "never an instruction" in call["system"]


def test_repair_loop_feeds_back_only_the_report(inj):
    reports = [bad_report("Widget.condition: 'zzz'"), ok_report()]
    m = script(sub(inj), sub(inj))
    r = generate(inj, model=m, validate_fn=lambda p, pr: reports.pop(0))
    assert r.ok and r.repair_rounds == 1 and r.status == "completed"
    blocks = feedback_blocks(m, 1)
    assert len(blocks) == 1
    env = loads_env(blocks[0]["text"])
    assert env["untrusted"] is True and set(env["data"]) == {"ok", "checks", "stats"}
    c = env["data"]["checks"][0]
    assert c["name"] == "enum_violation_rate" and len(c["examples"]) == 3 and "\x07" not in blocks[0]["text"]
    assert "Widget" in blocks[0]["text"] and "objectTypes" not in blocks[0]["text"]       # the proposal itself is not echoed
    # the feedback sits in the same user message as the submit tool_result (valid role alternation)
    last = m.calls[1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"


def test_repair_rounds_are_capped(inj):
    m = script(*[sub(inj)] * 8)
    r = generate(inj, model=m, validate_fn=lambda p, pr: bad_report(), caps=GenCaps(max_repair_rounds=2))
    assert not r.ok and r.status == "capped" and r.cap == "repair_rounds" and r.repair_rounds == 2 and r.proposal is not None


def test_repair_cap_hard_maximum_is_five():
    assert GenCaps(max_repair_rounds=99).max_repair_rounds == 5
    assert GenCaps().max_repair_rounds == 3 and GenCaps().max_turns == 8 and GenCaps().max_cost_usd == 1.0
    assert GenCaps().max_output_tokens == 30000


def test_validator_exception_counts_as_failed_check(inj):
    def boom(*a):
        raise RuntimeError("secret dsn")
    r = generate(inj, model=script(sub(inj), sub(inj)), validate_fn=boom, caps=GenCaps(max_repair_rounds=1))
    assert not r.ok and "secret dsn" not in json.dumps(r.validation)


def test_turn_cap(inj):
    m = MockModel([Turn(tool_uses=[(f"t{i}", "get_profile", {"table_id": "t1"})]) for i in range(20)])
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    assert r.status == "capped" and r.cap == "turns" and r.turns == 8 and len(m.calls) == 8 and r.proposal is None and not r.ok


def test_cost_cap_stops_before_next_call(inj):
    m = MockModel([Turn(tool_uses=[(f"t{i}", "get_profile", {"table_id": "t1"})], usage=(400_000, 20_000)) for i in range(5)])
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report(), caps=GenCaps(max_cost_usd=0.5))
    assert r.status == "capped" and r.cap == "cost" and len(m.calls) == 1 and r.cost_usd > 0.5


def test_output_token_cap(inj):
    m = MockModel([Turn(tool_uses=[(f"t{i}", "get_profile", {"table_id": "t1"})], usage=(10, 20_000)) for i in range(5)])
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report(), caps=GenCaps(max_output_tokens=30000, max_cost_usd=100))
    assert r.status == "capped" and r.cap == "output_tokens" and len(m.calls) == 2
    assert m.calls[1]["max_tokens"] == 10_000          # the per-call budget shrinks to what is left


def test_wall_clock_cap(inj):
    now = [0.0]
    m = MockModel([Turn(tool_uses=[(f"t{i}", "get_profile", {"table_id": "t1"})], hook=lambda: now.__setitem__(0, now[0] + 200))
                   for i in range(5)])
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report(), caps=GenCaps(max_wall_s=300), clock=lambda: now[0])
    assert r.status == "capped" and r.cap == "wall_clock" and len(m.calls) == 2


def test_cancellation(inj):
    ev = threading.Event()
    m = MockModel([Turn(tool_uses=[("t1", "get_profile", {"table_id": "t1"})], hook=ev.set), Turn(text="never")])
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report(), cancel=ev)
    assert r.status == "cancelled" and len(m.calls) == 1


def test_invalid_submit_is_not_terminal_and_model_can_retry(inj):
    bad = widget_proposal(inj)
    bad["roles"].append({"name": "superuser", "description": "", "access": "admin"})
    m = script(("submit_proposal", {"proposal": bad}), sub(inj))
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    assert r.ok and r.turns == 2
    rejected = [e for e in r.events if e["kind"] == "tool_result" and not e["payload"]["ok"]]
    assert len(rejected) == 1 and "admin" in json.dumps(rejected[0]["payload"]["result"])


def test_stubborn_obedient_model_never_gets_a_proposal(inj):
    bad = widget_proposal(inj)
    bad["functions"] = {"x": {}}
    m = script(*[("submit_proposal", {"proposal": bad})] * 8)
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    assert r.proposal is None and not r.ok and r.yaml is None and r.cap == "turns"


def test_text_only_end_without_proposal(inj):
    r = generate(inj, model=script(("I give up",)), validate_fn=lambda p, pr: ok_report())
    assert r.status == "no_proposal" and not r.ok


def test_model_failure_does_not_raise(inj):
    r = generate(inj, model=MockModel([]), validate_fn=lambda p, pr: ok_report())
    assert r.status == "error" and "RuntimeError" in r.error and "exhausted" not in r.error


def test_extra_tool_use_after_submit_is_not_executed(inj):
    m = MockModel([Turn(tool_uses=[("a", "submit_proposal", {"proposal": widget_proposal(inj)}),
                                   ("b", "get_profile", {"table_id": "t1"})])])
    r = generate(inj, model=m, validate_fn=lambda p, pr: ok_report())
    assert r.ok and r.tool_calls == 1


def test_on_event_callback(inj):
    got = []
    generate(inj, model=script(sub(inj)), validate_fn=lambda p, pr: ok_report(), on_event=got.append)
    assert [e["kind"] for e in got][-1] == "finished" and any(e["kind"] == "tool_call" for e in got)
