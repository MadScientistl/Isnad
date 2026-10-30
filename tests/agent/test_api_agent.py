"""Agent HTTP API (A2): run lifecycle, SSE, ownership, limits, approval flow. MockModel only, no network."""
import json
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agent import MockModel, Turn
from agent_helpers import COMP, ROOT, tu
from engine.api.main import create_app
from engine.db import bitemporal as bt
from engine.actions import propose

ONTOLOGY = str(ROOT / "ontology" / "supply_chain.yaml")


def H(role):
    return {"x-role": role}


def ev(ref, call="q1", kind="asserted", claim="seen"):
    return {"ref": ref, "claim": claim, "kind": kind, "tool_call_id": call}


def pay_script(inv, text="done"):
    return [Turn(tool_uses=[tu("q1", "query_objects", type="Invoice", limit=5)], text="looking"),
            Turn(tool_uses=[tu("p1", "propose_action", action="pay_invoice", params={"invoice": str(inv)},
                               rationale="due", evidence=[ev(f"Invoice:{inv}")])]),
            Turn(text=text)]


def delay_script(ship):
    return [Turn(tool_uses=[tu("q1", "query_objects", type="Shipment", limit=5)]),
            Turn(tool_uses=[tu("p1", "propose_action", action="delay_shipment",
                               params={"shipment": str(ship), "delay_hours": 5.0, "reason": "storm"},
                               rationale="closed", evidence=[ev(f"Shipment:{ship}")])]),
            Turn(text="proposed")]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AMARH_DEFAULT_ROLE", raising=False)
    for k in ("AMARH_AGENT_MAX_CONCURRENT", "AMARH_AGENT_RUNS_PER_MIN", "AMARH_AGENT_DAILY_USD",
              "AMARH_AGENT_MAX_GLOBAL"):
        monkeypatch.delenv(k, raising=False)


