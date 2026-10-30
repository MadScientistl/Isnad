"""Phase 5 agent scenario tools: permission intersection, the scenario id is never a parameter, caps, foreign
scenarios, nothing executes on main, audit rows carry the scenario id, reads are isolated."""
import sys
import uuid

import pytest

from agent import MemorySink, allowed_tools
from agent import tools as T
import agent_helpers
from agent_helpers import COMP, T0, principal, results, run_agent, tu
from engine import scenarios
from engine.actions import execute, make_principal
from engine.db import bitemporal as bt
from engine.db.principal import scoped
from datetime import UTC, datetime


_CTXS = []


def ctx_for(*a, **kw):
    c = agent_helpers.ctx_for(*a, **kw)
    _CTXS.append(c)
    return c


@pytest.fixture(autouse=True)
def _close_contexts(aschema):          # depends on aschema so it is torn down BEFORE the schema is dropped
    yield
    while _CTXS:
        _CTXS.pop().close()


def call(ctx, tool, call_id="c1", **inp):
    return T.run_tool(ctx, tool, inp, call_id)


def opened(role, factory, world, seen=("s_emea", "s_apac", "emea_port", "apac_port")):
    ctx = ctx_for(role, factory)
    assert call(ctx, "scenario_open", "o1", name="what-if").ok
    for k in seen:
        ctx.see_id(str(world[k]))
    return ctx


def main_row(conn, ship):
    conn.rollback()
    return bt.get_object(conn, "Shipment", ship)


def audit(conn, **where):
    conn.rollback()
    q = "SELECT name, actor, source, status, scenario_id, run_id FROM audit_event"
    w = " AND ".join(f"{k} = %s" for k in where)
    return conn.execute(q + (f" WHERE {w}" if w else "") + " ORDER BY seq", list(where.values())).fetchall()


# ------------------------------------------------------------------ availability per role
def test_tools_per_role():
    sc = ("scenario_open", "scenario_apply", "scenario_simulate", "scenario_compare")
    for role in ("planner", "regional_ops_lead", "admin"):
        assert set(sc) <= set(allowed_tools(COMP, role))
    fin = allowed_tools(COMP, "finance_analyst")
    assert "scenario_open" in fin and "scenario_compare" in fin
    assert "scenario_apply" not in fin and "scenario_simulate" not in fin


def test_scenario_id_is_never_a_tool_parameter():
    for name, spec in T.TOOLS.items():
        props = spec.input_model.model_json_schema().get("properties", {})
        assert not [p for p in props if "scenario" in p.lower()], name


def test_scenario_id_param_rejected(world, factory):
    ctx = opened("planner", factory, world)
    o = call(ctx, "query_objects", type="Shipment", scenario_id=999)
    assert not o.ok and o.envelope["error"].startswith("invalid input")
    o = call(ctx, "scenario_compare", scenario_id=1)
    assert not o.ok and o.envelope["error"].startswith("invalid input")
    assert ctx.sid == ctx.scenario_ids[0]


# ------------------------------------------------------------------ open / envelope / cap
def test_open_activates_scenario_and_envelope_carries_it(world, factory):
    ctx = ctx_for("planner", factory)
    before = call(ctx, "query_objects", "q0", type="Port")
    assert "scenario" not in before.envelope
    o = call(ctx, "scenario_open", "o1", name="Reroute test")
    assert o.ok and o.envelope["data"]["owner"] == "pat" and ctx.sid == o.envelope["data"]["scenario_id"]
    q = call(ctx, "query_objects", "q1", type="Port")
    assert q.envelope["scenario"]["id"] == ctx.sid and q.envelope["scenario"]["name"] == "Reroute test"


def test_per_run_cap(world, factory):
    ctx = ctx_for("planner", factory)
    outs = [call(ctx, "scenario_open", f"o{i}", name=f"s{i}") for i in range(4)]
    assert [o.ok for o in outs] == [True, True, True, False]
    assert "limit" in outs[3].envelope["error"] and len(ctx.scenario_ids) == 3


def test_scenarios_owned_by_launching_human_and_tagged_with_run(world, factory, conn):
    ctx = opened("planner", factory, world)
    s = scenarios.get(conn, principal("planner"), ctx.sid)
    assert s["owner_user"] == "pat" and s["run_id"] == ctx.run_id


