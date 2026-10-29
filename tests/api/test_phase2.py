"""Phase 2 API: actions routing, bbox, geo, stats, events, graph, sim (with doubles)."""
import sys
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from engine.actions import apply_action
from engine.api import main as api_main
from engine.api.main import create_app
from engine.api.seed import _id
from engine.db import connect

from .conftest import ONTOLOGY

H = {"x-role": "planner"}


def T(m, d, h=0):
    return datetime(2026, m, d, h, tzinfo=UTC)


def audit(schema, **where):
    c = connect(schema=schema)
    try:
        q = "SELECT name, actor, source, params, sim_time FROM audit_event"
        if where:
            q += " WHERE " + " AND ".join(f"{k} = %s" for k in where)
        return c.execute(q + " ORDER BY seq", list(where.values())).fetchall()
    finally:
        c.close()


# --------------------------------------------------------------- mutations / audit
def test_mutations_write_audit_rows(client, schema):
    cid = client.post("/objects/Carrier", json={"name": "AuditCo"}, headers=H).json()["id"]
    client.put(f"/objects/Carrier/{cid}", json={"name": "AuditCo2"}, headers=H)
    client.delete(f"/objects/Carrier/{cid}", headers=H)
    rows = [r for r in audit(schema, source="api") if r[3].get("id") == cid]
    assert [r[0] for r in rows] == ["create_object", "update_object", "delete_object"]
    assert {r[1] for r in rows} == {"planner"}


def test_link_actions_audited_and_abstract_facility(client, schema):
    ship = client.post("/objects/Shipment", json={"ref": "SHP-AUD", "status": "planned"}, headers=H).json()["id"]
    port = str(_id("Port", "Rotterdam"))
    wh = str(_id("Warehouse", "Venlo DC"))
    # ships_from targets the abstract Facility: both a Port and a Warehouse must work
    assert client.post("/links/ships_from", json={"src": ship, "dst": port}, headers=H).status_code == 201
    assert client.post("/links/ships_to", json={"src": ship, "dst": wh}, headers=H).status_code == 201
    assert client.delete(f"/links/ships_from?src={ship}&dst={port}", headers=H).status_code == 200
    names = [r[0] for r in audit(schema, source="api") if r[3].get("src") == ship]
    assert names == ["create_link", "create_link", "end_link"]


def test_error_mapping(client):
    # missing endpoint -> ValueError -> 422; connection still usable afterwards
    ghost = str(uuid.uuid4())
    assert client.post("/links/ships_to", json={"src": ghost, "dst": ghost}, headers=H).status_code == 422
    assert client.post("/objects/Port", json={"name": "x", "valid_from": "garbage"}, headers=H).status_code == 422
    assert client.get("/objects/Port?order_by=nope", headers=H).status_code == 422
    assert client.put(f"/objects/Port/{ghost}", json={"name": "x"}, headers=H).status_code == 404
    pid = client.post("/objects/Port", json={"name": "Dup"}, headers=H).json()["id"]
    assert client.post("/objects/Port", json={"id": pid, "name": "Dup"}, headers=H).status_code == 409


def test_pagination_in_sql_and_order(client):
    a = client.get("/objects/Port?limit=3&order_by=name", headers=H).json()
    b = client.get("/objects/Port?limit=3&offset=3&order_by=name", headers=H).json()
    assert len(a) == len(b) == 3
    assert a[-1]["name"] < b[0]["name"]
    d = client.get("/objects/Port?limit=1&order_by=name&desc=true", headers=H).json()
    assert d[0]["name"] >= b[-1]["name"]


def test_bbox_on_list(client):
    r = client.get("/objects/Port?bbox=-10,40,20,60", headers=H).json()
    assert {"Rotterdam", "Hamburg", "Antwerp"} <= {p["name"] for p in r}
    assert all(-10 <= p["location"]["coordinates"][0] <= 20 for p in r)
    assert client.get("/objects/Supplier?bbox=0,0,1,1", headers=H).status_code == 422
    assert client.get("/objects/Port?bbox=1,2,3", headers=H).status_code == 422


def test_seed_shape(client):
    ports = client.get("/objects/Port?limit=100", headers=H).json()
    assert len([p for p in ports if p["location"]]) >= 12
    assert len(client.get("/objects/Warehouse", headers=H).json()) >= 6
    assert len(client.get("/objects/Carrier", headers=H).json()) >= 6


