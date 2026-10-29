"""Tool behaviour per role: permissions, hidden == missing, masking, envelope, bounds, provenance."""
import json
import uuid
from datetime import timedelta

import pytest

from agent import MemorySink, allowed_tools
from agent import tools as T
from agent_helpers import COMP, T0, ctx_for, dumps, link, obj, run_agent, tu
from engine.db import bitemporal as bt
from engine.sync.pipeline import SyncPipeline

MISSING = str(uuid.uuid4())


def call(ctx, tool, call_id="c1", **inp):
    return T.run_tool(ctx, tool, inp, call_id)


def ids(o):
    return {x["id"] for x in o.envelope["data"]["objects"]}


def node_ids(o):
    return {n["id"] for n in o.envelope["data"]["nodes"]}


# ------------------------------------------------------------------ envelope / bounds
def test_envelope_untrusted_with_provenance(world, factory):
    o = call(ctx_for("planner", factory), "traverse", start_id=str(world["emea_port"]), depth=1)
    e = o.envelope
    assert e["ok"] and e["untrusted"] is True and e["truncated"] is False and e["tool_call_id"] == "c1"
    assert f"Port:{world['emea_port']}" in e["provenance"]["asserted"]
    assert e["provenance"]["inferred"] == []
    assert any(x.startswith("ships_via:") for x in e["provenance"]["asserted"])


def test_error_envelope(world, factory):
    o = call(ctx_for("planner", factory), "query_objects", type="Nope")
    assert not o.ok and o.envelope["ok"] is False and o.envelope["untrusted"] is True and o.envelope["data"] is None


def test_query_limit_truncated_and_string_cap(world, conn, factory):
    for i in range(55):
        obj(conn, "Shipment", {"ref": f"BULK-{i:03d}", "status": "planned"})
    obj(conn, "Event", {"kind": "x", "note": "N" * 1000})
    ctx = ctx_for("planner", factory)
    o = call(ctx, "query_objects", type="Shipment", limit=50)
    assert o.ok and o.envelope["truncated"] is True and len(o.envelope["data"]["objects"]) <= 50
    assert call(ctx, "query_objects", type="Shipment", limit=51).envelope["error"].startswith("invalid input")
    ev = call(ctx, "query_objects", type="Event", filters=[{"property": "kind", "op": "eq", "value": "x"}])
    assert len(ev.envelope["data"]["objects"][0]["props"]["note"]) <= T.MAX_STR + 1


def test_query_filters_and_validation(world, factory):
    ctx = ctx_for("planner", factory)
    o = call(ctx, "query_objects", type="Shipment", filters=[{"property": "ref", "op": "eq", "value": "SH-EMEA"}])
    assert ids(o) == {str(world["s_emea"])}
    o = call(ctx, "query_objects", type="Shipment", order_by="ref", desc=True, limit=1)
    assert o.envelope["data"]["objects"][0]["props"]["ref"] == "SH-EMEA" and o.envelope["truncated"] is True
    # injection-looking values are bound parameters, not SQL
    o = call(ctx, "query_objects", type="Shipment",
             filters=[{"property": "ref", "op": "eq", "value": "x' OR '1'='1"}])
    assert o.ok and ids(o) == set()
    assert not call(ctx, "query_objects", type="Shipment", filters=[{"property": 'ref" OR 1=1 --', "op": "eq", "value": 1}]).ok
    assert not call(ctx, "query_objects", type="Shipment", order_by="ref; drop table x").ok
    assert not call(ctx, "query_objects", type="Shipment", filters=[{"property": "status", "op": "eq", "value": "bogus"}]).ok
    assert not call(ctx, "query_objects", type="Shipment", filters=[{"property": "position", "op": "is_null"}]).ok


