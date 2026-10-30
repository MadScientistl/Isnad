"""Phase 5 security-review fixes at the HTTP layer: redaction for shared viewers (L2), input bounds (L4), body size
limit, retryable contention (L6), graph size disclosure (L2/M4c)."""
import sys
import types
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from engine.api.main import ApiError, create_app, map_error
from engine.api.seed import _id
from engine.db import bitemporal as bt
from engine.db import connect

from .conftest import ONTOLOGY

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def H(role, scn=None):
    h = {"x-role": role}
    if scn is not None:
        h["x-scenario"] = str(scn)
    return h


@pytest.fixture(scope="module")
def sch(make_schema):
    return make_schema()


@pytest.fixture(scope="module")
def cl(sch):
    return TestClient(create_app(ONTOLOGY, schema=sch, pipeline=False))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AMARH_DEFAULT_ROLE", raising=False)
    monkeypatch.setenv("AMARH_OUTBOX_WORKER", "0")


@pytest.fixture
def mk(cl):
    made = []

    def make(role="planner", name="t", **body):
        r = cl.post("/scenarios", json={"name": name, **body}, headers=H(role))
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()["id"]
    yield make
    for sid in made:
        cl.delete(f"/scenarios/{sid}", headers=H("admin"))


def test_shared_viewers_do_not_see_counters_or_run_id(cl, mk):
    sid = mk("planner", shared_roles=["finance_analyst"])
    own = cl.get(f"/scenarios/{sid}", headers=H("planner")).json()
    shared = cl.get(f"/scenarios/{sid}", headers=H("finance_analyst")).json()
    adm = cl.get(f"/scenarios/{sid}", headers=H("admin")).json()
    assert own["override_rows"] == 0 and own["data_version"] == 0
    assert adm["override_rows"] == 0
    assert shared["override_rows"] is None and shared["data_version"] is None and shared["run_id"] is None
    lst = [x for x in cl.get("/scenarios", headers=H("finance_analyst")).json() if x["id"] == sid][0]
    assert lst["override_rows"] is None and lst["data_version"] is None
    assert shared["name"] == own["name"]


def test_simulation_digest_and_actions_are_owner_admin_only(cl, mk, monkeypatch):
    import time

    def run(factory, ctx, *, days, seed, injections):
        return types.SimpleNamespace(digest="abc", actions={"ship_arrive": 2}, kpis={}, sim_now=datetime(2026, 2, 1, tzinfo=UTC))
    monkeypatch.setitem(sys.modules, "sim.whatif", types.SimpleNamespace(run=run))
    sid = mk("planner", shared_roles=["finance_analyst"])
    job = cl.post(f"/scenarios/{sid}/simulate", json={"days": 1}, headers=H("planner")).json()["job_id"]
    for _ in range(50):
        if cl.get(f"/scenarios/{sid}/simulate/{job}", headers=H("planner")).json()["status"] != "running":
            break
        time.sleep(0.1)
    mine = cl.get(f"/scenarios/{sid}/simulate/{job}", headers=H("planner")).json()
    theirs = cl.get(f"/scenarios/{sid}/simulate/{job}", headers=H("finance_analyst")).json()
    assert mine["digest"] == "abc" and mine["actions"] == {"ship_arrive": 2}
    assert theirs["status"] == "done" and theirs["digest"] is None and theirs["actions"] is None


@pytest.mark.parametrize("inj", [
    {"kind": "price_shock", "magnitude": float("nan")},
    {"kind": "price_shock", "magnitude": float("inf")},
    {"kind": "price_shock", "magnitude": 0},
    {"kind": "price_shock", "magnitude": 100.5},
    {"kind": "price_shock", "magnitude": -3},
    {"kind": "port_closure", "at_h": -1},
    {"kind": "port_closure", "at_h": 1e12},
    {"kind": "port_closure", "duration_h": 0},
    {"kind": "port_closure", "duration_h": float("inf")},
    {"kind": "port_closure", "target": "not-a-uuid"},
])
def test_injection_bounds_are_rejected(cl, mk, inj):
    sid = mk()
    body = '{"days": 3, "injections": [%s]}' % __import__("json").dumps(inj, allow_nan=True)
    r = cl.post(f"/scenarios/{sid}/simulate", content=body, headers={**H("planner"), "content-type": "application/json"})
    assert r.status_code == 422, (inj, r.text)


