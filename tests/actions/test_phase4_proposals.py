"""Phase 4 prerequisites: agent proposals (on_behalf_of), idempotency, staleness, expiry, reroute."""
from datetime import timedelta
from uuid import uuid4

import pytest

from act_helpers import ADMIN, ADMIN2, FIN, PLANNER, T0, owner_link, owner_obj
from engine.actions import (PermissionDenied, ProposalExpired, StaleProposal, decide, execute, propose)
from engine.db import bitemporal as bt
from engine.db import Principal

T1 = T0 + timedelta(hours=1)
FIN_AGENT = Principal("finance_analyst", "agent:finance_analyst", "")


def agent_prop(conn, world, **kw):
    return propose(conn, "pay_invoice", {"invoice": world["inv"]}, FIN_AGENT, "agent says so", valid_at=T1,
                   on_behalf_of="demo-finance_analyst", evidence=[{"ref": "x", "kind": "asserted"}], **kw)


def test_agent_proposal_stores_behalf_evidence_base_and_expiry(conn, world):
    pid = agent_prop(conn, world, idempotency_key="k1")
    row = conn.execute("SELECT proposer, on_behalf_of, evidence, base_versions, expires_at > clock_timestamp() "
                       "FROM proposal WHERE id=%s", [pid]).fetchone()
    assert row[0] == "agent:finance_analyst" and row[1] == "demo-finance_analyst"
    assert row[2][0]["kind"] == "asserted" and row[3] and row[4]


def test_idempotent_proposals(conn, world):
    a = agent_prop(conn, world, idempotency_key="same")
    b = agent_prop(conn, world, idempotency_key="same")
    assert a == b
    assert conn.execute("SELECT count(*) FROM proposal").fetchone()[0] == 1


def test_launching_user_cannot_approve_their_agents_proposal(conn, world):
    pid = agent_prop(conn, world)
    human = Principal("admin", "demo-finance_analyst", "", ("pay_invoice",))  # same user, approver-capable
    with pytest.raises(PermissionDenied):
        decide(conn, pid, human, True, valid_at=T1)
    assert decide(conn, pid, ADMIN, True).changed  # a different human can


def test_stale_proposal_is_blocked(conn, world):
    pid = agent_prop(conn, world)
    # the invoice amount changes between proposal and approval; criteria (status open) still pass
    cur = bt.get_object(conn, "Invoice", world["inv"], masked=False)
    props = {k: v for k, v in cur.items() if k in ("ref", "amount", "status", "currency", "due", "anomaly")}
    props["amount"] = 999.0
    bt.write_object(conn, "Invoice", world["inv"], props, T1 + timedelta(minutes=5), "someone")
    conn.commit()
    with pytest.raises(StaleProposal) as ei:
        decide(conn, pid, ADMIN, True)
    assert any("Invoice" in k for k in ei.value.changed)
    assert bt.get_object(conn, "Invoice", world["inv"], masked=False)["status"] == "open"
    assert conn.execute("SELECT count(*) FROM approval").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM audit_event WHERE status='denied'").fetchone()[0] == 1


def test_unchanged_data_still_approves_at_approval_time(conn, world):
    pid = agent_prop(conn, world)
    r = decide(conn, pid, ADMIN2, True)
    assert r.changed
    assert bt.get_object(conn, "Invoice", world["inv"], masked=False)["status"] == "paid"


def test_expired_proposal_cannot_be_approved(conn, world):
    pid = agent_prop(conn, world, ttl_h=-1)
    with pytest.raises(ProposalExpired):
        decide(conn, pid, ADMIN, True)
    decide(conn, pid, ADMIN, False, "expired anyway")  # rejecting stays possible


def test_reroute_ends_all_current_routes(conn, world):
    p1 = world["emea_port"]
    p2 = world["apac_port"]
    p3 = owner_obj(conn, "Port", {"name": "Hamburg", "status": "open", "region": "EMEA"})
    owner_link(conn, "ships_via", world["ship"], p1)
    owner_link(conn, "ships_via", world["ship"], p2)
    r = execute(conn, "reroute_shipment", {"shipment": world["ship"], "new_port": p3}, PLANNER,
                valid_at=T1)
    assert r.changed
    assert bt.neighbours(conn, "ships_via", world["ship"], "out") == [p3]


def test_volatile_sim_properties_are_ignored_by_the_staleness_check():
    """The sim rewrites position/progress constantly; they must not make a proposal stale."""
    from engine.actions.core import _base_versions, _stale_keys
    d = lambda prog, status: [{"kind": "object", "type": "Shipment", "key": "k", "op": "modify",
                               "before": {"status": status, "progress": prog, "position": {"x": prog}},
                               "after": {"status": "delayed"}}]
    base = _base_versions(d(0.2, "in_transit"))
    assert "progress" not in base["object:Shipment:k"]
    assert _stale_keys(base, d(0.7, "in_transit")) == []
    assert _stale_keys(base, d(0.7, "arrived")) == ["object:Shipment:k"]
