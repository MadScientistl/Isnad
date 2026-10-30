"""Phase 4 YAML additions: per-role `agent:` block and `functions:` registry."""
import copy
from pathlib import Path

import pytest
import yaml

from engine.compiler import OntologyError
from engine.compiler.model import AGENT_TOOLS, parse_ontology

RAW = yaml.safe_load((Path(__file__).resolve().parents[2] / "ontology" / "supply_chain.yaml").read_text())


def build(mut=None):
    raw = copy.deepcopy(RAW)
    if mut:
        mut(raw)
    return parse_ontology(raw)


def fails(mut, msg):
    with pytest.raises(OntologyError) as e:
        build(mut)
    assert msg in str(e.value)


def test_real_ontology_agents():
    o = build()
    assert o.agent_allowlist("planner") == ("delay_shipment", "reroute_shipment", "close_port")
    assert o.agent_allowlist("finance_analyst") == ("flag_invoice", "pay_invoice")
    assert o.agent_allowlist("regional_ops_lead") == ("close_port", "reopen_port")
    assert set(o.agent_allowlist("admin")) == {"flag_invoice", "delay_shipment", "close_port", "reopen_port",
                                              "reroute_shipment"}
    assert o.agent_allowlist("sim") == () and o.agent_tools("sim") == ()
    scn = {"scenario_open", "scenario_apply", "scenario_simulate", "scenario_compare"}
    for r in ("planner", "regional_ops_lead", "admin"):
        assert set(o.agent_tools(r)) == set(AGENT_TOOLS)
    assert set(o.agent_tools("finance_analyst")) == set(AGENT_TOOLS) - {"scenario_apply", "scenario_simulate"}
    assert scn <= set(AGENT_TOOLS)
    for r in ("planner", "finance_analyst", "regional_ops_lead", "admin"):
        assert o.roles[r].agent.instructions
    assert o.actions["pay_invoice"].approval_required


def test_functions_parsed():
    o = build()
    assert set(o.functions) == {"shipments_at_risk", "invoice_anomaly_score", "risk_factors"}
    assert o.role_can_run_function("finance_analyst", "invoice_anomaly_score")
    assert not o.role_can_run_function("planner", "invoice_anomaly_score")
    assert o.functions["shipments_at_risk"].param_specs()[0].ref == "Port"


def test_reroute_shipment_defined():
    o = build()
    a = o.actions["reroute_shipment"]
    assert set(a.roles) == {"planner", "admin"} and not a.approval_required
    assert o.role_can_execute("planner", "reroute_shipment")
    assert not o.role_can_execute("finance_analyst", "reroute_shipment")
    assert a.function == "reroute_shipment"


def test_unknown_tool():
    fails(lambda r: r["roles"]["planner"]["agent"]["tools"].append("rm_rf"), "unknown tool 'rm_rf'")


@pytest.mark.parametrize("bad,msg", [
    ("nonexistent", "unknown action 'nonexistent'"),
    ("pay_invoice", "not in the role's actions"),
    ("create_object", "generic action 'create_object'"),
    ("end_link", "generic action 'end_link'"),
])
def test_bad_allowlist(bad, msg):
    fails(lambda r: r["roles"]["planner"]["agent"]["allowlist"].append(bad), msg)


def test_allowlist_all_rejected():
    def m(r):
        r["roles"]["admin"]["agent"]["allowlist"] = "all"
    with pytest.raises(OntologyError):
        build(m)


def test_allowlist_requires_propose_tool():
    def m(r):
        r["roles"]["planner"]["agent"]["tools"].remove("propose_action")
    fails(m, "propose_action")


def test_function_validation():
    fails(lambda r: r["functions"].__setitem__("mystery", {"roles": ["admin"]}), "function 'mystery'")
    fails(lambda r: r["functions"]["risk_factors"]["roles"].append("ghost"), "unknown role 'ghost'")
    fails(lambda r: r["functions"]["risk_factors"]["params"].__setitem__("x", "uuid(Nope)!"), "unknown type 'Nope'")


def test_agent_optional():
    def m(r):
        del r["roles"]["regional_ops_lead"]["agent"]
    o = build(m)
    assert o.agent_allowlist("regional_ops_lead") == () and o.roles["regional_ops_lead"].agent is None