# ------------------------------------------------------------------ per-role permissions
def test_planner_cannot_see_invoices(world, factory):
    ctx = ctx_for("planner", factory)
    o = call(ctx, "query_objects", type="Invoice")
    assert not o.ok and "unknown object type" in o.envelope["error"]
    assert call(ctx, "query_objects", type="Nope").envelope["error"].replace("Nope", "Invoice") == o.envelope["error"]
    inv = call(ctx, "traverse", start_id=str(world["inv"]))
    miss = call(ctx, "traverse", start_id=MISSING)
    assert not inv.ok and inv.envelope["error"] == miss.envelope["error"] == "not found"
    # from the supplier side the hidden invoices are cut out of the neighbourhood
    o = call(ctx, "traverse", start_id=str(world["sup_a"]), depth=2)
    assert str(world["inv"]) not in dumps(o.envelope) and str(world["inv_other"]) not in dumps(o.envelope)
    assert not call(ctx, "get_history", type="Invoice", id=str(world["inv"])).ok


def test_finance_sees_invoices(world, factory):
    o = call(ctx_for("finance_analyst", factory), "query_objects", type="Invoice")
    assert ids(o) == {str(world["inv"]), str(world["inv_other"])}


def test_regional_lead_sees_only_emea(world, factory):
    ctx = ctx_for("regional_ops_lead", factory)
    assert ids(call(ctx, "query_objects", type="Port")) == {str(world["emea_port"])}
    assert ids(call(ctx, "query_objects", type="Warehouse")) == {str(world["emea_wh"])}
    assert ids(call(ctx, "query_objects", type="Shipment")) == {str(world["s_emea"])}
    assert not call(ctx, "query_objects", type="Invoice").ok
    hidden = call(ctx, "traverse", start_id=str(world["apac_port"]))
    assert not hidden.ok and hidden.envelope["error"] == call(ctx, "traverse", start_id=MISSING).envelope["error"]
    o = call(ctx, "traverse", start_id=str(world["emea_port"]), depth=3)
    assert o.ok and str(world["s_apac"]) not in dumps(o.envelope) and str(world["apac_port"]) not in dumps(o.envelope)
    assert not call(ctx, "find_paths", from_id=str(world["s_emea"]), to_id=str(world["s_apac"])).ok
    imp = call(ctx, "impact_analysis", start_id=str(world["emea_port"]), direction="downstream")
    assert imp.ok and {n["id"] for n in imp.envelope["data"]["impacted"]} == {str(world["s_emea"])}
    assert not call(ctx, "impact_analysis", start_id=str(world["apac_port"])).ok
    assert not call(ctx, "get_history", type="Port", id=str(world["apac_port"])).ok
    assert not call(ctx, "as_of", type="Port", id=str(world["apac_port"]), valid_at=T0.isoformat()).ok
    # hidden id is indistinguishable from a missing id in every id-taking tool
    for name, kw in [("get_history", {}), ("as_of", {"valid_at": T0.isoformat()})]:
        a = call(ctx, name, type="Port", id=str(world["apac_port"]), **kw).envelope["error"]
        b = call(ctx, name, type="Port", id=MISSING, **kw).envelope["error"]
        assert a == b


def test_masked_property_absent_everywhere(world, factory):
    sink = MemorySink()
    res, sink, _m, ctx = run_agent("planner", factory, [
        tu_turn(tu("q1", "query_objects", type="Supplier")),
        tu_turn(tu("t1", "traverse", start_id=str(world["sup_a"]), depth=2)),
        tu_turn(tu("q2", "query_objects", type="Supplier", filters=[{"property": "country", "op": "eq", "value": "NL"}])),
        text_turn("done"),
    ], sink=sink)
    assert res.status == "completed"
    # tool results (data) are masked; tool_call inputs are what the model itself typed
    blob = dumps([e for e in sink.events if e["kind"] == "tool_result"])
    for c in ("NL", "DE", "FR"):
        assert f'"country": "{c}"' not in blob and f"'country': '{c}'" not in blob and f'"{c}"' not in blob
    q1 = [e for e in sink.events if e["kind"] == "tool_result" and e["payload"]["tool_call_id"] == "q1"][0]
    assert all(o["props"]["country"] is None for o in q1["payload"]["result"]["data"]["objects"])
    q2 = [e for e in sink.events if e["kind"] == "tool_result" and e["payload"]["tool_call_id"] == "q2"][0]
    assert q2["payload"]["result"]["ok"] is False  # cannot filter on a masked property
    # admin sees the value
    adm = call(ctx_for("admin", factory), "query_objects", type="Supplier")
    assert {o["props"]["country"] for o in adm.envelope["data"]["objects"]} == {"NL", "DE", "FR"}


