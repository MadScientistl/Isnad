"""Scenario routes + the x-scenario header on the data routes (Phase 5)."""
import sys
import time
import types
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from engine.api.main import create_app
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
        made.append((role, r.json()["id"]))
        return r.json()["id"]
    yield make
    for role, sid in made:
        cl.delete(f"/scenarios/{sid}", headers=H("admin"))


@pytest.fixture
def ship(sch):
    """A fresh shipment (cargo 1000) shipping via Rotterdam (EMEA) to Venlo."""
    c = connect(schema=sch)
    i = uuid.uuid4()
    bt.write_object(c, "Shipment", i, {"ref": f"S-{i.hex[:6]}", "status": "in_transit", "cargo_value": 1000.0}, T0, "t")
    bt.write_link(c, "ships_via", i, _id("Port", "Rotterdam"), T0, "t")
    bt.write_link(c, "ships_to", i, _id("Warehouse", "Venlo DC"), T0, "t")
    c.commit()
    c.close()
    return str(i)


def port():
    return str(_id("Port", "Rotterdam"))


# --------------------------------------------------------------- header handling
def test_header_validation(cl, mk):
    sid = mk()
    assert cl.get("/objects/Port?limit=1", headers=H("planner", sid)).status_code == 200
    r = cl.get("/objects/Port", headers={**H("planner"), "x-scenario": "abc"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_scenario"
    assert cl.get("/objects/Port", headers={**H("planner"), "x-scenario": "-3"}).status_code == 400
    r = cl.get("/objects/Port", headers=H("planner", 987654))
    assert r.status_code == 404 and r.json()["code"] == "scenario_not_found"
    assert cl.get("/objects/Port?limit=1", headers=H("planner", 0)).status_code == 200      # 0 = main
    assert cl.get("/objects/Port?limit=1", headers={**H("planner"), "x-scenario": ""}).status_code == 200


def test_foreign_scenario_is_404_everywhere(cl, mk):
    sid = mk("planner")
    for method, path in (("get", "/objects/Port"), ("get", "/stats/summary"), ("get", "/events"),
                         ("get", "/proposals"), ("get", "/executions"), ("get", "/audit"), ("get", "/actions"),
                         ("get", "/geo/Port"), ("get", "/stats/series?metric=value_at_risk")):
        r = getattr(cl, method)(path, headers=H("finance_analyst", sid))
        assert r.status_code == 404 and r.json()["code"] == "scenario_not_found", path
    for role in ("finance_analyst", "regional_ops_lead"):
        assert cl.get(f"/scenarios/{sid}", headers=H(role)).status_code == 404
        assert cl.get(f"/scenarios/{sid}/compare", headers=H(role)).status_code == 404
        assert cl.post(f"/scenarios/{sid}/merge", json={}, headers=H(role)).status_code == 404
        assert cl.delete(f"/scenarios/{sid}", headers=H(role)).status_code == 404
        assert cl.get(f"/scenarios/{sid}/series?metric=value_at_risk", headers=H(role)).status_code == 404
        assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 1}, headers=H(role)).status_code == 404
    assert cl.get(f"/scenarios/{sid}", headers=H("admin")).status_code == 200


def test_unsupported_routes_are_400(cl, mk):
    sid = mk()
    for method, path in (("get", "/sim/state"), ("post", "/sim/control"), ("post", "/sim/inject"),
                         ("post", f"/webhooks/{uuid.uuid4()}/retry"),
                         ("post", f"/proposals/{uuid.uuid4()}/approve"),
                         ("post", f"/proposals/{uuid.uuid4()}/reject")):
        r = getattr(cl, method)(path, headers=H("admin", sid))
        assert r.status_code == 400 and r.json()["code"] == "scenario_unsupported", path
    assert cl.get("/sim/state", headers=H("admin")).status_code != 400                           # main unaffected


def test_graph_and_sparql_have_a_pending_hook(cl, mk):
    sid = mk()
    r = cl.get(f"/graph?center={port()}", headers=H("planner", sid))
    assert r.status_code == 501 and r.json()["code"] == "scenario_graph_pending"
    r = cl.post("/sparql", json={"query": "SELECT * WHERE {?s ?p ?o} LIMIT 1"}, headers=H("planner", sid))
    assert r.status_code == 501 and r.json()["code"] == "scenario_graph_pending"


