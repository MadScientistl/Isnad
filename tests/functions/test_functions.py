"""Read-only function registry and the reroute_shipment action."""
import pytest

from engine import functions as fx
from engine.actions import PermissionDenied, execute
from engine.actions.core import CriteriaFailed
from engine.compiler import compile_ontology
from engine.db import bitemporal as bt
from engine.db.principal import scoped

from fn_helpers import ADMIN, FIN, LEAD, PLANNER, T0, owner_link, owner_obj

MODEL = compile_ontology("ontology/supply_chain.yaml").model


def run_as(conn, pr, name, params):
    with conn.transaction():
        with scoped(conn, pr):
            return fx.run(conn, name, params)


def world(conn):
    w = {}
    w["p1"] = owner_obj(conn, "Port", {"name": "Rotterdam", "status": "closed", "region": "EMEA"})
    w["p2"] = owner_obj(conn, "Port", {"name": "Antwerp", "status": "open", "region": "EMEA"})
    w["s1"] = owner_obj(conn, "Shipment", {"ref": "S1", "status": "in_transit", "delay_hours": 5.0})
    w["s2"] = owner_obj(conn, "Shipment", {"ref": "S2", "status": "arrived"})
    owner_link(conn, "ships_via", w["s1"], w["p1"])
    owner_link(conn, "ships_via", w["s2"], w["p1"])
    w["ev"] = owner_obj(conn, "Event", {"kind": "port_closure", "severity": "high"})
    owner_link(conn, "affects", w["ev"], w["p1"])
    w["sup"] = owner_obj(conn, "Supplier", {"name": "Acme"})
    invs = []
    for i, amt in enumerate([100.0, 110.0, 105.0, 900.0]):
        inv = owner_obj(conn, "Invoice", {"ref": f"I{i}", "amount": amt, "status": "open"})
        owner_link(conn, "billed_by", inv, w["sup"])
        invs.append(inv)
    w["invs"] = invs
    return w


def test_registry_matches_yaml():
    assert set(MODEL.functions) == set(fx.FUNCTIONS)


def test_shipments_at_risk(conn):
    w = world(conn)
    r = run_as(conn, PLANNER, "shipments_at_risk", {"port": w["p1"]})
    assert r["count"] == 1 and r["shipments"][0]["ref"] == "S1"


def test_risk_factors(conn):
    w = world(conn)
    r = run_as(conn, PLANNER, "risk_factors", {"shipment": w["s1"]})
    assert r["delay_hours"] == 5.0
    assert [p["name"] for p in r["closed_ports"]] == ["Rotterdam"]
    assert len(r["open_events"]) == 1 and len(r["factors"]) == 3


def test_invoice_anomaly_score(conn):
    w = world(conn)
    hi = run_as(conn, FIN, "invoice_anomaly_score", {"invoice": w["invs"][3]})
    lo = run_as(conn, FIN, "invoice_anomaly_score", {"invoice": w["invs"][0]})
    assert 0 <= lo["score"] <= 1 and 0 <= hi["score"] <= 1
    assert hi["score"] > lo["score"] and hi["reasons"]
    assert run_as(conn, FIN, "invoice_anomaly_score", {"invoice": w["invs"][3]}) == hi  # deterministic


def test_invisible_object_is_not_found(conn):
    w = world(conn)
    with pytest.raises(fx.FunctionError):  # planner cannot see invoices
        run_as(conn, PLANNER, "invoice_anomaly_score", {"invoice": w["invs"][0]})


def test_unknown_and_bad_params(conn):
    with pytest.raises(fx.FunctionError):
        fx.run(conn, "nope", {})
    with pytest.raises(fx.FunctionError):
        fx.run(conn, "risk_factors", {"bogus": 1})
    with pytest.raises(fx.FunctionError):
        fx.run(conn, "risk_factors", {"shipment": "not-a-uuid"})


def test_role_can_run_function():
    assert MODEL.role_can_run_function("planner", "shipments_at_risk")
    assert not MODEL.role_can_run_function("planner", "invoice_anomaly_score")
    assert not MODEL.role_can_run_function("planner", "nope")


def test_reroute_shipment(conn):
    w = world(conn)
    r = execute(conn, "reroute_shipment", {"shipment": w["s1"], "new_port": w["p2"]},
                PLANNER, valid_at=T0)
    assert r.changed
    assert bt.neighbours(conn, "ships_via", w["s1"], "out", as_of_valid=T0) == [w["p2"]]


def test_reroute_criteria_and_permissions(conn):
    w = world(conn)
    with pytest.raises(CriteriaFailed):  # new port closed
        execute(conn, "reroute_shipment", {"shipment": w["s1"], "new_port": w["p1"]},
                PLANNER, valid_at=T0)
    with pytest.raises(CriteriaFailed):  # arrived
        execute(conn, "reroute_shipment", {"shipment": w["s2"], "new_port": w["p2"]},
                ADMIN, valid_at=T0)
    with pytest.raises(PermissionDenied):
        execute(conn, "reroute_shipment", {"shipment": w["s1"], "new_port": w["p2"]},
                LEAD, valid_at=T0)