# --------------------------------------------------------------- sim world fixture
@pytest.fixture(scope="module")
def world(make_schema):
    """Isolated schema with a scripted history in March 2026 (sim time)."""
    name = make_schema()
    c = connect(schema=name)
    port, wh = _id("Port", "Rotterdam"), _id("Warehouse", "Venlo DC")
    sup = _id("Supplier", 0)
    shp, shp2 = uuid.uuid4(), uuid.uuid4()

    def act(n, p, at):
        return apply_action(c, n, p, "sim", valid_at=at, source="sim", run_id="t", ontology=ONTOLOGY)

    act("create_object", {"type": "Shipment", "id": shp, "props": {
        "ref": "SHP-T1", "status": "planned", "cargo_value": 1000.0, "eta": T(3, 20),
        "position": {"type": "Point", "coordinates": [4.14, 51.95]}}}, T(3, 1))
    act("create_link", {"link": "ships_via", "src": shp, "dst": port}, T(3, 1))
    act("create_link", {"link": "ships_from", "src": shp, "dst": port}, T(3, 1))
    act("create_link", {"link": "ships_to", "src": shp, "dst": wh}, T(3, 1))
    act("ship_depart", {"shipment": shp}, T(3, 2))
    act("close_port", {"port": port, "reason": "storm"}, T(3, 3))
    act("delay_shipment", {"shipment": shp, "delay_hours": 18.0}, T(3, 4))
    act("issue_invoice", {"shipment": shp, "supplier": sup, "amount": 500.0}, T(3, 4, 12))
    inv = c.execute("SELECT id FROM obj_invoice").fetchone()[0]
    act("flag_invoice", {"invoice": inv, "anomaly": "duplicate"}, T(3, 5))
    act("reopen_port", {"port": port}, T(3, 6))
    act("ship_position", {"shipment": shp, "position": {"type": "Point", "coordinates": [30.0, 40.0]},
                          "progress": 0.5}, T(3, 7))
    act("ship_arrive", {"shipment": shp}, T(3, 8))
    c.close()
    return {"schema": name, "client": TestClient(create_app(ONTOLOGY, schema=name, pipeline=False)),
            "ship": str(shp), "port": str(port), "wh": str(wh)}


# --------------------------------------------------------------- stats
def test_summary_shape_and_as_of(world):
    c = world["client"]
    s = c.get("/stats/summary", params={"as_of": T(3, 3, 12).isoformat()}, headers=H).json()
    assert set(s) == {"as_of", "shipments", "ports", "invoices", "value_at_risk", "delay_hours_total",
                      "open_events"}
    assert set(s["shipments"]) == {"planned", "in_transit", "delayed", "arrived"}
    assert set(s["ports"]) == {"open", "closed"} and set(s["invoices"]) == {"open", "approved", "paid", "flagged"}
    assert s["shipments"]["in_transit"] == 1 and s["ports"]["closed"] == 1
    assert s["value_at_risk"] == 1000.0 and s["open_events"] == 1 and s["delay_hours_total"] == 0
    s = c.get("/stats/summary", params={"as_of": T(3, 5, 6).isoformat()}, headers=H).json()
    assert s["shipments"]["delayed"] == 1 and s["delay_hours_total"] == 18.0
    assert s["invoices"]["flagged"] == 1 and s["invoices"]["open"] == 0
    s = c.get("/stats/summary", params={"as_of": T(3, 7).isoformat()}, headers=H).json()
    assert s["ports"]["closed"] == 0 and s["open_events"] == 0 and s["value_at_risk"] == 0  # position update resumed the shipment
    s = c.get("/stats/summary?as_of=2026-03-09T00:00:00Z", headers=H).json()
    assert s["shipments"]["arrived"] == 1 and s["value_at_risk"] == 0
    live = c.get("/stats/summary?as_of=live", headers=H).json()
    assert live["as_of"] == "live" and live["shipments"]["arrived"] == 1
    assert c.get("/stats/summary?as_of=nonsense", headers=H).status_code == 422