# ------------------------------------------------------------------ scenario CRUD
def test_crud_and_list_isolation(cl, mk):
    a = mk("planner", "mine")
    sh = mk("admin", "shared", shared_roles=["finance_analyst"])
    ids = lambda role: {s["id"] for s in cl.get("/scenarios", headers=H(role)).json()}  # noqa: E731
    assert ids("planner") == {a} and ids("regional_ops_lead") == set()
    assert {a, sh} <= ids("admin") and ids("finance_analyst") == {sh}
    d = cl.get(f"/scenarios/{a}", headers=H("planner")).json()
    assert set(d) >= {"id", "name", "owner_user", "owner_role", "status", "base_recorded", "base_sim_time", "clock",
                      "shared_roles", "created_at", "expires_at", "run_id", "override_rows", "data_version"}
    assert d["owner_user"] == "demo-planner" and d["status"] == "open"
    # shared viewer cannot discard
    assert cl.delete(f"/scenarios/{sh}", headers=H("finance_analyst")).status_code == 403
    assert cl.delete(f"/scenarios/{a}", headers=H("planner")).json() == {"id": a, "status": "discarded"}
    assert cl.get("/scenarios?status=all", headers=H("planner")).json()[0]["status"] == "discarded"
    assert cl.get("/scenarios", headers=H("planner")).json() == []
    assert cl.post("/scenarios", json={"name": ""}, headers=H("planner")).status_code == 422
    assert cl.post("/scenarios", json={"name": "x", "shared_roles": ["nope"]}, headers=H("planner")).status_code == 422
    assert cl.get("/scenarios?status=bogus", headers=H("planner")).status_code == 422


def test_limit_429(cl, mk, monkeypatch):
    monkeypatch.setenv("AMARH_SCN_MAX_PER_USER", "1")
    mk("finance_analyst")
    r = cl.post("/scenarios", json={"name": "two"}, headers=H("finance_analyst"))
    assert r.status_code == 429 and r.json()["code"] == "scenario_limit"


# ---------------------------------------------- mutations under the header write the overlay only
def test_rest_mutation_under_header_is_overlay_only(cl, mk, ship, sch):
    sid = mk()
    r = cl.put(f"/objects/Shipment/{ship}", json={"ref": "RENAMED", "status": "delayed", "cargo_value": 1000.0},
               headers=H("planner", sid))
    assert r.status_code == 200 and r.json()["ref"] == "RENAMED"
    assert cl.get(f"/objects/Shipment/{ship}", headers=H("planner", sid)).json()["status"] == "delayed"
    assert cl.get(f"/objects/Shipment/{ship}", headers=H("planner")).json()["status"] == "in_transit"
    hist = cl.get(f"/objects/Shipment/{ship}/history", headers=H("planner", sid)).json()
    assert hist and cl.get(f"/objects/Shipment/{ship}/history", headers=H("planner")).json()
    new = str(uuid.uuid4())
    assert cl.post("/objects/Warehouse", json={"id": new, "name": "Only here"}, headers=H("planner", sid)).status_code == 403
    r = cl.post("/objects/Event", json={"id": new, "kind": "note"}, headers=H("planner", sid))
    assert r.status_code == 201
    assert cl.get(f"/objects/Event/{new}", headers=H("planner", sid)).status_code == 200
    assert cl.get(f"/objects/Event/{new}", headers=H("planner")).status_code == 404
    assert new not in {o["id"] for o in cl.get("/objects/Event?limit=1000", headers=H("planner")).json()}
    assert new in {o["id"] for o in cl.get("/objects/Event?limit=1000", headers=H("planner", sid)).json()}
    # links
    other = str(_id("Port", "Hamburg"))
    assert cl.post("/links/ships_via", json={"src": ship, "dst": other}, headers=H("planner", sid)).status_code == 201
    assert other in {l["dst"] for l in cl.get("/links/ships_via", headers=H("planner", sid)).json() if l["src"] == ship}
    assert other not in {l["dst"] for l in cl.get("/links/ships_via", headers=H("planner")).json() if l["src"] == ship}
    n = cl.get(f"/neighbours/Shipment/{ship}", headers=H("planner", sid)).json()
    assert other in {x["id"] for x in n if x["link"] == "ships_via"}
    assert other not in {x["id"] for x in cl.get(f"/neighbours/Shipment/{ship}", headers=H("planner")).json()}
    assert cl.delete(f"/links/ships_via?src={ship}&dst={other}", headers=H("planner", sid)).status_code == 200
    assert cl.delete(f"/objects/Event/{new}", headers=H("planner", sid)).status_code == 200
    assert cl.get(f"/objects/Event/{new}", headers=H("planner", sid)).status_code == 404
    # nothing of it on main, in main's audit or outbox
    c = connect(schema=sch)
    assert c.execute("SELECT count(*) FROM obj_shipment WHERE id = %s AND scenario_id = 0 AND "
                     "upper_inf(recorded_during)", [ship]).fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM audit_event WHERE scenario_id = 0 AND params::text LIKE %s",
                     [f"%{ship}%"]).fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM graph_outbox WHERE id = %s", [uuid.UUID(new)]).fetchone()[0] == 0
    c.close()


