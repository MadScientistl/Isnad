"""API behaviour: access control, upload hardening, limits, named sources, edits, model jobs."""
import io
import json
import sqlite3
import time
import zipfile

import pytest

from agent.types import ModelTurn, ToolUse
from engine.ingest.profile import baseline_proposal
from engine.ingest.sources import NamedSource
from tests.ingest.importing.conftest import BIKE, bike_zip, csv_rows

ZONES = (BIKE / "zones.csv").read_bytes()


def test_non_admin_is_404_and_disabled_gate(h, monkeypatch):
    for method, path in (("get", "/ingest/sessions"), ("get", "/ingest/sources"), ("get", "/workspaces"),
                         ("post", "/ingest/uploads?filename=a.csv"), ("get", "/ingest/jobs/x")):
        r = getattr(h.client, method)(path, headers={"x-role": "planner"})
        assert r.status_code == 404 and r.json()["code"] == "not_found", path
    assert h.client.get("/ingest/sessions").status_code == 200
    monkeypatch.delenv("ISNAD_ALLOW_IMPORT")
    r = h.client.get("/ingest/sessions")
    assert r.status_code == 404 and r.json()["code"] == "import_disabled"
    monkeypatch.setenv("ISNAD_AUTH", "1")                    # M3: the allow flag is needed in every mode
    r = h.client.get("/ingest/sessions")
    assert r.status_code == 404 and r.json()["code"] == "import_disabled"


def test_upload_size_limit_413(h, monkeypatch):
    monkeypatch.setenv("ISNAD_MAX_UPLOAD_BYTES", "2000")
    r = h.upload(b"a,b\n" + b"1,2\n" * 2000, "big.csv")          # content-length known
    assert r.status_code == 413 and r.json()["code"] == "too_large"

    def chunks():                                               # chunked: no content-length, limit enforced while reading
        yield b"a,b\n"
        for _ in range(100):
            yield b"1,2\n" * 100
    r = h.client.post("/ingest/uploads", params={"filename": "big.csv"}, content=chunks())
    assert r.status_code == 413
    assert h.sessions() == []
    tmpd = h.ingest / "_tmp"
    assert not tmpd.exists() or not list(tmpd.iterdir())        # temp file removed


def test_hostile_filename_and_type_checks(h):
    name = "../../etc/pass‮wd\x00 <script>.csv"
    r = h.upload(b"id,name\n1,a\n2,b\n", name)
    assert r.status_code == 201, r.text
    ((sid, source_name),) = h.sessions()
    assert "/" not in source_name and "\x00" not in source_name and "‮" not in source_name and "<" not in source_name
    files = [p for p in h.ingest.rglob("*") if p.is_file()]
    assert len(files) == 1 and files[0].parent.name == str(sid) and len(files[0].name) == 64   # sha256, not the name
    assert not list(h.tmp.glob("etc*")) and not (h.tmp.parent / "etc").exists()
    # wrong extension / mismatching content
    assert h.upload(b"id,name\n1,a\n", "x.exe").status_code == 415
    assert h.upload(b"id,name\n1,a\n", "x.zip").status_code == 415
    assert h.upload(bike_zip(), "x.csv").status_code == 415
    assert h.upload(b"id,name\n1,a\n", "x.sqlite").status_code == 415
    assert h.upload(b"", "x.csv").status_code == 422


def test_zip_bomb_rejected_and_cleaned(h):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.csv", "a,b\n" + "0,0\n" * 3_000_000)    # ~12 MB of highly compressible rows
    r = h.upload(buf.getvalue(), "bomb.zip")
    assert r.status_code == 422 and r.json()["code"] == "unreadable_source"
    assert h.sessions() == []
    assert not [p for p in h.ingest.rglob("*") if p.is_file()]
    # path traversal member
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.csv", "a,b\n1,2\n")
    r = h.upload(buf.getvalue(), "trav.zip")
    assert r.status_code == 422 and r.json()["reason"] == "unsafe_archive"


def test_tamper_blocks_import(h):
    sid = h.upload(ZONES, "zones.csv").json()["session_id"]
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    (stored,) = [p for p in h.ingest.rglob("*") if p.is_file()]
    stored.write_bytes(stored.read_bytes() + b"zone_x,Injected\n")
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "source_tampered"
    assert not h.ws.exists() or not [d for d in h.ws.iterdir()]
    # the baseline also refuses to validate against tampered bytes
    r = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "baseline"})
    assert r.status_code == 409 and r.json()["code"] == "source_tampered"