def tu_turn(*uses):
    from agent import Turn
    return Turn(tool_uses=list(uses))


def text_turn(t):
    from agent import Turn
    return Turn(text=t)


# ------------------------------------------------------------------ history / as_of / diff / functions
def test_history_asof_diff(world, conn, factory):
    s = world["s_emea"]
    bt.write_object(conn, "Shipment", s, {"ref": "SH-EMEA", "status": "delayed", "cargo_value": 500.0},
                    T0 + timedelta(days=1), "setup")
    bt.write_link(conn, "ships_via", s, world["apac_port"], T0 + timedelta(days=1), "setup")
    conn.commit()
    ctx = ctx_for("planner", factory)
    h = call(ctx, "get_history", type="Shipment", id=str(s))
    assert h.ok and {v["props"]["status"] for v in h.envelope["data"]["versions"]} == {"in_transit", "delayed"}
    a = call(ctx, "as_of", type="Shipment", id=str(s), valid_at=(T0 + timedelta(hours=12)).isoformat(),
             include_neighbours=True)
    assert a.ok and a.envelope["data"]["props"]["status"] == "in_transit"
    assert str(world["apac_port"]) not in dumps(a.envelope)
    d = call(ctx, "diff", type="Shipment", id=str(s), valid_at_a=(T0 + timedelta(hours=12)).isoformat(),
             valid_at_b=(T0 + timedelta(days=2)).isoformat(), include_neighbours=True)
    assert d.ok
    assert d.envelope["data"]["changed"]["status"] == {"a": "in_transit", "b": "delayed"}
    assert [e["dst"] for e in d.envelope["data"]["links_added"]] == [str(world["apac_port"])]
    assert call(ctx, "as_of", type="Shipment", id=str(s), valid_at="not a date").envelope["error"].startswith("not an ISO")
    # before the object existed
    early = call(ctx, "as_of", type="Shipment", id=str(s), valid_at=(T0 - timedelta(days=9)).isoformat())
    assert not early.ok


def test_run_function_gated_by_role(world, factory):
    p = ctx_for("planner", factory)
    o = call(p, "run_function", name="shipments_at_risk", params={"port": str(world["emea_port"])})
    assert o.ok and o.envelope["data"]["result"]["count"] == 1
    assert f"Shipment:{world['s_emea']}" in o.envelope["provenance"]["asserted"]
    d = call(p, "run_function", name="invoice_anomaly_score", params={"invoice": str(world["inv"])})
    assert not d.ok and d.denied and d.denied["kind"] == "function"
    f = call(ctx_for("finance_analyst", factory), "run_function", name="invoice_anomaly_score",
             params={"invoice": str(world["inv"])})
    assert f.ok and "score" in f.envelope["data"]["result"]
    assert not call(p, "run_function", name="shipments_at_risk", params={"port": str(world["apac_port"]), "x": 1}).ok
    lead = call(ctx_for("regional_ops_lead", factory), "run_function", name="shipments_at_risk",
                params={"port": str(world["apac_port"])})
    assert not lead.ok