def test_series(world):
    c = world["client"]

    def series(metric, **kw):
        r = c.get("/stats/series", params={"metric": metric, "from": T(3, 1).isoformat(),
                                           "to": T(3, 9).isoformat(), "step_hours": 24, **kw}, headers=H)
        assert r.status_code == 200, r.text
        j = r.json()
        assert set(j) == {"metric", "points"} and set(j["points"][0]) == {"t", "v"}
        return [p["v"] for p in j["points"]]
    assert series("ports_closed") == [0, 0, 1, 1, 1, 0, 0, 0, 0]
    assert series("shipments_in_transit") == [0, 1, 1, 0, 0, 0, 1, 0, 0]
    assert series("shipments_delayed") == [0, 0, 0, 1, 1, 1, 0, 0, 0]
    assert series("invoices_flagged") == [0, 0, 0, 0, 1, 1, 1, 1, 1]
    assert series("value_at_risk") == [0, 0, 1000, 1000, 1000, 1000, 0, 0, 0]
    t0 = time.perf_counter()
    r = c.get("/stats/series", params={"metric": "value_at_risk", "from": T(2, 1).isoformat(),
                                       "to": T(3, 3).isoformat(), "step_hours": 24}, headers=H)
    assert r.status_code == 200 and len(r.json()["points"]) == 31
    assert time.perf_counter() - t0 < 1.0
    assert c.get("/stats/series?metric=bogus", headers=H).status_code == 422
    assert c.get("/stats/series?metric=ports_closed&from=2026-01-01&to=2030-01-01&step_hours=0.01",
                 headers=H).status_code == 422


# --------------------------------------------------------------- events
def test_events_feed(world):
    c = world["client"]
    ev = c.get("/events?limit=100", headers=H).json()
    assert ev[0]["name"] == "ship_arrive" and ev[-1]["name"] == "create_object"
    assert set(ev[0]) == {"seq", "action_id", "name", "actor", "source", "sim_time", "recorded_at",
                          "params", "summary"}
    assert [e["seq"] for e in ev] == sorted((e["seq"] for e in ev), reverse=True)
    summ = {e["name"]: e["summary"] for e in ev}
    assert summ["close_port"] == "Port Rotterdam closed"
    assert summ["reopen_port"] == "Port Rotterdam reopened"
    assert summ["delay_shipment"] == "Shipment SHP-T1 delayed 18 h"
    assert summ["flag_invoice"].endswith("flagged: duplicate")
    window = c.get("/events", params={"from": T(3, 3).isoformat(), "to": T(3, 4).isoformat()}, headers=H).json()
    assert [e["name"] for e in window] == ["delay_shipment", "close_port"]
    assert len(c.get("/events?limit=2", headers=H).json()) == 2


# --------------------------------------------------------------- geo
def test_geo_collections(world):
    c = world["client"]
    fc = c.get("/geo/Port", headers=H).json()
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 12
    f = next(x for x in fc["features"] if x["properties"]["name"] == "Rotterdam")
    assert f["id"] == f["properties"]["id"] == world["port"] and f["geometry"]["type"] == "Point"
    assert f["properties"]["type"] == "Port" and "location" not in f["properties"]
    small = c.get("/geo/Port?bbox=-10,40,20,60", headers=H).json()["features"]
    assert {"Rotterdam", "Hamburg", "Antwerp"} <= {x["properties"]["name"] for x in small} < \
        {x["properties"]["name"] for x in fc["features"]}
    sh = c.get("/geo/Shipment", headers=H).json()["features"]
    assert len(sh) == 1
    p = sh[0]["properties"]
    assert p["origin"] == [4.14, 51.95] and p["destination"] == [6.17, 51.37]
    assert sh[0]["geometry"]["coordinates"] == [30.0, 40.0]
    old = c.get("/geo/Shipment", params={"as_of": T(3, 2).isoformat()}, headers=H).json()["features"]
    assert old[0]["geometry"]["coordinates"] == [4.14, 51.95] and old[0]["properties"]["status"] == "in_transit"
    assert c.get("/geo/Shipment", params={"as_of": T(2, 1).isoformat()}, headers=H).json()["features"] == []
    assert c.get("/geo/Supplier", headers=H).status_code == 404


# --------------------------------------------------------------- graph
class FakePipeline:
    def __init__(self, edges=()):
        self.edges, self.syncs, self.reasons, self.asked = list(edges), 0, 0, []

    def sync(self):
        self.syncs += 1
        return 0

    def maybe_reason(self, force=False):
        self.reasons += 1
        return False

    def rebuild(self):
        pass

    def inferred_edges(self, ids):
        self.asked.append(list(ids))
        return self.edges


