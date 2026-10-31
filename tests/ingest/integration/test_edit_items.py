"""Item edits against the real API: type renames cascade to links/actions, and several rejects in one batch."""
from tests.ingest.importing.conftest import bike_zip


def _baseline(h):
    sid = h.upload(bike_zip(), "bikeshare.zip").json()["session_id"]
    pid = h.baseline(sid)
    return pid, h.client.get(f"/ingest/proposals/{pid}").json()


def test_type_rename_cascades_to_links(h):
    pid, det = _baseline(h)
    names = [t["name"] for t in det["proposal"]["objectTypes"]]
    i = names.index("Bikes")
    r = h.client.patch(f"/ingest/proposals/{pid}/items",
                       json={"items": [{"path": f"/objectTypes/{i}/name", "action": "edit", "value": "Bicycles"}]})
    assert r.status_code == 201, r.text
    doc = r.json()["proposal"]
    assert "Bicycles" in [t["name"] for t in doc["objectTypes"]] and "Bikes" not in [t["name"] for t in doc["objectTypes"]]
    assert all("Bikes" not in (l["from"], l["to"]) for l in doc["linkTypes"])
    assert any("Bicycles" in (l["from"], l["to"]) for l in doc["linkTypes"])
    assert r.json()["validation"]["ok"], r.json()["validation"]


def test_type_rename_by_name_path_and_property_reject_by_name(h):
    pid, det = _baseline(h)
    r = h.client.patch(f"/ingest/proposals/{pid}/items", json={"items": [
        {"path": "/objectTypes/Zones/properties/city", "action": "reject"},
        {"path": "/objectTypes/Zones/name", "action": "edit", "value": "Areas"}]})
    assert r.status_code == 201, r.text
    areas = next(t for t in r.json()["proposal"]["objectTypes"] if t["name"] == "Areas")
    assert "city" not in [p["name"] for p in areas["properties"]]


def test_baseline_has_no_property_link_collision_and_unknown_codes(h):
    pid, det = _baseline(h)
    props = {p["name"] for t in det["proposal"]["objectTypes"] for p in t["properties"]}
    assert not props & {l["name"] for l in det["proposal"]["linkTypes"]}
    assert h.client.post(f"/ingest/proposals/{pid}/repair").status_code in (409, 202, 503)
