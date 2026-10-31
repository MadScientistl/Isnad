"""An imported workspace must be servable by its own API process: graph store built on first start (the importer writes no
outbox rows), no seeding, no reset, generic actions audited."""
import pytest
from fastapi.testclient import TestClient

from engine.api.main import create_app
from tests.ingest.importing.conftest import bike_zip

ADM = {"x-role": "admin"}


@pytest.fixture
def ws(h, monkeypatch, tmp_path):
    sid = h.upload(bike_zip(), "bikeshare.zip").json()["session_id"]
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done", job
    res = job["result"]
    monkeypatch.setenv("ISNAD_BUILD_DIR", str(tmp_path / "build"))
    monkeypatch.setenv("ISNAD_ALLOW_IMPORT", "0")
    monkeypatch.setenv("ISNAD_ALLOW_RESET", "0")
    monkeypatch.setenv("ISNAD_OUTBOX_WORKER", "0")
    monkeypatch.setenv("ISNAD_SIM_AUTOSTART", "0")
    app = create_app(str(h.ws / res["slug"] / "ontology.yaml"), schema=res["schema"])
    with TestClient(app) as c:
        yield c, res


def test_workspace_process_serves_imported_data(ws):
    c, res = ws
    types = c.get("/types", headers=ADM).json()
    names = {t["name"] for t in types["objectTypes"]}
    assert {"Stations", "Bikes", "Trips"} <= names
    st = next(t for t in types["objectTypes"] if t["name"] == "Stations")
    assert st["geo"] is True

    n = res["counts"]["objects"]["Stations"]
    objs = c.get("/objects/Stations", headers=ADM, params={"limit": 1000})
    assert objs.status_code == 200, objs.text
    rows = objs.json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    assert len(rows) == n

    geo = c.get("/geo/Stations", headers=ADM).json()
    assert len(geo["features"]) == n and geo["features"][0]["geometry"]["type"] == "Point"

    summ = c.get("/stats/summary", headers=ADM).json()
    assert summ["counts"]["Stations"] == n

    # the graph store was built from the views although no outbox row exists
    ask = c.post("/sparql", headers=ADM, json={"query": "ASK { ?s a ?t }"})
    assert ask.status_code == 200, ask.text
    assert ask.json().get("boolean", ask.json()) in (True, {"boolean": True}) or ask.json()["boolean"] is True
    g = c.get("/graph", headers=ADM, params={"center": rows[0]["id"]})
    assert g.status_code == 200, g.text
    assert len(g.json()["nodes"]) >= 2 and g.json()["edges"]


def test_workspace_never_resets_or_seeds_and_audits_actions(ws):
    c, res = ws
    assert c.get("/sim/state", headers=ADM).status_code == 404
    assert c.post("/sim/control", headers=ADM, json={"reset": True}).status_code in (403, 404)
    # no supply-chain data leaked in
    assert c.get("/objects/Ports", headers=ADM).status_code == 404

    acts = {a["name"]: a for a in c.get("/actions", headers=ADM).json()}
    assert "create_object" in acts
    name = "create_object"
    r = c.post(f"/actions/{name}/execute", headers=ADM,
               json={"params": {"type": "Stations", "props": {"station_id": "ST999", "name": "Test station"}}})
    assert r.status_code in (200, 201), r.text
    aid = r.json()["action_id"] if "action_id" in r.json() else r.json()["execution"]["action_id"]
    feed = c.get("/audit", headers=ADM, params={"kind": "execution"}).json()
    names = {x["name"] for x in feed}
    assert name in names and "import_dataset" in names
    assert any(x["id"] == aid for x in feed)