def test_governance_reads_follow_the_header(cl, mk, ship):
    sid = mk()
    r = cl.post("/actions/close_port/execute", json={"params": {"port": port(), "reason": "x"}},
                headers=H("planner", sid))
    assert r.status_code == 200 and r.json()["webhooks"] == []
    aid = r.json()["action_id"]
    assert aid in {e["action_id"] for e in cl.get("/events", headers=H("planner", sid)).json()}
    assert aid not in {e["action_id"] for e in cl.get("/events", headers=H("planner")).json()}
    assert aid in {e["action_id"] for e in cl.get("/executions", headers=H("planner", sid)).json()}
    assert aid not in {e["action_id"] for e in cl.get("/executions", headers=H("planner")).json()}
    assert aid not in {e["action_id"] for e in cl.get("/executions", headers=H("admin")).json()}          # not even admin on main
    assert cl.get(f"/executions/{aid}", headers=H("planner", sid)).status_code == 200
    assert cl.get(f"/executions/{aid}", headers=H("planner")).status_code == 404
    assert aid in {a["action_id"] for a in cl.get("/audit?kind=execution", headers=H("planner", sid)).json()}
    assert aid not in {a["action_id"] for a in cl.get("/audit?kind=execution", headers=H("planner")).json()}
    assert cl.get("/audit?kind=webhook", headers=H("planner", sid)).json() == []
    # the event summary reads scenario-effective names
    summ = [e["summary"] for e in cl.get("/events", headers=H("planner", sid)).json() if e["action_id"] == aid]
    assert summ and "Rotterdam" in summ[0]
    # rollback inside the scenario only
    assert cl.post(f"/executions/{aid}/rollback", json={"reason": "r"}, headers=H("planner")).status_code == 404
    rb = cl.post(f"/executions/{aid}/rollback", json={"reason": "r"}, headers=H("planner", sid))
    assert rb.status_code == 200 and rb.json()["changed"]
    s = cl.get("/stats/summary", headers=H("planner", sid)).json()
    assert s["ports"]["closed"] == 0 and set(s) >= {"shipments", "ports", "invoices", "value_at_risk", "open_events"}


def test_stats_geo_actions_in_scenario(cl, mk, ship):
    sid = mk()
    cl.post("/actions/close_port/execute", json={"params": {"port": port()}}, headers=H("planner", sid))
    main = cl.get("/stats/summary", headers=H("planner")).json()
    scn = cl.get("/stats/summary", headers=H("planner", sid)).json()
    assert scn["ports"]["closed"] == main["ports"]["closed"] + 1
    assert scn["value_at_risk"] >= main["value_at_risk"] + 1000.0
    feats = cl.get("/geo/Port", headers=H("planner", sid)).json()["features"]
    assert {f["properties"]["status"] for f in feats if f["id"] == port()} == {"closed"}
    sf = cl.get("/geo/Shipment", headers=H("planner", sid)).json()["features"]
    assert sf is not None
    s = cl.get("/stats/series?metric=value_at_risk&step_hours=24", headers=H("planner", sid)).json()
    assert s["metric"] == "value_at_risk" and s["points"]
    acts = cl.get(f"/actions?type=Port&id={port()}", headers=H("planner", sid)).json()
    assert any(a["name"] == "reopen_port" for a in acts)


