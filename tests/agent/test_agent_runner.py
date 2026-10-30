"""Runner loop: caps, cancellation, errors, event stream, DbSink."""
import json

import pytest
from psycopg import errors as pgerr

from agent import Caps, DbSink, MemorySink, Turn
from agent.runner import AgentRun, stream_events
from agent import MockModel
from agent.pricing import cost_usd
from agent_helpers import COMP, ctx_for, principal, run_agent, tu
from engine.db.principal import OWNER, scoped

Q = lambda i: tu(i, "query_objects", type="Shipment", limit=1)


def test_completes_and_events_are_ordered(world, factory):
    res, sink, model, ctx = run_agent("planner", factory, [Turn(tool_uses=[Q("a")], text="looking"), Turn(text="done")])
    assert res.status == "completed" and res.summary == "done" and res.turns == 2 and res.tool_calls == 1
    seqs = [e["seq"] for e in sink.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert sink.kinds() == ["message", "tool_call", "tool_result", "message", "finished"]
    assert res.tokens_in == 200 and res.tokens_out == 100
    assert res.cost_usd == pytest.approx(2 * cost_usd(model.name, {"input_tokens": 100, "output_tokens": 50}))
    # second request carries assistant tool_use then the user tool_result, in order
    m = model.calls[1]["messages"]
    assert [x["role"] for x in m] == ["user", "assistant", "user"]
    assert m[2]["content"][0]["type"] == "tool_result" and m[2]["content"][0]["tool_use_id"] == "a"
    assert json.loads(m[2]["content"][0]["content"])["untrusted"] is True


def test_deltas_go_to_callback_not_sink(world, factory):
    got = []
    ctx = ctx_for("planner", factory)
    sink = MemorySink()
    AgentRun(ctx, MockModel([Turn(text="hello")]), sink).run("hi", on_event=got.append)
    assert [e["kind"] for e in got] == ["delta", "message", "finished"]
    assert "delta" not in sink.kinds()


def test_stream_events_generator(world, factory):
    run = AgentRun(ctx_for("planner", factory), MockModel([Turn(tool_uses=[Q("a")]), Turn(text="ok")]))
    stream = stream_events(run, "go")
    kinds = [e["kind"] for e in stream]
    assert kinds[-1] == "finished" and "tool_result" in kinds and stream.result.status == "completed"


def test_tool_call_cap(world, factory):
    res, sink, _m, ctx = run_agent("planner", factory, [Turn(tool_uses=[Q("a"), Q("b"), Q("c")]), Turn(text="x")],
                                   caps=Caps(max_tool_calls=2))
    assert res.status == "capped" and res.cap == "tool_calls" and res.tool_calls == 2
    assert sink.kinds().count("tool_result") == 2 and sink.kinds()[-1] == "finished"
    assert "capped" in res.summary


def test_turn_cap(world, factory):
    res, *_ = run_agent("planner", factory, [Turn(tool_uses=[Q(f"t{i}")]) for i in range(5)], caps=Caps(max_turns=2))
    assert res.status == "capped" and res.cap == "turns" and res.turns == 2


def test_output_token_cap(world, factory):
    res, sink, model, _ = run_agent("planner", factory, [Turn(tool_uses=[Q("a")], usage=(10, 150)), Turn(text="x")],
                                    caps=Caps(max_output_tokens=100))
    assert res.status == "capped" and res.cap == "output_tokens" and res.tool_calls == 0
    assert model.calls[0]["max_tokens"] == 100  # the per-call limit shrinks to the remaining budget


def test_cost_cap(world, factory):
    res, *_ = run_agent("planner", factory, [Turn(tool_uses=[Q("a")], usage=(500_000, 10)), Turn(text="x")],
                        caps=Caps(max_cost_usd=0.01))
    assert res.status == "capped" and res.cap == "cost" and res.tool_calls == 0 and res.cost_usd > 0.01


def test_wall_clock_cap(world, factory):
    t = [0.0]
    ctx = ctx_for("planner", factory, clock=lambda: t[0], caps=Caps(max_wall_s=10))
    script = [Turn(tool_uses=[Q("a")], hook=lambda: t.__setitem__(0, 50.0)), Turn(text="x")]
    res, *_ = run_agent("planner", factory, script, ctx=ctx)
    assert res.status == "capped" and res.cap == "wall_clock" and res.tool_calls == 0


def test_cancellation(world, factory):
    ctx = ctx_for("planner", factory)
    script = [Turn(tool_uses=[Q("a")]), Turn(tool_uses=[Q("b")], hook=ctx.cancel.set), Turn(text="x")]
    res, sink, *_ = run_agent("planner", factory, script, ctx=ctx)
    assert res.status == "cancelled" and res.tool_calls == 1 and sink.events[-1]["payload"]["status"] == "cancelled"
    pre = ctx_for("planner", factory)
    pre.cancel.set()
    res, *_ = run_agent("planner", factory, [Turn(text="x")], ctx=pre)
    assert res.status == "cancelled" and res.turns == 0


def test_model_error_ends_run_with_status_error(world, factory):
    res, sink, *_ = run_agent("planner", factory, [Turn(tool_uses=[Q("a")])])  # script then exhausted -> RuntimeError
    assert res.status == "error" and "exhausted" in res.error and sink.events[-1]["kind"] == "finished"


def test_refusal_is_error(world, factory):
    res, *_ = run_agent("planner", factory, [Turn(text="", stop_reason="refusal")])
    assert res.status == "error"


def test_max_tokens_without_tools_is_capped(world, factory):
    res, *_ = run_agent("planner", factory, [Turn(text="partial", stop_reason="max_tokens")])
    assert res.status == "capped" and "partial" in res.summary


def test_caps_default_from_yaml_and_contract():
    c = Caps.for_role(COMP.model.roles["planner"])
    assert (c.max_turns, c.max_tool_calls, c.max_output_tokens, c.max_cost_usd, c.max_wall_s) == (12, 25, 60000, 0.5, 120.0)


def test_tool_result_size_bounded(world, conn, factory):
    from agent_helpers import obj
    for i in range(50):
        obj(conn, "Event", {"kind": f"k{i}", "note": "z" * 290})
    res, sink, model, _ = run_agent("planner", factory, [
        Turn(tool_uses=[tu("a", "query_objects", type="Event", limit=50)]), Turn(text="x")])
    content = model.calls[1]["messages"][-1]["content"][0]["content"]
    assert len(content) <= 24_000 and json.loads(content)["truncated"] is True


def test_cost_unknown_model_priced_at_max():
    assert cost_usd("claude-future", {"output_tokens": 1_000_000}) >= cost_usd("claude-sonnet-5-5", {"output_tokens": 1_000_000})


# ------------------------------------------------------------------ DbSink
def rows(conn, principal_, sql, args=()):
    with conn.transaction():
        with scoped(conn, principal_):
            return conn.execute(sql, list(args)).fetchall()


def test_db_sink_rows_carry_human_identity_and_are_masked(world, conn, factory):
    p = principal("planner")
    sink = DbSink(factory, p)
    res, _s, _m, ctx = run_agent("planner", factory, [
        Turn(tool_uses=[tu("a", "query_objects", type="Supplier")], text="checking"),
        Turn(tool_uses=[tu("b", "traverse", start_id=str(world["emea_port"]))]),
        Turn(text="finished")], prompt="what suppliers", sink=sink)
    sink.close()
    assert res.status == "completed"
    run = rows(conn, OWNER, 'SELECT id, role, "user", prompt, model, caps FROM agent_run')
    assert len(run) == 1 and run[0][1:5] == ("planner", "pat", "what suppliers", "mock-model")
    assert str(run[0][0]) == ctx.run_id and run[0][5]["max_tool_calls"] == 25
    ev = rows(conn, OWNER, "SELECT kind, payload, tokens_in, tokens_out, cost_usd FROM agent_run_event ORDER BY seq")
    assert [e[0] for e in ev] == ["message", "tool_call", "tool_result", "message", "tool_call", "tool_result",
                                  "message", "finished"]
    sq = [e[1]["seq"] for e in ev]
    assert sq == sorted(set(sq))  # strictly increasing (deltas consume numbers but are not stored)
    assert sum(e[2] for e in ev) == 300 and sum(e[3] for e in ev) == 150 and sum(e[4] for e in ev) > 0
    blob = json.dumps([e[1] for e in ev if e[0] == "tool_result"])
    assert "NL" not in blob and "DE" not in blob and "country\": null" in blob
    assert "sk-ant" not in json.dumps([e[1] for e in ev]).lower()
    # owner of the run sees it; another role does not; admin sees it
    assert rows(conn, p, "SELECT count(*) FROM agent_run_event")[0][0] == 8
    assert rows(conn, principal("finance_analyst"), "SELECT count(*) FROM agent_run")[0][0] == 0
    assert rows(conn, principal("finance_analyst"), "SELECT count(*) FROM agent_run_event")[0][0] == 0
    assert rows(conn, principal("admin"), "SELECT count(*) FROM agent_run_event")[0][0] == 8
    # append-only, also for the table owner
    with pytest.raises(pgerr.Error):
        conn.execute("UPDATE agent_run_event SET kind='message'")
    conn.rollback()
    with pytest.raises(pgerr.Error):
        conn.execute("DELETE FROM agent_run")
    conn.rollback()


def test_db_sink_rejects_foreign_identity(world, factory):
    sink = DbSink(factory, principal("finance_analyst"))
    with pytest.raises(ValueError):
        sink.run_started("00000000-0000-0000-0000-000000000001", principal("planner"), "p", "m", {})
    sink.close()


def test_sink_failure_ends_run_as_error(world, factory):
    class Boom(MemorySink):
        def emit(self, event):
            raise OSError("disk full")
    res, *_ = run_agent("planner", factory, [Turn(text="hi")], sink=Boom())
    assert res.status == "error" and "OSError" in res.error


def test_failed_model_call_is_charged_an_estimate(world, factory):
    res, sink, *_ = run_agent("planner", factory, [Turn(tool_uses=[Q("a")])])   # 2nd call fails
    assert res.status == "error" and sink.events[-1]["kind"] == "finished"
    assert sink.events[-1]["cost_usd"] > 0