# ------------------------------------------------------------------ apply: isolation, intersection, audit
def test_apply_executes_in_scenario_only(world, factory, conn):
    ctx = opened("planner", factory, world)
    o = call(ctx, "scenario_apply", "a1", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 12.0, "reason": "what-if"})
    assert o.ok, o.envelope
    assert o.envelope["data"]["executed_on"] == "scenario only"
    # inside the scenario the change is visible to the agent's reads ...
    q = call(ctx, "query_objects", "q1", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    assert q.envelope["data"]["objects"][0]["props"]["delay_hours"] == 12.0
    # ... main (and a second run without a scenario) is unchanged
    assert (main_row(conn, world["s_emea"]).get("delay_hours") or 0) == 0
    other = ctx_for("planner", factory)
    q2 = call(other, "query_objects", "q2", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    assert (q2.envelope["data"]["objects"][0]["props"]["delay_hours"] or 0) == 0
    rows = audit(conn, name="delay_shipment")
    assert len(rows) == 1 and rows[0][4] == ctx.sid and rows[0][2] == "agent" and rows[0][3] == "executed"
    assert rows[0][5] == ctx.run_id
    assert [r for r in audit(conn) if r[4] == 0 and r[3] == "executed"] == []


def test_apply_requires_active_scenario(world, factory):
    ctx = ctx_for("planner", factory)
    ctx.see_id(str(world["s_emea"]))
    o = call(ctx, "scenario_apply", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 1.0, "reason": "x"})
    assert not o.ok and "no active scenario" in o.envelope["error"]


@pytest.mark.parametrize("action", ["ship_arrive", "update_object", "delete_object", "pay_invoice"])
def test_apply_outside_allowlist_denied_with_scenario_id(world, factory, action, conn):
    ctx = opened("planner", factory, world)
    o = call(ctx, "scenario_apply", "a1", action=action, params={})
    assert not o.ok and o.denied["kind"] == "action" and o.denied["scenario_id"] == ctx.sid
    assert o.envelope["scenario"]["id"] == ctx.sid
    assert [r for r in audit(conn) if r[3] == "executed"] == []


def test_lead_cannot_apply_planner_actions_in_scenario(world, factory, conn):
    ctx = opened("regional_ops_lead", factory, world)
    o = call(ctx, "scenario_apply", "a1", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 3.0, "reason": "x"})
    assert not o.ok and o.denied == {"kind": "action", "name": "delay_shipment", "reason": o.denied["reason"],
                                     "scenario_id": ctx.sid}
    assert [r for r in audit(conn) if r[3] == "executed"] == []


def test_finance_scenario_apply_is_denied_as_tool(world, factory):
    ctx = opened("finance_analyst", factory, world, seen=())
    o = call(ctx, "scenario_apply", "a1", action="flag_invoice", params={"invoice": str(world["inv"]), "anomaly": "x"})
    assert not o.ok and o.denied["kind"] == "tool" and o.denied["scenario_id"] == ctx.sid
    o = call(ctx, "scenario_simulate", "a2", days=1)
    assert not o.ok and o.denied["kind"] == "tool"


def test_apply_intersection_rls_hidden_object_is_not_found(world, factory, conn):
    """Allowed action, but the object is outside the lead's RLS: not found, nothing written."""
    ctx = opened("regional_ops_lead", factory, world)
    ctx.see_id(str(world["apac_port"]))        # even if the id leaked into the run, the engine decides
    o = call(ctx, "scenario_apply", "a1", action="close_port",
             params={"port": str(world["apac_port"]), "reason": "x", "duration_h": 5.0})
    assert not o.ok
    assert [r for r in audit(conn) if r[3] == "executed"] == []


def test_apply_uuid_params_must_have_been_seen(world, factory):
    ctx = opened("planner", factory, world, seen=())
    o = call(ctx, "scenario_apply", "a1", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 1.0, "reason": "x"})
    assert not o.ok and "not retrieved by this run" in o.envelope["error"]
    o = call(ctx, "scenario_apply", "a2", action="delay_shipment",
             params={"shipment": str(uuid.uuid4()), "delay_hours": 1.0, "reason": "x"})
    assert not o.ok


def test_apply_unknown_params_rejected(world, factory):
    ctx = opened("planner", factory, world)
    o = call(ctx, "scenario_apply", "a1", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 1.0, "reason": "x", "scenario": 7})
    assert not o.ok and "unknown params" in o.envelope["error"]