class Api:
    def __init__(self, aschema):
        self.scripts = []                       # one script (list of Turn) per started run, in order
        self.models = []
        self.app = create_app(ONTOLOGY, schema=aschema, pipeline=False, agent_model_factory=self._factory)
        self.c = TestClient(self.app)

    def _factory(self, _p):
        m = MockModel(self.scripts.pop(0))
        self.models.append(m)
        return m

    def run(self, role, script, prompt="go"):
        self.scripts.append(script)
        r = self.c.post("/agent/runs", json={"prompt": prompt}, headers=H(role))
        assert r.status_code == 201, r.text
        return r.json()["run_id"]

    def wait(self, run_id, role="admin", timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            j = self.c.get(f"/agent/runs/{run_id}", headers=H(role)).json()
            if j["run"]["status"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("run did not finish")

    def sse(self, run_id, role, **kw):
        frames = []
        with self.c.stream("GET", f"/agent/runs/{run_id}/stream", headers={**H(role), **kw.pop("headers", {})},
                           **kw) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            cur = {}
            for line in r.iter_lines():
                if line.startswith("id: "):
                    cur["id"] = int(line[4:])
                elif line.startswith("event: "):
                    cur["event"] = line[7:]
                elif line.startswith("data: "):
                    cur["data"] = json.loads(line[6:])
                elif line == "" and cur:
                    frames.append(cur)
                    cur = {}
        return frames


@pytest.fixture
def api(world, aschema):
    a = Api(aschema)
    yield a
    for e in list(a.app.state.agent_registry.runs.values()):
        e.ctx.cancel.set()
    time.sleep(0.1)


# ---------------------------------------------------------------- full flow
def test_full_flow_propose_stream_approve(api, world, conn):
    inv = world["inv"]
    rid = api.run("finance_analyst", pay_script(inv))
    j = api.wait(rid, "finance_analyst")
    assert j["run"]["status"] == "completed" and j["run"]["proposals"] == 1 and j["run"]["turns"] == 3
    assert j["run"]["role"] == "finance_analyst" and j["run"]["user"] == "demo-finance_analyst"
    assert j["run"]["tokens_in"] > 0 and j["run"]["cost_usd"] > 0 and j["run"]["tool_calls"] == 2
    kinds = [e["kind"] for e in j["events"]]
    assert kinds == ["message", "tool_call", "tool_result", "message", "tool_call", "tool_result", "proposal",
                     "message", "finished"]
    seqs = [e["seq"] for e in j["events"]]
    assert seqs == sorted(seqs)

    # stream replays the stored events and closes after `finished`
    frames = api.sse(rid, "finance_analyst")
    assert [f["event"] for f in frames] == kinds
    assert frames[-1]["data"]["payload"]["status"] == "completed" and frames[0]["id"] == frames[0]["data"]["seq"]

    # proposal visible to the launching role and to admin, with the agent fields; hidden from planner
    pid = j["run"]["proposal_ids"][0]
    rows = api.c.get("/proposals", headers=H("finance_analyst")).json()
    row = next(r for r in rows if r["id"] == pid)
    assert row["on_behalf_of"] == "demo-finance_analyst" and row["proposer"] == "agent:finance_analyst"
    assert row["run_id"] == rid and row["evidence"] and row["expired"] is False and row["stale"] is None
    assert any(r["id"] == pid for r in api.c.get("/proposals", headers=H("admin")).json())
    assert not any(r["id"] == pid for r in api.c.get("/proposals", headers=H("planner")).json())

    # the launching user (and a non-approver role) cannot approve
    r = api.c.post(f"/proposals/{pid}/approve", headers=H("finance_analyst"))
    assert r.status_code == 403
    # admin approves: executes as the approver
    r = api.c.post(f"/proposals/{pid}/approve", headers=H("admin"), json={"comment": "ok"})
    assert r.status_code == 200, r.text
    assert bt.get_object(conn, "Invoice", inv, masked=False)["status"] == "paid"
    ex = conn.execute("SELECT actor, on_behalf_of, proposal_id::text, source, status FROM audit_event "
                      "WHERE proposal_id = %s AND status = 'executed'", [pid]).fetchall()
    assert ex == [("demo-admin", "demo-finance_analyst", pid, "api", "executed")]
    # approving twice is a conflict
    assert api.c.post(f"/proposals/{pid}/approve", headers=H("admin")).status_code == 409


def test_stale_and_expired_give_409(api, world, conn):
    inv = world["inv_other"]
    rid = api.run("finance_analyst", pay_script(inv))
    pid = api.wait(rid)["run"]["proposal_ids"][0]
    cur = bt.get_object(conn, "Invoice", inv, masked=False)
    props = {k: v for k, v in cur.items() if k in ("ref", "amount", "status")}
    props["amount"] = 999.0
    bt.write_object(conn, "Invoice", inv, props, datetime.now(UTC), "test")
    conn.commit()
    r = api.c.post(f"/proposals/{pid}/approve", headers=H("admin"))
    assert r.status_code == 409 and r.json()["code"] == "stale_proposal" and r.json()["changed"]

    # an expired proposal (created directly with a negative ttl)
    from engine.actions import make_principal
    agent = make_principal("finance_analyst", "agent:finance_analyst", COMP)
    pid2 = propose(conn, "pay_invoice", {"invoice": str(world["inv"])}, agent, "x", valid_at=datetime.now(UTC),
                   on_behalf_of="demo-finance_analyst", ttl_h=-1, ontology=COMP)
    r = api.c.post(f"/proposals/{pid2}/approve", headers=H("admin"))
    assert r.status_code == 409 and r.json()["code"] == "proposal_expired"
    row = next(x for x in api.c.get("/proposals", headers=H("admin")).json() if x["id"] == str(pid2))
    assert row["expired"] is True


def test_planner_run_streams_live_in_order_with_deltas(api, world):
    gate = threading.Event()
    s = world["s_emea"]
    script = delay_script(s)
    script[0] = Turn(tool_uses=[tu("q1", "query_objects", type="Shipment", limit=5)], text="thinking",
                     hook=lambda: gate.wait(5))
    rid = api.run("planner", script)
    gate.set()
    frames = api.sse(rid, "planner")
    names = [f["event"] for f in frames]
    assert names[-1] == "finished" and "delta" in names and "proposal" in names
    seqs = [f["id"] for f in frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert names.index("tool_call") < names.index("tool_result") < names.index("proposal")
    # deltas are live-only: not in the stored events
    stored = api.wait(rid, "planner")["events"]
    assert "delta" not in [e["kind"] for e in stored]
    assert [e["seq"] for e in stored] == [f["id"] for f in frames if f["event"] != "delta"]
    p = api.c.get("/proposals", headers=H("planner")).json()
    assert any(r["run_id"] == rid and r["action"] == "delay_shipment" for r in p)


def test_reconnect_replays_after_seq(api, world):
    rid = api.run("planner", delay_script(world["s_emea"]))
    j = api.wait(rid, "planner")
    seqs = [e["seq"] for e in j["events"]]
    mid = seqs[3]
    f1 = api.sse(rid, "planner", headers={"Last-Event-ID": str(mid)})
    assert [f["id"] for f in f1] == [s for s in seqs if s > mid]
    f2 = api.sse(rid, "planner", params={"after": mid})
    assert [f["id"] for f in f2] == [f["id"] for f in f1]
    assert api.sse(rid, "planner", params={"after": seqs[-1]}) == []


# ---------------------------------------------------------------- ownership
def test_run_ownership_is_404_for_other_roles(api, world):
    rid = api.run("planner", delay_script(world["s_emea"]))
    api.wait(rid, "planner")
    for role in ("finance_analyst", "regional_ops_lead"):
        assert api.c.get(f"/agent/runs/{rid}", headers=H(role)).status_code == 404
        assert api.c.get(f"/agent/runs/{rid}/stream", headers=H(role)).status_code == 404
        assert api.c.post(f"/agent/runs/{rid}/cancel", headers=H(role)).status_code == 404
        assert rid not in [r["id"] for r in api.c.get("/agent/runs", headers=H(role)).json()]
    assert api.c.get(f"/agent/runs/{rid}", headers=H("admin")).status_code == 200
    assert rid in [r["id"] for r in api.c.get("/agent/runs", headers=H("admin")).json()]
    assert rid in [r["id"] for r in api.c.get("/agent/runs?limit=5", headers=H("planner")).json()]
    assert api.c.get("/agent/runs/not-a-uuid", headers=H("planner")).status_code == 404
    assert api.c.get("/agent/runs/00000000-0000-0000-0000-000000000000", headers=H("planner")).status_code == 404


# ---------------------------------------------------------------- limits
def test_concurrency_limit_then_cancel(api, world):
    gate = threading.Event()
    first = [Turn(tool_uses=[tu("q1", "query_objects", type="Shipment", limit=1)], hook=lambda: gate.wait(10)),
             Turn(text="never")]
    rid = api.run("planner", first)
    api.scripts.append([Turn(text="x")])
    r = api.c.post("/agent/runs", json={"prompt": "again"}, headers=H("planner"))
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and r.json()["retry_after"] >= 1
    assert api.c.get(f"/agent/runs/{rid}", headers=H("planner")).json()["run"]["status"] == "running"
    # a different user is not blocked by it
    api.scripts.append([Turn(text="fine")])
    assert api.c.post("/agent/runs", json={"prompt": "other"}, headers=H("finance_analyst")).status_code == 201
    # cancel: the run ends `cancelled` once the model call returns
    c = api.c.post(f"/agent/runs/{rid}/cancel", headers=H("planner"))
    assert c.status_code == 200 and c.json()["status"] == "cancelling"
    gate.set()
    j = api.wait(rid, "planner")
    assert j["run"]["status"] == "cancelled" and j["events"][-1]["kind"] == "finished"
    # after it ends the user can start again, and cancel of a finished run just reports its status
    assert api.c.post(f"/agent/runs/{rid}/cancel", headers=H("planner")).json()["status"] == "cancelled"
    api.wait(api.run("planner", [Turn(text="ok")]), "planner")


def test_per_minute_limit(api, monkeypatch):
    monkeypatch.setenv("AMARH_AGENT_RUNS_PER_MIN", "2")
    for _ in range(2):
        api.wait(api.run("planner", [Turn(text="ok")]), "planner")
    api.scripts.append([Turn(text="ok")])
    r = api.c.post("/agent/runs", json={"prompt": "3rd"}, headers=H("planner"))
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "minute" in r.json()["detail"]
    assert r.headers["retry-after"]


def test_daily_budget_limit(api, monkeypatch):
    rid = api.run("planner", [Turn(text="ok", usage=(1_000_000, 1_000_000))])   # about USD 12
    api.wait(rid, "planner")
    api.scripts.append([Turn(text="ok")])
    r = api.c.post("/agent/runs", json={"prompt": "more"}, headers=H("planner"))
    assert r.status_code == 429 and "budget" in r.json()["detail"]
    monkeypatch.setenv("AMARH_AGENT_DAILY_USD", "1000")
    api.scripts.append([Turn(text="ok")])
    assert api.c.post("/agent/runs", json={"prompt": "more"}, headers=H("planner")).status_code == 201


def test_global_cap(api, monkeypatch):
    monkeypatch.setenv("AMARH_AGENT_MAX_GLOBAL", "1")
    gate = threading.Event()
    api.run("planner", [Turn(text="x", hook=lambda: gate.wait(10))])
    api.scripts.append([Turn(text="y")])
    r = api.c.post("/agent/runs", json={"prompt": "p"}, headers=H("finance_analyst"))
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    gate.set()


def test_concurrent_posts_cannot_both_pass(api):
    gate = threading.Event()
    api.scripts.extend([[Turn(text="x", hook=lambda: gate.wait(10))] for _ in range(4)])
    out = []

    def go():
        out.append(api.c.post("/agent/runs", json={"prompt": "race"}, headers=H("planner")).status_code)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    gate.set()
    assert sorted(out) == [201, 429, 429, 429]


# ---------------------------------------------------------------- validation, identity, availability
def test_missing_key_is_503(world, aschema, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = TestClient(create_app(ONTOLOGY, schema=aschema, pipeline=False))
    r = c.post("/agent/runs", json={"prompt": "hi"}, headers=H("planner"))
    assert r.status_code == 503 and r.json()["code"] == "agent_unavailable"
    assert c.get("/agent/config", headers=H("planner")).json()["available"] is False


def test_prompt_validation(api):
    for body in ({"prompt": "x" * 4001}, {"prompt": ""}, {"prompt": "   "}, {}):
        assert api.c.post("/agent/runs", json=body, headers=H("planner")).status_code == 422
    api.scripts.append([Turn(text="ok")])
    assert api.c.post("/agent/runs", json={"prompt": "x" * 4000}, headers=H("planner")).status_code == 201


def test_identity_headers(api):
    assert api.c.post("/agent/runs", json={"prompt": "hi"}).status_code == 403
    assert api.c.post("/agent/runs", json={"prompt": "hi"}, headers=H("nobody")).status_code == 403
    assert api.c.post("/agent/runs", json={"prompt": "hi"}, headers=H("sim")).status_code == 403
    assert api.c.get("/agent/runs", headers=H("nobody")).status_code == 403
    assert api.c.get("/agent/config").status_code == 403


def test_body_cannot_set_role_or_user(api, conn):
    for extra in ({"role": "admin"}, {"user": "demo-admin"}, {"principal": {"role": "admin"}}):
        r = api.c.post("/agent/runs", json={"prompt": "hi", **extra}, headers=H("planner"))
        assert r.status_code == 422
    for q in ("?role=admin", "?user=demo-admin"):   # query params are ignored, identity stays the header's
        api.scripts.append([Turn(text="ok")])
        r = api.c.post("/agent/runs" + q, json={"prompt": "hi"}, headers=H("planner"))
        assert r.status_code == 201
        j = api.wait(r.json()["run_id"], "planner")
        assert j["run"]["role"] == "planner" and j["run"]["user"] == "demo-planner"
        time.sleep(0.05)
    assert conn.execute("SELECT count(*) FROM agent_run WHERE role = 'admin'").fetchone()[0] == 0


def test_config_per_role(api):
    c = api.c.get("/agent/config", headers=H("finance_analyst")).json()
    assert c["role"] == "finance_analyst" and "propose_action" in c["tools"]
    acts = {a["name"]: a for a in c["actions"]}
    assert set(acts) == {"flag_invoice", "pay_invoice"} and acts["pay_invoice"]["requires_approval"] is True
    assert acts["pay_invoice"]["params"] == {"invoice": "uuid!"} and acts["pay_invoice"]["approvers"] == ["admin"]
    assert c["caps"]["max_tool_calls"] == 25 and c["limits"]["max_concurrent"] == 1 and c["available"] is True
    p = api.c.get("/agent/config", headers=H("planner")).json()
    assert {a["name"] for a in p["actions"]} == {"delay_shipment", "reroute_shipment", "close_port"}


def test_model_failure_does_not_leak(api, world):
    secret = "sk-ant-SECRET-upstream-body"

    def boom(system, messages, tools):
        raise RuntimeError(secret)
    rid = api.run("planner", [boom])
    j = api.wait(rid, "planner")
    assert j["run"]["status"] == "error"
    blob = json.dumps(j)
    assert secret not in blob and "model_error" in blob
    assert secret not in json.dumps(api.sse(rid, "planner"))


def test_interrupted_when_not_in_registry(api, aschema, conn, world):
    """A run row without a `finished` event and no live worker reads as `interrupted`; the stream closes."""
    import uuid
    from agent import DbSink
    from engine.actions import make_principal
    p = make_principal("planner", "demo-planner", COMP)
    sink = DbSink(lambda: __import__("engine.db", fromlist=["connect"]).connect(schema=aschema), p)
    rid = str(uuid.uuid4())
    sink.run_started(rid, p, "orphan", "mock-model", {})
    sink.emit({"seq": 1, "kind": "message", "payload": {"text": "hi"}, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0})
    sink.close()
    j = api.c.get(f"/agent/runs/{rid}", headers=H("planner")).json()
    assert j["run"]["status"] == "interrupted" and len(j["events"]) == 1
    frames = api.sse(rid, "planner")
    assert [f["event"] for f in frames] == ["message", "finished"]
    assert frames[-1]["data"]["payload"]["status"] == "interrupted"
