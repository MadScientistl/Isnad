"""Existing API flows with real tokens: cross-role denial, launcher != approver by different users, agent + SSE."""
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent import MockModel, Turn  # noqa: E402

from .auth_helpers import ONTOLOGY, SECRET, bearer, login  # noqa: E402
from engine.api.main import create_app  # noqa: E402
from engine.auth import tokens  # noqa: E402


def open_invoice(c, h):
    """A fresh open invoice (written as the schema owner); the schema is the app's."""
    from datetime import UTC, datetime
    from uuid import uuid4
    from engine.db import bitemporal as bt, connect
    i = uuid4()
    conn = connect(schema=c.app.state.schema_name)
    bt.write_object(conn, "Invoice", i, {"ref": f"INV-{i.hex[:6]}", "amount": 100.0, "status": "open"},
                    datetime(2026, 1, 1, tzinfo=UTC), "setup")
    conn.commit()
    conn.close()
    return str(i)


def test_cross_role_denial_with_tokens(client):
    alice, carol, bob, dave = (bearer(client, u) for u in ("alice", "carol", "bob", "dave"))
    inv = open_invoice(client, bob)
    # planner may not pay an invoice (not in the role's actions); finance may only propose it
    assert client.post("/actions/pay_invoice/execute", json={"params": {"invoice": inv}}, headers=alice).status_code in (403, 404)
    assert client.post("/actions/pay_invoice/execute", json={"params": {"invoice": inv}}, headers=carol).status_code == 202   # approval required
    # each role sees its own /me and /roles
    assert client.get("/me", headers=dave).json()["region"] == "EMEA"
    assert client.get("/me", headers=bob).json()["role"] == "admin"
    # role switching by header is impossible
    r = client.post("/actions/pay_invoice/execute", json={"params": {"invoice": inv}}, headers={**alice, "x-role": "admin"})
    assert r.status_code in (403, 404)
    # admin-only routes: planner token with an admin header gets nothing
    assert client.get("/ontology/versions", headers={**alice, "x-role": "admin"}).status_code in (403, 404)
    assert client.get("/ontology/versions", headers=bob).status_code == 200


def test_proposer_and_approver_are_real_users(client):
    carol, bob, erin, alice = (bearer(client, u) for u in ("carol", "bob", "erin", "alice"))
    inv = open_invoice(client, bob)
    pid = client.post("/actions/pay_invoice/propose", json={"params": {"invoice": inv}, "rationale": "due"},
                      headers=carol).json()["proposal_id"]
    row = next(r for r in client.get("/proposals", headers=bob).json() if r["id"] == pid)
    assert row["proposer"] == "carol"
    # the proposer's role cannot approve, even claiming admin in a header
    assert client.post(f"/proposals/{pid}/approve", headers={**carol, "x-role": "admin"}).status_code == 403
    assert client.post(f"/proposals/{pid}/approve", headers=alice).status_code in (403, 404)
    r = client.post(f"/proposals/{pid}/approve", headers=bob, json={"comment": "ok"})
    assert r.status_code == 200, r.text

    # admin proposes, the SAME admin cannot approve; a different admin (erin) can
    inv2 = open_invoice(client, bob)
    pid2 = client.post("/actions/pay_invoice/propose", json={"params": {"invoice": inv2}}, headers=bob).json()["proposal_id"]
    assert client.post(f"/proposals/{pid2}/approve", headers=bob).status_code == 403
    r = client.post(f"/proposals/{pid2}/approve", headers=erin, json={"comment": "second pair of eyes"})
    assert r.status_code == 200, r.text


def test_forged_sim_and_mismatched_role_tokens_rejected(client):
    good, _ = tokens.issue(SECRET.encode(), "alice", "sim", 600)       # valid signature, role not alice's
    assert client.get("/me", headers={"Authorization": f"Bearer {good}"}).status_code == 401
    t2, _ = tokens.issue(SECRET.encode(), "alice", "admin", 600)
    assert client.get("/me", headers={"Authorization": f"Bearer {t2}"}).status_code == 401
    t3, _ = tokens.issue(SECRET.encode(), "mallory", "admin", 600)     # signed but not in the users file
    assert client.get("/me", headers={"Authorization": f"Bearer {t3}"}).status_code == 401