# -------------------------------------------------------- compare / merge / series
def test_compare_merge_flow(cl, mk, ship):
    sid = mk()
    cl.put(f"/objects/Shipment/{ship}", json={"ref": "R", "status": "delayed", "cargo_value": 1000.0},
           headers=H("planner", sid))
    c = cl.get(f"/scenarios/{sid}/compare", headers=H("planner")).json()
    assert c["a"]["id"] == sid and c["b"] == {"id": "main"} and c["truncated"] is False
    d = [x for x in c["diff"] if x["key"] == ship][0]
    assert d["before"]["status"] == "in_transit" and d["after"]["status"] == "delayed" and d["op"] == "modify"
    assert c["kpis"]["delta"]["shipments_delayed"] == 1
    assert cl.get(f"/scenarios/{sid}/compare?with=x", headers=H("planner")).status_code == 422
    assert cl.get(f"/scenarios/{sid}/compare?with=987654", headers=H("planner")).status_code == 404
    # conflict: main moves on
    cl.post("/actions/ship_arrive/execute", json={"params": {"shipment": ship}}, headers=H("planner"))
    r = cl.post(f"/scenarios/{sid}/merge", json={}, headers=H("planner"))
    assert r.status_code == 409 and r.json()["code"] == "merge_conflict"
    assert r.json()["conflicts"][0]["key"] == ship and r.json()["conflicts"][0]["type"] == "Shipment"
    assert cl.get(f"/objects/Shipment/{ship}", headers=H("planner")).json()["status"] == "arrived"
    assert cl.get(f"/scenarios/{sid}", headers=H("planner")).json()["status"] == "open"
    # a clean one
    s2 = mk()
    cl.post("/actions/close_port/execute", json={"params": {"port": str(_id("Port", "Busan"))}}, headers=H("planner", s2))
    m = cl.post(f"/scenarios/{s2}/merge", json={"comment": "go"}, headers=H("planner"))
    assert m.status_code == 200 and m.json()["status"] == "merged" and m.json()["merged"][0]["name"] == "close_port"
    assert cl.get(f"/objects/Port/{_id('Port', 'Busan')}", headers=H("planner")).json()["status"] == "closed"
    assert cl.get(f"/scenarios/{s2}", headers=H("planner")).json()["status"] == "merged"
    assert cl.post(f"/scenarios/{s2}/merge", json={}, headers=H("planner")).status_code == 404


def test_series_route(cl, mk, ship):
    sid = mk()
    r = cl.get(f"/scenarios/{sid}/series?metric=value_at_risk&step_hours=24", headers=H("planner"))
    assert r.status_code == 200 and r.json()["metric"] == "value_at_risk" and len(r.json()["points"]) >= 2
    assert cl.get(f"/scenarios/{sid}/series?metric=nope", headers=H("planner")).status_code == 422


# ------------------------------------------------------------------- simulation
def test_simulate_without_whatif_reports_error(cl, mk, monkeypatch):
    monkeypatch.setitem(sys.modules, "sim.whatif", None)            # import fails => not available
    sid = mk()
    r = cl.post(f"/scenarios/{sid}/simulate", json={"days": 3}, headers=H("planner"))
    assert r.status_code == 202
    j = poll(cl, sid, r.json()["job_id"], "planner")
    assert j["status"] == "error" and j["error"] == "whatif not available"
    assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 0}, headers=H("planner")).status_code == 422
    assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 91}, headers=H("planner")).status_code == 422
    assert cl.get(f"/scenarios/{sid}/simulate/nope", headers=H("planner")).status_code == 404


def poll(cl, sid, job, role, tries=100):
    for _ in range(tries):
        j = cl.get(f"/scenarios/{sid}/simulate/{job}", headers=H(role)).json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_simulate_with_stub_whatif(cl, mk, monkeypatch):
    calls = []

    def run(factory, ctx, *, days, seed, injections):
        calls.append((ctx.id, days, seed, injections))
        return types.SimpleNamespace(digest="abc", actions={"ship_arrive": 2}, kpis={"x": 1}, sim_now=datetime(2026, 2, 1, tzinfo=UTC))
    monkeypatch.setitem(sys.modules, "sim.whatif", types.SimpleNamespace(run=run))
    sid = mk()
    r = cl.post(f"/scenarios/{sid}/simulate", headers=H("planner"),
                json={"days": 7, "seed": 5, "injections": [{"kind": "port_closure", "target": port(), "at_h": 1, "duration_h": 10}]})
    assert r.status_code == 202
    j = poll(cl, sid, r.json()["job_id"], "planner")
    assert j["status"] == "done" and j["digest"] == "abc" and j["actions"] == {"ship_arrive": 2}
    assert set(j["kpis"]) >= {"value_at_risk", "extra_cost", "exposure_suppliers"}      # recomputed for the viewer
    assert calls[0][0] == sid and calls[0][1:3] == (7, 5) and calls[0][3][0]["kind"] == "port_closure"
    ev = cl.get(f"/scenarios/{sid}", headers=H("planner")).json()
    assert ev["status"] == "open"
    # another scenario cannot read this job
    other = mk("admin")
    assert cl.get(f"/scenarios/{other}/simulate/{r.json()['job_id']}", headers=H("admin")).status_code == 404
    # only the owner / admin may simulate
    sh = mk("planner", shared_roles=["finance_analyst"])
    assert cl.post(f"/scenarios/{sh}/simulate", json={"days": 1}, headers=H("finance_analyst")).status_code == 403
    assert cl.post("/scenarios/%d/simulate" % sh, json={"days": 1, "injections": [{"kind": "bogus"}]},
                   headers=H("planner")).status_code == 422