# ------------------------------------------------------------------ foreign scenarios
def test_foreign_scenario_is_unreachable(world, factory, conn):
    admin = principal("admin")
    foreign = scenarios.create(conn, admin, "admin what-if", ontology=COMP)
    execute(conn, "delay_shipment", {"shipment": world["s_emea"], "delay_hours": 777.5, "reason": "foreign"}, admin,
            valid_at=datetime.now(UTC), scenario_id=foreign["id"], ontology=COMP)
    ctx = ctx_for("planner", factory)
    ctx.see_id(str(world["s_emea"]))
    # not active, no tool accepts an id: reading, comparing and applying all stay on main / refuse
    q = call(ctx, "query_objects", "q1", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    assert "777.5" not in str(q.envelope)
    assert not call(ctx, "scenario_compare", "c1").ok
    assert not call(ctx, "query_objects", "q2", type="Shipment", scenario_id=foreign["id"]).ok
    # the server-side resolver also refuses it for the planner (hidden = missing)
    with pytest.raises(scenarios.ScenarioNotFound):
        scenarios.resolve(conn, principal("planner"), foreign["id"])
    # and an own scenario never shows the foreign one's data
    assert call(ctx, "scenario_open", "o1", name="mine").ok
    q = call(ctx, "query_objects", "q3", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    assert "777.5" not in str(q.envelope) and ctx.sid != foreign["id"]


def test_agent_principal_writes_launchers_scenario_as_agent_on_behalf(world, factory, conn):
    """The agent identity (agent:<role>, on_behalf_of = launcher) writes the launcher's scenario; audit says so."""
    ctx = opened("planner", factory, world)
    assert ctx.agent_principal.user == "agent:planner" and ctx.agent_principal.on_behalf_of == ctx.principal.user
    assert call(ctx, "scenario_apply", "a1", action="delay_shipment",
                params={"shipment": str(world["s_emea"]), "delay_hours": 3.0, "reason": "x"}).ok
    rows = conn.execute("SELECT actor, on_behalf_of, source FROM audit_event WHERE scenario_id = %s AND "
                        "status='executed'", [ctx.sid]).fetchall()
    conn.rollback()
    assert rows == [("agent:planner", ctx.principal.user, "agent")]


@pytest.mark.parametrize("role,action,params", [
    ("finance_analyst", "delay_shipment", lambda w: {"shipment": w["s_emea"], "delay_hours": 1.0, "reason": "x"}),
    ("regional_ops_lead", "reroute_shipment", None),
])
def test_engine_denies_agent_principal_what_the_role_cannot_execute(world, factory, conn, role, action, params):
    """Defence in depth: even with the agent code bypassed, the engine permission check on the agent principal
    (role of the launcher) refuses actions the launcher's role cannot execute."""
    from engine.actions import PermissionDenied
    ctx = ctx_for(role, factory)
    d = scenarios.create(conn, ctx.agent_principal, "x", run_id=ctx.run_id, ontology=COMP)
    ctx.see_id(str(world["s_emea"]))
    p = params(world) if params else {"shipment": world["s_emea"], "new_port": world["apac_port"], "reason": "x"}
    with pytest.raises((PermissionDenied, ValueError)):
        execute(conn, action, p, ctx.agent_principal, valid_at=datetime.now(UTC), source="agent",
                run_id=ctx.run_id, scenario_id=d["id"], ontology=COMP)
    conn.rollback()
    rows = conn.execute("SELECT count(*) FROM audit_event WHERE scenario_id=%s AND status='executed'",
                        [d["id"]]).fetchone()
    conn.rollback()
    assert rows[0] == 0


def test_scenario_proposal_is_attributed_to_the_agent(world, factory, conn):
    ctx = opened("planner", factory, world)
    q = call(ctx, "query_objects", "q1", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    ref = q.envelope["data"]["objects"][0]["ref"]
    assert call(ctx, "propose_action", "p1", action="delay_shipment",
                params={"shipment": str(world["s_emea"]), "delay_hours": 5.0, "reason": "x"}, rationale="r",
                evidence=[{"ref": ref, "claim": "c", "kind": "asserted", "tool_call_id": "q1"}]).ok
    row = conn.execute("SELECT proposer, on_behalf_of, scenario_id FROM proposal WHERE run_id=%s",
                       [ctx.run_id]).fetchall()
    conn.rollback()
    assert row == [("agent:planner", ctx.principal.user, ctx.sid)]


def test_expire_run_discards_unreferenced_scenarios_at_run_end(world, factory, conn):
    from agent import Turn
    res, sink, _m, ctx = run_agent("planner", factory, [
        Turn(tool_uses=[("a", "scenario_open", {"name": "tmp"})]), Turn(tool_uses=[tu("b", "scenario_compare")]),
        Turn(text="done")])
    assert res.status == "completed" and ctx.scenario_ids
    conn.rollback()
    ev = conn.execute("SELECT kind FROM scenario_event WHERE scenario_id=%s ORDER BY seq", [ctx.scenario_ids[0]]).fetchall()
    conn.rollback()
    assert ("discarded",) in ev


# ------------------------------------------------------------------ compare
def test_compare_shape_and_isolation(world, factory):
    ctx = opened("planner", factory, world)
    call(ctx, "scenario_apply", "a1", action="delay_shipment",
         params={"shipment": str(world["s_emea"]), "delay_hours": 12.0, "reason": "x"})
    o = call(ctx, "scenario_compare", "c1")
    assert o.ok
    d = o.envelope["data"]
    assert set(d["kpis"]) == {"main", "scenario", "delta"}
    assert d["kpis"]["delta"]["delay_hours_total"] == 12.0 and d["kpis"]["main"]["delay_hours_total"] == 0
    items = d["diff_summary"]["items"]
    assert [i["ref"] for i in items if i["type"] == "Shipment"] == [f"Shipment:{world['s_emea']}"]
    assert d["diff_summary"]["items"][0]["changed"]["delay_hours"]["scenario"] == 12.0


# ------------------------------------------------------------------ propose from a scenario
def test_propose_from_scenario_has_impact_and_executes_nothing(world, factory, conn):
    ctx = opened("planner", factory, world)
    q = call(ctx, "query_objects", "q1", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    ref = q.envelope["data"]["objects"][0]["ref"]
    call(ctx, "scenario_apply", "a1", action="delay_shipment",
         params={"shipment": str(world["s_emea"]), "delay_hours": 12.0, "reason": "x"})
    o = call(ctx, "propose_action", "p1", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 12.0, "reason": "x"},
             rationale="tested in scenario", evidence=[{"ref": ref, "claim": "c", "kind": "asserted", "tool_call_id": "q1"}])
    assert o.ok, o.envelope
    dr = o.envelope["data"]["dry_run"]
    assert dr["scenario_id"] == ctx.sid
    assert dr["impact"]["delta"]["kpis"]["delay_hours_total"] == 12.0
    conn.rollback()
    row = conn.execute("SELECT scenario_id, on_behalf_of, run_id FROM proposal").fetchone()
    assert row == (ctx.sid, "pat", ctx.run_id)
    assert (main_row(conn, world["s_emea"]).get("delay_hours") or 0) == 0
    assert [r for r in audit(conn) if r[4] == 0 and r[3] == "executed"] == []
    assert conn.execute("SELECT count(*) FROM approval").fetchone()[0] == 0


def test_launcher_cannot_approve_scenario_proposal(world, factory, conn):
    from engine.actions import PermissionDenied, decide
    ctx = opened("planner", factory, world)
    q = call(ctx, "query_objects", "q1", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    ref = q.envelope["data"]["objects"][0]["ref"]
    o = call(ctx, "propose_action", "p1", action="delay_shipment",
             params={"shipment": str(world["s_emea"]), "delay_hours": 4.0, "reason": "x"},
             rationale="r", evidence=[{"ref": ref, "claim": "c", "kind": "asserted", "tool_call_id": "q1"}])
    pid = uuid.UUID(o.envelope["data"]["proposal_id"])
    with pytest.raises(PermissionDenied):
        decide(conn, pid, principal("planner"), True, ontology=COMP)


# ------------------------------------------------------------------ reads isolated (graph, history)
def test_reads_use_scenario_graph_and_history(world, factory, conn):
    ctx = opened("planner", factory, world)
    call(ctx, "scenario_apply", "a1", action="delay_shipment",
         params={"shipment": str(world["s_emea"]), "delay_hours": 12.0, "reason": "x"})
    h = call(ctx, "get_history", "h1", type="Shipment", id=str(world["s_emea"]))
    assert h.ok and h.envelope["scenario"]["id"] == ctx.sid
    assert h.envelope["data"]["versions"][-1]["props"]["delay_hours"] == 12.0
    main_h = call(ctx_for("planner", factory), "get_history", "h2", type="Shipment", id=str(world["s_emea"]))
    assert all((v["props"].get("delay_hours") or 0) == 0 for v in main_h.envelope["data"]["versions"])
    t = call(ctx, "traverse", "t1", start_id=str(world["emea_port"]), depth=1)
    assert t.ok and t.envelope["scenario"]
    n = call(ctx, "as_of", "n1", type="Shipment", id=str(world["s_emea"]), valid_at=datetime.now(UTC).isoformat(),
             include_neighbours=True)
    assert not n.ok and "scenario" in n.envelope["error"]


# ------------------------------------------------------------------ simulate
def test_simulate_validation_and_unavailable(world, factory, monkeypatch):
    ctx = opened("planner", factory, world)
    try:
        assert not call(ctx, "scenario_simulate", "s1", days=15).ok            # > 14 days
        assert not call(ctx, "scenario_simulate", "s2", days=1, injections=[{"kind": "meteor"}]).ok
        o = call(ctx, "scenario_simulate", "s3", days=1,
                 injections=[{"kind": "port_closure", "target": str(uuid.uuid4())}])
        assert not o.ok and "not retrieved" in o.envelope["error"]
        monkeypatch.setitem(sys.modules, "sim.whatif", None)
        o = call(ctx, "scenario_simulate", "s4", days=1)
        assert not o.ok and "not available" in o.envelope["error"]
    finally:
        ctx.close()


def test_simulate_runs_inside_scenario_when_available(world, factory, conn):
    pytest.importorskip("sim.whatif")
    ctx = opened("planner", factory, world)
    o = call(ctx, "scenario_simulate", "s1", days=1, injections=[{"kind": "shipment_delay", "duration_h": 5}])
    assert o.ok, o.envelope
    assert o.envelope["data"]["scenario_id"] == ctx.sid and "digest" in o.envelope["data"]
    rows = [r for r in audit(conn) if r[3] == "executed"]
    assert all(r[4] == ctx.sid for r in rows)           # nothing executed on main


# ------------------------------------------------------------------ runner / audit
def test_run_trace_and_events_carry_scenario(world, factory, conn):
    script = [__import__("agent").Turn(tool_uses=[("o1", "scenario_open", {"name": "w"}),
                                                  tu("q1", "query_objects", type="Port")]),
              __import__("agent").Turn(tool_uses=[tu("c1", "scenario_compare")]),
              __import__("agent").Turn(text="done")]
    res, sink, _m, ctx = run_agent("planner", factory, script)
    assert res.status == "completed"
    r = results(sink)
    assert r["q1"]["scenario"]["id"] == ctx.sid and r["c1"]["scenario"]["id"] == ctx.sid
    assert [t["name"] for t in ctx.trace] == ["scenario_open", "query_objects", "scenario_compare"]


def test_scenario_tools_count_toward_tool_cap(world, factory):
    from agent import Caps
    script = [__import__("agent").Turn(tool_uses=[("o1", "scenario_open", {"name": "w"}),
                                                  tu("c1", "scenario_compare"), tu("c2", "scenario_compare")]),
              __import__("agent").Turn(text="x")]
    caps = Caps(max_tool_calls=2)
    res, sink, _m, ctx = run_agent("planner", factory, script, caps=caps)
    assert res.status == "capped" and res.cap == "tool_calls" and ctx.tool_calls == 2


def test_prompt_mentions_scenarios_only_with_tools():
    from agent.prompt import build_system_prompt
    assert "what-if sandboxes" in build_system_prompt(COMP, "planner")


def test_api_run_json_lists_scenario_ids(world, aschema):
    from agent import Turn
    from test_api_agent import Api
    api = Api(aschema)
    rid = api.run("planner", [Turn(tool_uses=[("o1", "scenario_open", {"name": "w"})]),
                              Turn(tool_uses=[tu("c1", "scenario_compare")]), Turn(text="done")])
    j = api.wait(rid, role="planner")
    assert j["run"]["status"] == "completed"
    assert j["run"]["scenario_ids"] and j["run"]["scenario_id"] == j["run"]["scenario_ids"][-1]
    listed = api.c.get("/agent/runs", headers={"x-role": "planner"}).json()
    assert listed[0]["scenario_ids"] == j["run"]["scenario_ids"]
    # another role's run list does not expose it
    assert api.c.get("/agent/runs", headers={"x-role": "finance_analyst"}).json() == []
    cfg = api.c.get("/agent/config", headers={"x-role": "finance_analyst"}).json()
    assert "scenario_open" in cfg["tools"] and "scenario_apply" not in cfg["tools"]
