import dataclasses
from datetime import timedelta
from uuid import uuid4

import pytest

from act_helpers import ADMIN, ADMIN2, FIN, PLANNER, T0, owner_obj
from engine.actions import (NotFound, PermissionDenied, RollbackConflict, RollbackRefused, decide, execute,
                            propose, rollback)
from engine.actions.core import _load
from engine.db import bitemporal as bt

T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)


def test_rollback_close_port(conn, world):
    p = world["emea_port"]
    r = execute(conn, "close_port", {"port": p, "reason": "x"}, PLANNER, valid_at=T1)
    ev = r.created["Event"]
    rb = rollback(conn, r.action_id, PLANNER, "mistake", valid_at=T2)
    assert rb.changed and rb.name == "rollback"
    assert bt.get_object(conn, "Port", p, as_of_valid=T2)["status"] == "open"
    assert bt.get_object(conn, "Event", ev, as_of_valid=T2) is None
    assert bt.neighbours(conn, "affects", ev, "out", as_of_valid=T2) == []
    # history is kept
    assert bt.get_object(conn, "Port", p, as_of_valid=T1)["status"] == "closed"
    row = conn.execute("SELECT name, parent_execution_id, actor, role, status, params FROM audit_event "
                       "WHERE action_id=%s", [rb.action_id]).fetchone()
    assert row[:5] == ("rollback", r.action_id, "pat", "planner", "executed")
    assert row[5]["reason"] == "mistake"
    assert conn.execute("SELECT original_action_id, rollback_action_id, actor, reason FROM rollback").fetchall() == [
        (r.action_id, rb.action_id, "pat", "mistake")]
    assert {(d["type"], d["op"]) for d in rb.diff} == {("Port", "modify"), ("Event", "delete"),
                                                       ("affects", "delete")}
    # webhooks are not undone and not re-sent
    assert conn.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0] == 1
    # once only
    with pytest.raises(RollbackRefused, match="already"):
        rollback(conn, r.action_id, PLANNER, valid_at=T3)


def test_rollback_conflict_lists_keys(conn, world):
    p = world["emea_port"]
    r = execute(conn, "close_port", {"port": p}, PLANNER, valid_at=T1)
    execute(conn, "reopen_port", {"port": p}, PLANNER, valid_at=T2)
    n = conn.execute("SELECT count(*) FROM audit_event").fetchone()[0]
    with pytest.raises(RollbackConflict) as e:
        rollback(conn, r.action_id, PLANNER, valid_at=T3)
    assert f"Port:{p}" in e.value.keys and f"Event:{r.created['Event']}" in e.value.keys
    assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == n
    assert conn.execute("SELECT count(*) FROM rollback").fetchone()[0] == 0


def test_rollback_restores_timestamps_geometry_and_links(conn, world):
    sh = owner_obj(conn, "Shipment", {"ref": "R", "eta": T0 + timedelta(days=3), "status": "in_transit",
                                      "position": {"type": "Point", "coordinates": [1.0, 2.0]}})
    d = execute(conn, "delay_shipment", {"shipment": sh, "delay_hours": 10}, PLANNER, valid_at=T1)
    pos = execute(conn, "ship_position", {"shipment": sh, "position": {"type": "Point", "coordinates": [5.0, 6.0]},
                                          "progress": 0.5}, PLANNER, valid_at=T2)
    rollback(conn, pos.action_id, PLANNER, valid_at=T3)
    g = bt.get_object(conn, "Shipment", sh, as_of_valid=T3)
    assert g["position"] == {"type": "Point", "coordinates": [1.0, 2.0]} and g["progress"] is None
    assert g["status"] == "delayed"
    rollback(conn, d.action_id, PLANNER, valid_at=T0 + timedelta(hours=4))
    g = bt.get_object(conn, "Shipment", sh, as_of_valid=T0 + timedelta(hours=5))
    assert g["eta"] == T0 + timedelta(days=3) and g["status"] == "in_transit" and g["delay_hours"] is None
    # a link that was ended is restored
    lk = execute(conn, "end_link", {"link": "ships_to", "src": world["ship"], "dst": world["emea_wh"]}, PLANNER,
                 valid_at=T1)
    rollback(conn, lk.action_id, PLANNER, valid_at=T2)
    assert bt.neighbours(conn, "ships_to", world["ship"], as_of_valid=T3) == [world["emea_wh"]]


def test_irreversible_refused(conn, world, monkeypatch):
    m = _load(None)
    monkeypatch.setitem(m.actions, "close_port", dataclasses.replace(m.actions["close_port"], rollback="irreversible"))
    r = execute(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    with pytest.raises(RollbackRefused, match="irreversible"):
        rollback(conn, r.action_id, ADMIN, valid_at=T2)
    assert conn.execute("SELECT count(*) FROM rollback").fetchone()[0] == 0


def test_rollback_permissions_and_visibility(conn, world):
    r = execute(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    with pytest.raises(NotFound):          # finance analyst cannot see the planner's execution at all
        rollback(conn, r.action_id, FIN, valid_at=T2)
    with pytest.raises(NotFound):
        rollback(conn, uuid4(), PLANNER, valid_at=T2)
    # same role, other user: execution is not theirs -> not visible
    from act_helpers import make_principal
    with pytest.raises(NotFound):
        rollback(conn, r.action_id, make_principal("planner", "pia"), valid_at=T2)
    # own execution of an action the role cannot undo: admin approves a payment, finance cannot roll it back
    pid = propose(conn, "pay_invoice", {"invoice": world["inv"]}, FIN, "pay", valid_at=T1)
    paid = decide(conn, pid, ADMIN, True, valid_at=T1)
    with pytest.raises(PermissionDenied):   # visible (on_behalf_of) but only approvers may undo
        rollback(conn, paid.action_id, FIN, valid_at=T2)
    rb = rollback(conn, paid.action_id, ADMIN2, "oops", valid_at=T2)   # admins see all executions
    assert rb.changed
    assert bt.get_object(conn, "Invoice", world["inv"], as_of_valid=T3, masked=False)["status"] == "open"


def test_cannot_rollback_denied_or_rollback_or_unknown(conn, world):
    with pytest.raises(PermissionDenied):
        execute(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "x"}, PLANNER, valid_at=T1)
    (denied_id,) = conn.execute("SELECT action_id FROM audit_event WHERE status='denied'").fetchone()
    with pytest.raises(RollbackRefused):
        rollback(conn, denied_id, PLANNER, valid_at=T2)
    r = execute(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    rb = rollback(conn, r.action_id, PLANNER, valid_at=T2)
    with pytest.raises(RollbackRefused):
        rollback(conn, rb.action_id, PLANNER, valid_at=T3)