# ---------------------------------------------------------- proposals / inbox rule
def test_proposals_from_scenario_show_in_main_inbox(cl, mk, ship):
    sid = mk()
    r = cl.post("/actions/delay_shipment/propose", headers=H("planner", sid),
                json={"params": {"shipment": ship, "delay_hours": 5}, "rationale": "tested"})
    assert r.status_code == 201
    pid = r.json()["proposal_id"]
    in_scn = {p["id"]: p for p in cl.get("/proposals?state=all", headers=H("planner", sid)).json()}
    assert pid in in_scn and in_scn[pid]["scenario_id"] == sid
    assert in_scn[pid]["dry_run"]["impact"]["delta"]["kpis"]["shipments_delayed"] == 1
    main = {p["id"]: p for p in cl.get("/proposals?state=all", headers=H("admin")).json()}
    assert pid in main and main[pid]["scenario_id"] == sid                 # evidence scenario is shown in the main inbox
    # a plain main proposal does not appear under the scenario header
    r2 = cl.post("/actions/delay_shipment/propose", headers=H("planner"),
                 json={"params": {"shipment": ship, "delay_hours": 1}})
    assert r2.json()["proposal_id"] not in {p["id"] for p in cl.get("/proposals?state=all", headers=H("planner", sid)).json()}
    # approving is a main-only operation
    assert cl.post(f"/proposals/{pid}/approve", json={}, headers=H("admin", sid)).status_code == 400
    ok = cl.post(f"/proposals/{pid}/approve", json={}, headers=H("admin"))
    assert ok.status_code == 200
    assert cl.get(f"/objects/Shipment/{ship}", headers=H("planner", sid)).json()["status"] == "in_transit"   # fork snapshot
    assert cl.get(f"/objects/Shipment/{ship}", headers=H("planner")).json()["status"] == "delayed"


# ------------------------------------------------------------ startup / reset
def test_startup_registers_version_and_reset_discards_scenarios(sch, monkeypatch):
    monkeypatch.setenv("AMARH_ALLOW_RESET", "1")
    app = create_app(ONTOLOGY, schema=sch, pipeline=False)
    with TestClient(app) as c:
        cx = connect(schema=sch)
        assert cx.execute("SELECT count(*) FROM ontology_version").fetchone()[0] >= 1
        assert c.get("/ontology/versions", headers=H("admin")).status_code == 200
        assert c.get("/ontology/versions", headers=H("planner")).status_code == 403
        sid = c.post("/scenarios", json={"name": "r"}, headers=H("planner")).json()["id"]
        c.post("/actions/close_port/execute", json={"params": {"port": port()}}, headers=H("planner", sid))
        inf0 = cx.execute("SELECT count(*) FROM inferred_support WHERE scenario_id = %s", [sid]).fetchone()[0]
        assert inf0 == 0
        cx.commit()
        audit0 = cx.execute("SELECT count(*) FROM audit_event").fetchone()[0]
        cx.commit()
        r = c.post("/sim/control", json={"reset": True}, headers=H("admin"))
        assert r.status_code == 200, r.text
        assert c.get(f"/scenarios/{sid}", headers=H("planner")).json()["status"] == "discarded"
        assert cx.execute("SELECT count(*) FROM obj_port WHERE scenario_id = %s", [sid]).fetchone()[0] == 0
        assert cx.execute("SELECT count(*) FROM scenario_override WHERE scenario_id = %s", [sid]).fetchone()[0] == 0
        assert cx.execute("SELECT count(*) FROM audit_event").fetchone()[0] > audit0                  # audit kept
        cx.close()