def _import(h, data, name, **kw):
    sid = h.upload(data, name).json()["session_id"]
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    return sid, h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"]), did


def test_failure_mid_import_leaves_nothing(make_harness):
    import engine.ingest.materialize as M

    class Boom:
        calls = 0
        new_stats = staticmethod(M.new_stats)
        enrich_mapping = staticmethod(M.enrich_mapping)
        iter_links = staticmethod(M.iter_links)

        @staticmethod
        def iter_objects(src, mapping, t, **kw):
            Boom.calls += 1
            if Boom.calls == 2:
                raise RuntimeError("secret internals /etc/passwd")
            yield from M.iter_objects(src, mapping, t, **kw)

    h = make_harness(materialize_mod=Boom)
    sid, job, did = _import(h, bike_zip(), "bikeshare.zip")
    assert job["status"] == "failed" and job["error"]["code"] == "import_failed"
    assert "passwd" not in json.dumps(job)
    (schema,) = h.schemas()
    c = h.conn()
    try:
        assert c.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone() is None
        assert c.execute("SELECT count(*) FROM audit_event WHERE name='import_dataset' AND params->>'decision_id' = %s",
                         (did,)).fetchone()[0] == 0
    finally:
        c.close()
    assert not h.ws.exists() or not list(h.ws.iterdir())
    # a retry after fixing the cause works (same decision)
    Boom.calls = 100
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done", job


def test_validation_rerun_blocks_import(make_harness):
    from engine.ingest import validate as V
    state = {"n": 0}

    def flaky(profile, prop, src, *, namespace=None, slug=None):
        state["n"] += 1
        rep = V.validate(profile, prop, src, namespace=namespace, slug=slug)
        if state["n"] > 1:                                    # the re-validation at import time fails
            return {"ok": False, "checks": [{"name": "key_violations", "ok": False}]}
        return rep
    h = make_harness(validate_fn=flaky)
    sid, job, did = _import(h, ZONES, "zones.csv")
    assert job["status"] == "failed" and job["error"]["code"] == "validation_failed"
    assert job["error"]["failed"] == ["key_violations"]
    assert h.schemas() and not [s for s in h.schemas() if _exists(h, s)]


def _exists(h, schema):
    c = h.conn()
    try:
        return c.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone() is not None
    finally:
        c.close()


