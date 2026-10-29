from datetime import timedelta
from uuid import uuid4

import pytest

from act_helpers import ADMIN, ADMIN2, FIN, FIN2, PLANNER, T0, owner_obj
from engine.actions import CriteriaFailed, NotFound, PermissionDenied, decide, execute, propose
from engine.db import bitemporal as bt
from engine.db import Principal, scoped

T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def status(conn, inv, t=T2):
    return bt.get_object(conn, "Invoice", inv, as_of_valid=t, masked=False)["status"]


def mkprop(conn, world, who=FIN):
    return propose(conn, "pay_invoice", {"invoice": world["inv"]}, who, "month end", valid_at=T1,
                   tool_trace=[{"tool": "t"}])


def test_propose_writes_only_a_proposal(conn, world):
    pid = mkprop(conn, world)
    row = conn.execute("SELECT action, params, proposer, proposer_role, rationale, dry_run, tool_trace "
                       "FROM proposal WHERE id=%s", [pid]).fetchone()
    assert row[0] == "pay_invoice" and row[2:5] == ("fiona", "finance_analyst", "month end")
    assert row[5]["diff"][0]["after"]["status"] == "paid" and row[6] == [{"tool": "t"}]
    assert status(conn, world["inv"]) == "open"
    assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM approval").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM obj_invoice WHERE action_id IS NOT NULL").fetchone()[0] == 0


def test_propose_requires_role_and_valid_criteria(conn, world):
    with pytest.raises(PermissionDenied):
        propose(conn, "pay_invoice", {"invoice": world["inv"]}, PLANNER, "x", valid_at=T1)
    paid = owner_obj(conn, "Invoice", {"ref": "P", "amount": 1.0, "status": "paid"})
    with pytest.raises(CriteriaFailed):
        propose(conn, "pay_invoice", {"invoice": paid}, FIN, "x", valid_at=T1)
    with pytest.raises(NotFound):
        propose(conn, "pay_invoice", {"invoice": uuid4()}, FIN, "x", valid_at=T1)
    assert conn.execute("SELECT count(*) FROM proposal").fetchone()[0] == 0


def test_approve_executes_as_approver(conn, world):
    pid = mkprop(conn, world)
    r = decide(conn, pid, ADMIN, True, "ok", valid_at=T2)
    assert r.changed and status(conn, world["inv"]) == "paid"
    ev = conn.execute("SELECT actor, role, on_behalf_of, proposal_id, status, name FROM audit_event "
                      "WHERE action_id=%s", [r.action_id]).fetchone()
    assert ev == ("adam", "admin", "fiona", pid, "executed", "pay_invoice")
    ap = conn.execute("SELECT decision, approver, approver_role, comment, execution_id FROM approval "
                      "WHERE proposal_id=%s", [pid]).fetchone()
    assert ap == ("approved", "adam", "admin", "ok", r.action_id)
    # the data rows were written with the approver as changed_by
    assert conn.execute("SELECT changed_by FROM obj_invoice WHERE action_id=%s", [r.action_id]).fetchone()[0] == "adam"


def test_finance_analyst_cannot_approve_and_proposer_cannot_self_approve(conn, world):
    pid = mkprop(conn, world)
    with pytest.raises(NotFound):  # another analyst cannot even see the proposal (RLS)
        decide(conn, pid, FIN2, True, valid_at=T2)
    # a finance analyst who can see it (GUC grants visibility) is still refused by the ontology
    peeker = Principal("finance_analyst", "peek", "", ("pay_invoice",))
    with pytest.raises(PermissionDenied, match="may not approve"):
        decide(conn, pid, peeker, True, valid_at=T2)
    with pytest.raises(PermissionDenied):
        decide(conn, pid, FIN, True, valid_at=T2)        # proposer
    assert status(conn, world["inv"]) == "open"
    assert conn.execute("SELECT count(*) FROM approval").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM audit_event WHERE status='denied'").fetchone()[0] == 2
    # admin as proposer cannot approve either (no admin bypass), another admin can
    pid2 = propose(conn, "pay_invoice", {"invoice": world["inv"]}, ADMIN, "mine", valid_at=T1)
    with pytest.raises(PermissionDenied):
        decide(conn, pid2, ADMIN, True, valid_at=T2)
    r = decide(conn, pid2, ADMIN2, True, valid_at=T2)
    assert r.changed


def test_one_decision_per_proposal(conn, world):
    pid = mkprop(conn, world)
    decide(conn, pid, ADMIN, True, valid_at=T2)
    for approve in (True, False):
        with pytest.raises(ValueError, match="already decided"):
            decide(conn, pid, ADMIN2, approve, valid_at=T2)
    assert conn.execute("SELECT count(*) FROM approval").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM audit_event WHERE name='pay_invoice' AND status='executed'"
                        ).fetchone()[0] == 1


def test_reject_executes_nothing(conn, world):
    pid = mkprop(conn, world)
    assert decide(conn, pid, ADMIN, False, "no") is None
    assert status(conn, world["inv"]) == "open"
    assert conn.execute("SELECT decision, execution_id FROM approval").fetchone() == ("rejected", None)
    assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 0
    with pytest.raises(NotFound):  # only approvers can reject too (FIN2 cannot see it)
        decide(conn, mkprop(conn, world), FIN2, False)


def test_approve_rechecks_criteria_at_execution_time(conn, world):
    pid = mkprop(conn, world)
    execute(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "late finding"}, FIN2, valid_at=T1)
    with pytest.raises(CriteriaFailed) as e:
        decide(conn, pid, ADMIN, True, valid_at=T2)
    assert e.value.failures[0]["message"] == "Only open invoices can be paid"
    assert status(conn, world["inv"]) == "flagged"
    assert conn.execute("SELECT count(*) FROM approval").fetchone()[0] == 0   # still pending
    d = conn.execute("SELECT status, proposal_id, name FROM audit_event WHERE status='denied'").fetchall()
    assert d == [("denied", pid, "pay_invoice")]


def test_unknown_proposal_is_not_found(conn):
    with pytest.raises(NotFound):
        decide(conn, uuid4(), ADMIN, True)


def test_proposal_visibility_under_rls(conn, world):
    pid = mkprop(conn, world)
    def count(p):
        with conn.transaction():
            with scoped(conn, p):
                return conn.execute("SELECT count(*) FROM proposal WHERE id=%s", [pid]).fetchone()[0]
    assert count(FIN) == 1 and count(ADMIN) == 1 and count(FIN2) == 0 and count(PLANNER) == 0