def test_too_many_injections_and_bad_days_and_seed(cl, mk):
    sid = mk()
    inj = [{"kind": "port_closure"}] * 21
    assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 3, "injections": inj}, headers=H("planner")).status_code == 422
    assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 3, "seed": -1}, headers=H("planner")).status_code == 422
    assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 10**9}, headers=H("planner")).status_code == 422


def test_text_fields_have_maximum_lengths(cl, mk):
    big = "x" * 4001
    assert cl.post("/scenarios", json={"name": "x" * 201}, headers=H("planner")).status_code == 422
    assert cl.post("/scenarios", content='{"name": "ok", "expires_in_h": NaN}',
                   headers={**H("planner"), "content-type": "application/json"}).status_code == 422
    assert cl.post("/scenarios", json={"name": "ok", "expires_in_h": 100000}, headers=H("planner")).status_code == 422
    sid = mk()
    assert cl.post(f"/scenarios/{sid}/merge", json={"comment": big}, headers=H("planner")).status_code == 422
    assert cl.post(f"/actions/close_port/execute", json={"params": {}, "rationale": big}, headers=H("planner")).status_code == 422
    assert cl.post(f"/actions/close_port/propose", json={"params": {}, "rationale": big}, headers=H("planner")).status_code == 422
    assert cl.post("/proposals/%s/approve" % uuid.uuid4(), json={"comment": big}, headers=H("admin")).status_code == 422
    assert cl.post("/executions/%s/rollback" % uuid.uuid4(), json={"reason": big}, headers=H("admin")).status_code == 422


def test_request_body_size_limit(cl):
    r = cl.post("/scenarios", content=b'{"name": "' + b"a" * (2 * 1024 * 1024 + 10) + b'"}',
                headers={**H("planner"), "content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "body_too_large"
    # a normal request is unaffected
    assert cl.get("/scenarios", headers=H("planner")).status_code == 200


def test_price_index_must_be_below_1000(cl, mk, sch):
    c = connect(schema=sch)
    try:
        sup = str(c.execute("SELECT id FROM cur_obj_supplier LIMIT 1").fetchone()[0])
    finally:
        c.close()
    sid = mk("admin")
    ok = cl.post("/actions/set_supplier_price_index/execute", json={"params": {"supplier": sup, "price_index": 2.0}},
                 headers=H("admin", sid))
    assert ok.status_code == 200, ok.text
    bad = cl.post("/actions/set_supplier_price_index/execute", json={"params": {"supplier": sup, "price_index": 1000}},
                  headers=H("admin", sid))
    assert bad.status_code == 422


def test_contention_is_a_retryable_409_busy_not_a_500():
    for e in (psycopg.errors.DeadlockDetected("deadlock detected"), psycopg.errors.LockNotAvailable("lock timeout")):
        ae = map_error(e)
        assert isinstance(ae, ApiError) and ae.status == 409 and ae.body["code"] == "busy"


def test_graph_too_large_is_disclosed_only_to_owner_and_admin(cl, mk, monkeypatch):
    monkeypatch.setenv("AMARH_SCN_GRAPH_MAX_OBJECTS", "2")
    sid = mk("planner", shared_roles=["finance_analyst"])
    p = _id("Port", "Rotterdam")
    own = cl.get(f"/graph?center={p}&depth=1", headers=H("planner", sid))
    assert own.status_code == 413 and own.json()["code"] == "graph_too_large"
    assert cl.get(f"/graph?center={p}&depth=1", headers=H("admin", sid)).status_code == 413
    shared = cl.get(f"/graph?center={p}&depth=1", headers=H("finance_analyst", sid))
    assert shared.status_code == 403 and shared.json()["code"] == "graph_unavailable"
    assert "object" not in shared.text and "limit" not in shared.text
    q = cl.post("/sparql", json={"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}, headers=H("finance_analyst", sid))
    assert q.status_code == 403 and q.json()["code"] == "graph_unavailable"