def test_dropped_links_are_counted(h):
    """Orphan foreign keys are dropped and counted, never silently."""
    zones = ("zone_id,name\n" + "".join(f"z{i},Zone {i}\n" for i in range(1, 21))).encode()
    stations = ("station_id,name,zone_id\n" + "".join(f"s{i},Station {i},z{i % 20 + 1}\n" for i in range(100))
                + "s999,Orphan,z99\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("zones.csv", zones)
        z.writestr("stations.csv", stations)
    sid = h.upload(buf.getvalue(), "mini.zip").json()["session_id"]
    pid = h.baseline(sid)
    det = h.client.get(f"/ingest/proposals/{pid}").json()
    assert det["validation"]["ok"], det["validation"]
    assert det["mapping"]["links"], "an fk link is expected"
    did = h.approve(pid).json()["decision_id"]
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["rows"] == 20 + 101
    assert sum(v["unresolved"] for v in res["dropped_links"].values()) == 1
    assert sum(res["counts"]["links"].values()) == 100


def test_named_sqlite_source_and_no_dsn_leak(make_harness, tmp_path):
    db = tmp_path / "secret_dir_xyz" / "bike.sqlite"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE zones (zone_id TEXT PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO zones VALUES (?,?)", [("z1", "North"), ("z2", "South"), ("z3", "East")])
    con.commit()
    con.close()
    reg = {"bike": NamedSource("bike", "sqlite", str(db))}
    h = make_harness(source_registry=reg)
    r = h.client.get("/ingest/sources")
    assert r.json() == [{"name": "bike", "kind": "sqlite"}]
    assert "secret_dir_xyz" not in r.text
    r = h.client.post("/ingest/sources/bike/profile")
    assert r.status_code == 201 and "secret_dir_xyz" not in r.text
    sid = r.json()["session_id"]
    s = h.client.get(f"/ingest/sessions/{sid}")
    assert s.json()["named"] is True and s.json()["source_name"] == "bike" and "secret_dir_xyz" not in s.text
    assert h.client.post("/ingest/sources/nope/profile").status_code == 404
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done" and job["result"]["rows"] == 3
    assert "secret_dir_xyz" not in json.dumps(job) and "secret_dir_xyz" not in h.client.get("/workspaces").text
    # the named source changing after profiling is refused
    sid2 = h.client.post("/ingest/sources/bike/profile").json()["session_id"]
    pid2 = h.baseline(sid2)
    did2 = h.approve(pid2).json()["decision_id"]
    con = sqlite3.connect(db)
    con.execute("INSERT INTO zones VALUES ('z4','West')")
    con.commit()
    con.close()
    job = h.wait(h.client.post(f"/ingest/decisions/{did2}/import").json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "source_changed"


# ------------------------------------------------------------------ edits
def test_edit_policy(h):
    sid = h.upload(ZONES, "zones.csv").json()["session_id"]
    pid = h.baseline(sid)
    base = h.client.get(f"/ingest/proposals/{pid}").json()["proposal"]

    def edit(items):
        return h.client.patch(f"/ingest/proposals/{pid}/items", json={"items": items})
    r = edit([{"path": "/roles/-", "action": "edit", "value": {"name": "ops", "description": "x", "access": "write"}}])
    assert r.status_code == 422 and r.json()["code"] == "role_not_allowed"
    r = edit([{"path": "/profile_hash", "action": "edit", "value": "0" * 64}])
    assert r.json()["code"] == "path_not_editable"
    r = edit([{"path": "/functions", "action": "edit", "value": {"f": "x"}}])
    assert r.json()["code"] == "path_not_editable"
    r = edit([{"path": "/objectTypes/Nope/label", "action": "edit", "value": "x"}])
    assert r.json()["code"] == "invalid_path"
    r = edit([{"path": "/objectTypes/0/bogus", "action": "edit", "value": 1}])
    assert r.status_code == 422 and r.json()["code"] == "invalid_proposal"
    r = edit([{"path": "/notes", "action": "edit", "value": ["a note"]}, {"path": "/rationale", "action": "accept"}])
    assert r.status_code == 201 and r.json()["proposal"]["notes"] == ["a note"]
    assert r.json()["validation"]["edit_items"][0]["path"] == "/notes"
    # an extra field in a body is rejected without echoing it
    r = h.client.patch(f"/ingest/proposals/{pid}/items", json={"items": [{"path": "/notes", "action": "edit", "evil_field_abc": 1}]})
    assert r.status_code == 422 and "evil_field_abc" not in r.text


def test_error_bodies_do_not_echo_input(h):
    r = h.client.get("/ingest/sessions/not-a-uuid-<script>")
    assert r.status_code == 404 and "script" not in r.text
    r = h.client.post("/ingest/sessions/00000000-0000-0000-0000-000000000000/proposals", json={"mode": "zzz<b>"})
    assert r.status_code == 422 and "<b>" not in r.text


# ------------------------------------------------------------------ limits and model jobs
class Scripted:
    """ModelClient that submits the heuristic baseline proposal on its first turn."""
    name = "scripted"

    def __init__(self, profile_getter):
        self.get, self.calls = profile_getter, 0
        self.delay = 0.0

    def complete(self, *, system, messages, tools, max_tokens, on_text=None, timeout=None):
        self.calls += 1
        time.sleep(self.delay)
        prof = self.get()
        prop = baseline_proposal(prof)
        tu = ToolUse("tu1", "submit_proposal", {"proposal": prop})
        return ModelTurn(content=[{"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}],
                         tool_uses=[tu], stop_reason="tool_use", usage={"input_tokens": 10, "output_tokens": 10})


def test_model_unavailable_503(make_harness):
    def nokey(_p):
        raise RuntimeError("no key")
    h = make_harness(model_factory=nokey)
    sid = h.upload(ZONES, "zones.csv").json()["session_id"]
    r = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r.status_code == 503 and r.json()["code"] == "agent_unavailable"
    pid = h.baseline(sid)
    assert h.client.post(f"/ingest/proposals/{pid}/repair").status_code == 503


def test_model_job_repair_and_limits(make_harness, monkeypatch):
    holder = {}
    model = Scripted(lambda: holder["profile"])
    h = make_harness(model_factory=lambda p: model)
    sid = h.upload(ZONES, "zones.csv").json()["session_id"]
    holder["profile"] = h.client.get(f"/ingest/sessions/{sid}").json()["profile"]
    r = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r.status_code == 202
    job = h.wait(r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["ok"] is True
    kinds = [e["kind"] for e in job["events"]]
    assert "message" in kinds and kinds[-1] == "finished"
    d = h.client.get(f"/ingest/proposals/{job['proposal_id']}").json()
    assert d["kind"] == "model" and d["model"] == "scripted" and d["validation"]["ok"]
    # the stream replays the events and ends
    s = h.client.get(f"/ingest/jobs/{job['id']}/stream")
    assert s.status_code == 200 and "event: finished" in s.text
    # repair makes a new 'repair' proposal chained to the first
    j2 = h.wait(h.client.post(f"/ingest/proposals/{job['proposal_id']}/repair").json()["job_id"])
    assert j2["status"] == "done"
    d2 = h.client.get(f"/ingest/proposals/{j2['proposal_id']}").json()
    assert d2["kind"] == "repair" and d2["parent_id"] == job["proposal_id"]
    sess = h.client.get(f"/ingest/sessions/{sid}").json()
    assert [p["kind"] for p in sess["proposals"]] == ["model", "repair"]
    # cost recorded in the table (0 for the scripted model), daily budget enforced
    monkeypatch.setenv("ISNAD_AGENT_DAILY_USD", "0")
    r = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"


def test_concurrency_and_rate_limits(make_harness, monkeypatch):
    holder = {}
    model = Scripted(lambda: holder["profile"])
    model.delay = 1.5
    h = make_harness(model_factory=lambda p: model)
    sid = h.upload(ZONES, "zones.csv").json()["session_id"]
    holder["profile"] = h.client.get(f"/ingest/sessions/{sid}").json()["profile"]
    monkeypatch.setenv("ISNAD_AGENT_MAX_CONCURRENT", "1")
    r1 = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r1.status_code == 202
    r2 = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r2.status_code == 429 and "retry-after" in {k.lower() for k in r2.headers}
    h.wait(r1.json()["job_id"])
    monkeypatch.setenv("ISNAD_AGENT_RUNS_PER_MIN", "1")
    model.delay = 0
    r3 = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r3.status_code == 429                                    # the minute budget was spent by r1


def test_session_quota_429(h, monkeypatch):
    monkeypatch.setenv("ISNAD_INGEST_SESSIONS_PER_USER", "2")
    assert h.upload(ZONES, "a.csv").status_code == 201
    assert h.upload(ZONES, "b.csv").status_code == 201
    r = h.upload(ZONES, "c.csv")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"


def test_self_approval_blocked_with_auth(h, monkeypatch):
    sid = h.upload(ZONES, "zones.csv").json()["session_id"]
    pid = h.baseline(sid)
    monkeypatch.setenv("ISNAD_AUTH", "1")
    r = h.client.post(f"/ingest/proposals/{pid}/approve", json={"comment": "ok"})
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    r = h.client.post(f"/ingest/proposals/{pid}/approve", json={"comment": "ok"}, headers={"x-user": "someone-else"})
    assert r.status_code == 201
    did = r.json()["decision_id"]
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done"
    # the import itself re-checks: an approval by the creator (made while auth was off) is refused when auth is on
    sid2 = h.upload(ZONES, "zones2.csv").json()["session_id"]
    pid2 = h.baseline(sid2)
    monkeypatch.delenv("ISNAD_AUTH")
    did2 = h.approve(pid2).json()["decision_id"]
    monkeypatch.setenv("ISNAD_AUTH", "1")
    job = h.wait(h.client.post(f"/ingest/decisions/{did2}/import").json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "self_approval"


def test_row_cap(make_harness, monkeypatch):
    h = make_harness()
    monkeypatch.setenv("ISNAD_INGEST_MAX_IMPORT_ROWS", "2")
    sid, job, did = _import(h, ZONES, "zones.csv")
    assert job["status"] == "failed" and job["error"]["code"] == "too_many_rows"
    assert not _exists(h, h.schemas()[0])