def test_sim_routes_admin_only_with_tokens(client):
    assert client.get("/sim/state", headers=bearer(client, "alice")).status_code in (200, 403, 404)
    r = client.post("/sim/control", json={"speed": "paused"}, headers=bearer(client, "alice"))
    assert r.status_code in (403, 404)


# ------------------------------------------------------------------ agent: launcher != approver, SSE with header
def _script(inv):
    ev = {"ref": f"Invoice:{inv}", "claim": "seen", "kind": "asserted", "tool_call_id": "q1"}
    return [Turn(tool_uses=[("q1", "query_objects", {"type": "Invoice", "limit": 5})], text="looking"),
            Turn(tool_uses=[("p1", "propose_action", {"action": "pay_invoice", "params": {"invoice": str(inv)},
                                                      "rationale": "due", "evidence": [ev]})]),
            Turn(text="done")]


def test_agent_run_launcher_cannot_approve_other_user_can(make_client):
    scripts = []
    c = make_client(agent_model_factory=lambda p: MockModel(scripts.pop(0)))
    carol, bob = bearer(c, "carol"), bearer(c, "bob")
    inv = open_invoice(c, bob)
    scripts.append(_script(inv))
    rid = c.post("/agent/runs", json={"prompt": "go"}, headers={**carol, "x-role": "admin"}).json()["run_id"]
    for _ in range(300):
        j = c.get(f"/agent/runs/{rid}", headers=carol).json()
        if j["run"]["status"] != "running":
            break
        time.sleep(0.05)
    assert j["run"]["status"] == "completed" and j["run"]["role"] == "finance_analyst" and j["run"]["user"] == "carol"
    pid = j["run"]["proposal_ids"][0]
    row = next(r for r in c.get("/proposals", headers=bob).json() if r["id"] == pid)
    assert row["on_behalf_of"] == "carol" and row["proposer"] == "agent:finance_analyst"
    # another user cannot read carol's run
    assert c.get(f"/agent/runs/{rid}", headers=bearer(c, "alice")).status_code in (403, 404)
    # launcher cannot approve (not an approver role and not the admin)
    assert c.post(f"/proposals/{pid}/approve", headers=carol).status_code == 403
    assert c.post(f"/proposals/{pid}/approve", headers=bob, json={"comment": "ok"}).status_code == 200

    # SSE: Authorization header works; a ticket works once
    frames = []
    with c.stream("GET", f"/agent/runs/{rid}/stream", headers=carol) as r:
        assert r.status_code == 200
        frames = [l for l in r.iter_lines() if l.startswith("event: ")]
    assert frames and frames[-1] == "event: finished"
    assert c.get(f"/agent/runs/{rid}/stream").status_code == 401
    t = c.post("/auth/stream-ticket", headers=carol).json()["ticket"]
    with c.stream("GET", f"/agent/runs/{rid}/stream?ticket={t}") as r:
        assert r.status_code == 200
        assert any(l.startswith("event: finished") for l in r.iter_lines())
    assert c.get(f"/agent/runs/{rid}/stream?ticket={t}").status_code == 401
    # alice's ticket does not open carol's run
    tb = c.post("/auth/stream-ticket", headers=bearer(c, "alice")).json()["ticket"]
    assert c.get(f"/agent/runs/{rid}/stream?ticket={tb}").status_code in (403, 404)


# ------------------------------------------------------------------ auth off: unchanged
def test_auth_off_unchanged(aschema, monkeypatch):
    monkeypatch.delenv("ISNAD_AUTH", raising=False)
    monkeypatch.delenv("ISNAD_DEFAULT_ROLE", raising=False)
    c = TestClient(create_app(ONTOLOGY, schema=aschema, pipeline=False))
    assert c.get("/me").status_code == 403 and c.get("/me").json()["code"] == "role_required"
    assert c.get("/me", headers={"x-role": "sim"}).status_code == 403
    me = c.get("/me", headers={"x-role": "planner"}).json()
    assert me["role"] == "planner" and me["user"] == "demo-planner"
    assert len(c.get("/roles").json()) >= 4
    assert c.get("/health").status_code == 200
    # a Bearer header is irrelevant when auth is off
    assert c.get("/me", headers={"Authorization": "Bearer junk"}).status_code == 403
