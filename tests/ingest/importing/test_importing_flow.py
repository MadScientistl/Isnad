"""Full flow with the bikeshare dataset: upload -> session -> baseline -> edit -> approve -> import."""
import json

import pytest

from tests.ingest.importing.conftest import BIKE, bike_zip, csv_rows
from engine.db import connect
from engine.ingest import workspaces as W


def test_full_flow_zip(h):
    r = h.upload(bike_zip(), "bikeshare.zip")
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    assert r.json()["profile_summary"]["tables"] == 7

    s = h.client.get(f"/ingest/sessions/{sid}").json()
    slug = s["slug"]
    pid = h.baseline(sid)
    det = h.client.get(f"/ingest/proposals/{pid}").json()
    assert det["validation"]["ok"], det["validation"]
    assert det["diff"]["sensitive"], "first-version roles are sensitive"

    # a comment is required for an import
    assert h.client.post(f"/ingest/proposals/{pid}/approve", json={"comment": ""}).json()["code"] == "comment_required"

    # edit: make a property optional-free rename of rationale (a harmless item edit) and reject nothing
    e = h.client.patch(f"/ingest/proposals/{pid}/items",
                       json={"items": [{"path": "/rationale", "action": "edit", "value": "reviewed by admin"}]})
    assert e.status_code == 201, e.text
    pid2 = e.json()["id"]
    assert e.json()["kind"] == "edit" and e.json()["parent_id"] == pid
    assert e.json()["validation"]["ok"]
    assert e.json()["proposal"]["rationale"] == "reviewed by admin"

    a = h.approve(pid2)
    assert a.status_code == 201, a.text
    did = a.json()["decision_id"]
    assert h.approve(pid2).json()["code"] == "already_decided"

    j = h.client.post(f"/ingest/decisions/{did}/import")
    assert j.status_code == 202
    job = h.wait(j.json()["job_id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["slug"] == slug and not res["already_imported"]

    # row counts match the CSVs exactly (minus counted drops: none expected in clean data)
    c = connect(schema=res["schema"])
    try:
        for t, name in (("Bikes", "bikes"), ("Stations", "stations"), ("Trips", "trips"), ("Riders", "riders")):
            n = c.execute(f'SELECT count(*) FROM obj_{t.lower()}').fetchone()[0]
            assert n == res["counts"]["objects"][t] == csv_rows(name), t
        assert sum(res["counts"]["objects"].values()) == res["rows"]
        for lname, n in res["counts"]["links"].items():
            assert c.execute(f"SELECT count(*) FROM link_{lname}").fetchone()[0] == n
        a_rows = c.execute("SELECT name, source, actor, role, params FROM audit_event WHERE name='import_dataset'").fetchall()
        assert len(a_rows) == 1
        name, source, actor, role, params = a_rows[0]
        assert (source, role, actor) == ("import", "owner", h.user)
        assert params["decision_id"] == did and params["counts"] == res["counts"]
        assert c.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 1
        # one shared action id for all rows
        t_ids = c.execute("SELECT DISTINCT action_id FROM obj_trips").fetchall()
        assert len(t_ids) == 1 and str(t_ids[0][0]) == res["action_id"]
        v = c.execute("SELECT source FROM ontology_version").fetchall()
        assert v == [("import",)]
    finally:
        c.close()
    hc = h.conn()
    try:
        assert hc.execute("SELECT count(*) FROM audit_event WHERE name='import_dataset' AND params->>'decision_id'=%s",
                          (did,)).fetchone()[0] == 1
    finally:
        hc.close()

    # files
    d = h.ws / slug
    for f in ("ontology.yaml", "mapping.json", "import_report.json", "build/schema.sql", "build/shapes.ttl"):
        assert (d / f).is_file(), f
    assert json.loads((d / "import_report.json").read_text())["counts"] == res["counts"]
    assert [w["slug"] for w in h.client.get("/workspaces").json()] == [slug]

    # idempotent second import: no new rows, same counts, flagged
    j2 = h.client.post(f"/ingest/decisions/{did}/import")
    job2 = h.wait(j2.json()["job_id"])
    assert job2["status"] == "done" and job2["result"]["already_imported"] is True
    assert job2["result"]["counts"] == res["counts"]
    c = connect(schema=res["schema"])
    try:
        assert c.execute("SELECT count(*) FROM audit_event WHERE name='import_dataset'").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM obj_trips").fetchone()[0] == csv_rows("trips")
    finally:
        c.close()


def test_single_csv_flow(h):
    data = (BIKE / "zones.csv").read_bytes()
    sid = h.upload(data, "zones.csv").json()["session_id"]
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["rows"] == csv_rows("zones")