def test_impact_directions(world, factory):
    ctx = ctx_for("planner", factory)
    down = call(ctx, "impact_analysis", start_id=str(world["emea_port"]), direction="downstream", depth=1)
    assert {n["id"] for n in down.envelope["data"]["impacted"]} == {str(world["s_emea"])}
    up = call(ctx, "impact_analysis", start_id=str(world["s_emea"]), direction="upstream", depth=2)
    got = {n["type"]: n for n in up.envelope["data"]["impacted"]}
    assert got["Port"]["hops"] == 1 and got["Event"]["hops"] == 2 and got["Event"]["basis"] == "asserted"


def test_find_paths_asserted(world, factory):
    ctx = ctx_for("planner", factory)
    o = call(ctx, "find_paths", from_id=str(world["s_emea"]), to_id=str(world["ev"]), max_hops=2)
    assert o.ok and o.envelope["data"]["paths"][0]["hops"][0]["inferred"] is False
    assert not call(ctx, "find_paths", from_id=str(world["s_emea"]), to_id=str(world["ev"]), max_hops=5).ok


# ------------------------------------------------------------------ inferred edges
def test_inferred_edges_flagged_with_rule_and_support(world, aschema, factory):
    pipe = SyncPipeline(schema=aschema, min_reason_interval_s=0)
    pipe.sync()
    pipe.maybe_reason(force=True)
    ctx = ctx_for("planner", factory, pipeline=pipe)
    a, c = str(world["sup_a"]), str(world["sup_c"])
    o = call(ctx, "traverse", start_id=a, depth=2, include_inferred=True)
    inf = o.envelope["provenance"]["inferred"]
    assert o.ok and any(i["edge"] == f"supplies:{a}>{c}" for i in inf)
    i = [i for i in inf if i["edge"] == f"supplies:{a}>{c}"][0]
    assert i["rule"] == "owl2rl:transitive(supplies)" and i["support"] == [a, str(world["sup_b"]), c]
    # without include_inferred nothing inferred is shown
    plain = call(ctx, "traverse", start_id=a, depth=2)
    assert plain.envelope["provenance"]["inferred"] == []
    p = call(ctx, "find_paths", from_id=a, to_id=c, include_inferred=True)
    assert p.envelope["data"]["paths"][0]["hops"][0]["inferred"] is True
    # evidence kind must match: citing an inferred edge as asserted is refused
    ref = f"supplies:{a}>{c}"
    bad = call(ctx, "propose_action", "p1", action="close_port", rationale="r",
               params={"port": str(world["emea_port"])},
               evidence=[{"ref": ref, "claim": "c", "kind": "asserted", "tool_call_id": "c1"}])
    assert not bad.ok and "inferred" in bad.envelope["error"]


# ------------------------------------------------------------------ schemas
FORBIDDEN = {"role", "user", "schema", "table", "principal", "actor", "on_behalf_of", "region", "approver", "sql",
             "sparql", "query", "header", "x-role"}


def _names(s, acc):
    if isinstance(s, dict):
        for k, v in s.items():
            if k == "properties":
                acc.update(v)
            _names(v, acc)
    elif isinstance(s, list):
        for v in s:
            _names(v, acc)
    return acc


def test_no_identity_or_sql_parameters_in_any_tool_schema():
    for sch in T.tool_schemas(list(T.TOOLS)):
        assert not (_names(sch["input_schema"], set()) & FORBIDDEN), sch["name"]
        assert sch["input_schema"].get("additionalProperties") is False


def test_extra_identity_fields_rejected(world, factory):
    ctx = ctx_for("planner", factory)
    o = call(ctx, "query_objects", type="Shipment", role="admin")
    assert not o.ok and o.envelope["error"].startswith("invalid input")
    o = call(ctx, "traverse", start_id=str(world["emea_port"]), user="adam")
    assert not o.ok


def test_tools_offered_are_the_role_tools(world, factory):
    for role in ("planner", "finance_analyst", "regional_ops_lead", "admin"):
        assert set(allowed_tools(COMP, role)) <= set(T.TOOLS) and "propose_action" in allowed_tools(COMP, role)