def test_graph_depth_and_inferred(schema):
    s = [str(_id("Supplier", i)) for i in range(5)]
    fp = FakePipeline([(s[0], "supplies", s[1]),   # also asserted -> returned once, as asserted
                       (s[0], "supplies", s[2]),   # new inferred edge between returned nodes
                       (s[0], "supplies", s[4])])  # endpoint not in the neighbourhood -> dropped
    c = TestClient(create_app(ONTOLOGY, schema=schema, pipeline=fp))
    g = c.get(f"/graph?center={s[1]}&depth=1", headers=H).json()
    assert set(g) == {"center", "as_of", "inferred_available", "truncated", "nodes", "edges"}
    assert g["center"] == s[1] and g["as_of"] == "live" and g["inferred_available"] is True
    assert g["truncated"] is False
    ids = {n["id"] for n in g["nodes"]}
    assert {s[0], s[1], s[2]} <= ids and s[3] not in ids and s[4] not in ids
    assert set(g["nodes"][0]) == {"id", "type", "label", "status"}
    assert g["nodes"][0]["id"] == s[1] and g["nodes"][0]["label"] == "Supplier 01"
    e = {(x["src"], x["dst"], x["link"]): x["inferred"] for x in g["edges"]}
    assert e[(s[0], s[1], "supplies")] is False and e[(s[1], s[2], "supplies")] is False
    assert e[(s[0], s[2], "supplies")] is True
    assert (s[0], s[4], "supplies") not in e
    assert len(g["edges"]) == len(e)
    # depth 2 reaches S3
    g2 = c.get(f"/graph?center={s[1]}&depth=2", headers=H).json()
    assert s[3] in {n["id"] for n in g2["nodes"]}
    # include_inferred=false
    g3 = c.get(f"/graph?center={s[1]}&depth=1&include_inferred=false", headers=H).json()
    assert not any(x["inferred"] for x in g3["edges"]) and g3["inferred_available"] is True
    # as_of => asserted only, inferred unavailable
    g4 = c.get(f"/graph?center={s[1]}&depth=1&as_of=2026-02-01T00:00:00Z", headers=H).json()
    assert g4["inferred_available"] is False and not any(x["inferred"] for x in g4["edges"])
    assert g4["as_of"].startswith("2026-02-01T00:00:00")
    # before the epoch nothing exists
    assert c.get(f"/graph?center={s[1]}&as_of=2025-01-01T00:00:00Z", headers=H).status_code == 404
    assert c.get(f"/graph?center={uuid.uuid4()}", headers=H).status_code == 404
    assert c.get(f"/graph?center={s[1]}&depth=4", headers=H).status_code == 422


def test_graph_truncation_and_no_pipeline(client, monkeypatch):
    s1 = str(_id("Supplier", 1))
    monkeypatch.setattr(api_main, "MAX_GRAPH_NODES", 2)
    g = client.get(f"/graph?center={s1}&depth=3", headers=H).json()
    assert g["truncated"] is True and len(g["nodes"]) == 2
    assert g["inferred_available"] is False
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["src"] in ids and e["dst"] in ids for e in g["edges"])


def test_graph_all_link_types(world):
    g = world["client"].get(f"/graph?center={world['ship']}&depth=1", headers=H).json()
    links = {e["link"] for e in g["edges"]}
    assert {"ships_via", "ships_from", "ships_to", "invoices_for"} <= links
    assert {n["type"] for n in g["nodes"]} >= {"Shipment", "Port", "Warehouse", "Invoice"}
    assert next(n for n in g["nodes"] if n["id"] == world["ship"])["status"] == "arrived"


def test_writes_trigger_sync(schema):
    fp = FakePipeline()
    c = TestClient(create_app(ONTOLOGY, schema=schema, pipeline=fp))
    assert c.post("/objects/Carrier", json={"name": "SyncCo"}, headers=H).status_code == 201
    assert fp.syncs == 1
    for _ in range(50):
        if fp.reasons:
            break
        time.sleep(0.02)
    assert fp.reasons >= 1


# --------------------------------------------------------------- sim
class FakeRunner:
    def __init__(self):
        self.speed, self.ticks, self.calls = "paused", 0, []

    def state(self):
        return {"sim_now": datetime(2026, 1, 1, self.ticks, tzinfo=UTC), "epoch": datetime(2026, 1, 1, tzinfo=UTC),
                "speed": self.speed, "running": self.speed != "paused", "seed": 1, "tick": self.ticks,
                "achieved_hours_per_s": None, "last_error": None, "extra": 1}

    def set_speed(self, s):
        self.calls.append(("speed", s))
        self.speed = s

    def step(self):
        self.calls.append(("step",))
        self.ticks += 1

    def reset(self):
        self.calls.append(("reset",))
        self.ticks, self.speed = 0, "paused"

    def inject(self, kind, target=None, at=None, duration_h=24.0):
        self.calls.append(("inject", kind, target, at, duration_h))
        if kind == "port_closure" and target is None:
            raise ValueError("port_closure needs a target")
        return {"kind": kind, "target": target, "at": at or datetime(2026, 1, 1, tzinfo=UTC)}

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        pass


def test_sim_endpoints_with_double(schema, make_schema, monkeypatch):
    monkeypatch.delenv("AMARH_ALLOW_RESET", raising=False)
    r = FakeRunner()
    c = TestClient(create_app(ONTOLOGY, schema=schema, pipeline=False, runner=r))
    s = c.get("/sim/state", headers=H).json()
    assert set(s) == {"sim_now", "epoch", "speed", "running", "seed", "tick", "achieved_hours_per_s", "last_error"}
    assert s["speed"] == "paused" and s["running"] is False and s["sim_now"].startswith("2026-01-01T00:00:00")
    s = c.post("/sim/control", json={"speed": "6"}, headers=H).json()
    assert s["speed"] == "6" and s["running"] is True and ("start",) in r.calls  # a speed starts the thread
    assert c.post("/sim/control", json={"speed": 24}, headers=H).json()["speed"] == "24"
    assert c.post("/sim/control", json={"speed": "99"}, headers=H).status_code == 422
    assert c.post("/sim/control", json={"step": True}, headers=H).json()["tick"] == 1
    assert c.post("/sim/control", json={"reset": True}, headers=H).status_code == 403  # opt-in only
    assert ("reset",) not in r.calls and ("step",) in r.calls
    monkeypatch.setenv("AMARH_ALLOW_RESET", "1")
    scratch = make_schema()  # reset truncates its schema: never the shared fixture
    r2 = FakeRunner()
    c2 = TestClient(create_app(ONTOLOGY, schema=scratch, pipeline=False, runner=r2))
    port = str(_id("Port", "Dubai"))
    assert c2.post("/objects/Carrier", json={"name": "Temp"}, headers=H).status_code == 201
    assert c2.post("/sim/control", json={"reset": True}, headers=H).json()["tick"] == 0
    assert ("reset",) in r2.calls
    assert {o["name"] for o in c2.get("/objects/Carrier", headers=H).json()} .isdisjoint({"Temp"})  # data wiped, re-seeded
    assert c2.get("/events", headers=H).json() == []  # audit emptied too
    port = str(_id("Port", "Dubai"))
    j = c.post("/sim/inject", json={"kind": "port_closure", "target": port, "duration_h": 12}, headers=H).json()
    assert j == {"injected": {"kind": "port_closure", "target": port, "at": "2026-01-01T00:00:00Z"}} or \
        j["injected"]["target"] == port
    assert set(j["injected"]) == {"kind", "target", "at"}
    assert r.calls[-1][1:3] == ("port_closure", uuid.UUID(port)) and r.calls[-1][4] == 12
    assert c.post("/sim/inject", json={"kind": "port_closure"}, headers=H).status_code == 422
    assert c.post("/sim/inject", json={"kind": "meteor"}, headers=H).status_code == 422


def test_sim_unavailable_503(schema, monkeypatch):
    monkeypatch.setitem(sys.modules, "sim.runner", None)  # forces ImportError
    c = TestClient(create_app(ONTOLOGY, schema=schema, pipeline=False))
    for call in (lambda: c.get("/sim/state"), lambda: c.post("/sim/control", json={"step": True}),
                 lambda: c.post("/sim/inject", json={"kind": "shipment_delay"})):
        r = call()
        assert r.status_code == 503 and r.json() == {"detail": "simulation unavailable"}


def test_health(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["db"] is True


def test_graph_real_pipeline_syncs_existing_data_on_first_use(make_schema):
    """A freshly started API over data it never wrote must still return inferred edges."""
    from engine.api.seed import _id
    name = make_schema()
    app = create_app(ONTOLOGY, schema=name)  # real, lazily created SyncPipeline
    c = TestClient(app)
    s0 = str(_id("Supplier", 0))  # seeded chain S0 -> S1 -> S2 -> ...
    g = c.get("/graph", params={"center": s0, "depth": 2}).json()
    assert g["inferred_available"] is True
    inf = [e for e in g["edges"] if e["inferred"]]
    assert inf, "transitive supplies closure should appear as inferred edges"
    assert all(e["link"] in ("supplies", "supplied_by") for e in inf)
