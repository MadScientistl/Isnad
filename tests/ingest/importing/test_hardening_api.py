"""Review hardening on the ingest API: budget ledger (M1), decision integrity (M2), job ownership and caps (L7),
orphan workspaces (L8), concurrency slots."""
import threading
import time

import pytest

from agent.types import ModelTurn
from tests.ingest.importing.conftest import BIKE
from tests.ingest.importing.test_importing_api import Scripted, ZONES



def ledger(h):
    c = h.conn()
    try:
        return c.execute("SELECT job_id, cost_usd, status FROM ingest_spend WHERE username = %s ORDER BY at",
                         (h.user,)).fetchall()
    finally:
        c.close()


class NoProposalButCostly:
    """Spends tokens and ends without a proposal (a failed run that still cost money)."""
    name = "costly-model"

    def complete(self, *, system, messages, tools, max_tokens, on_text=None, timeout=None):
        return ModelTurn(content=[{"type": "text", "text": "I give up"}], tool_uses=[], stop_reason="end_turn",
                         usage={"input_tokens": 1000, "output_tokens": 1000})


def mk_session(h):
    return h.upload(ZONES, "zones.csv").json()["session_id"]


# ------------------------------------------------------------------ M1
def test_failed_run_still_counts_against_the_daily_budget(make_harness, monkeypatch):
    h = make_harness(model_factory=lambda p: NoProposalButCostly())
    sid = mk_session(h)
    monkeypatch.setenv("ISNAD_AGENT_DAILY_USD", "0.05")
    monkeypatch.setenv("ISNAD_INGEST_GLOBAL_DAILY_USD", "100")
    job = h.wait(h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"}).json()["job_id"])
    assert job["status"] == "failed"
    rows = ledger(h)
    assert [r[2] for r in rows] == ["reserved", "failed"] and float(rows[1][1]) > 0
    monkeypatch.setenv("ISNAD_AGENT_DAILY_USD", str(float(rows[1][1]) * 0.9))      # the failed run's cost alone exhausts it
    r = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"


def test_exception_and_cancel_paths_are_charged(make_harness, monkeypatch):
    import agent.ontogen as og
    h = make_harness(model_factory=lambda p: NoProposalButCostly())
    sid = mk_session(h)

    def boom(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(og, "generate", boom)
    job = h.wait(h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"}).json()["job_id"])
    assert job["status"] == "failed"
    rows = ledger(h)
    assert rows[-1][2] == "error" and float(rows[-1][1]) == pytest.approx(1.0)       # worst case: the reserved cap
    c = h.conn()
    from engine.ingest import store as S
    try:
        assert S.daily_cost(c, h.user) == pytest.approx(1.0)                        # the reservation is not counted twice
    finally:
        c.close()


def test_reservation_blocks_a_second_job_and_global_cap(make_harness, monkeypatch):
    holder = {}
    model = Scripted(lambda: holder["profile"])
    model.delay = 1.0
    h = make_harness(model_factory=lambda p: model)
    sid = mk_session(h)
    holder["profile"] = h.client.get(f"/ingest/sessions/{sid}").json()["profile"]
    monkeypatch.setenv("ISNAD_AGENT_DAILY_USD", "1.0")
    monkeypatch.setenv("ISNAD_INGEST_GLOBAL_DAILY_USD", "100")
    r1 = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r1.status_code == 202
    r2 = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})   # r1's max cost is reserved
    assert r2.status_code == 429
    h.wait(r1.json()["job_id"])
    monkeypatch.setenv("ISNAD_INGEST_GLOBAL_DAILY_USD", "0")
    r3 = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"})
    assert r3.status_code == 429 and r3.json()["code"] == "rate_limited"


def test_ledger_is_append_only(h):
    c = h.conn()
    try:
        from engine.ingest import store as S
        from engine.db.principal import Principal, scoped
        with c.transaction():
            with scoped(c, Principal(role="admin", user=h.user)):
                S.record_spend(c, h.user, "j1", 0.5, "done")
        import psycopg
        with pytest.raises(psycopg.Error):
            with c.transaction():
                c.execute("UPDATE ingest_spend SET cost_usd = 0 WHERE job_id = 'j1'")
    finally:
        c.close()


# ------------------------------------------------------------------ M2
def test_item_decisions_other_than_accept_are_refused(h):
    sid = mk_session(h)
    pid = h.baseline(sid)
    r = h.client.post(f"/ingest/proposals/{pid}/approve",
                      json={"comment": "ok", "items": [{"path": "/objectTypes/0", "action": "reject"}]})
    assert r.status_code == 422 and r.json()["code"] == "use_edit"
    r = h.client.post(f"/ingest/proposals/{pid}/approve",
                      json={"comment": "ok", "items": [{"path": "/objectTypes/0", "action": "edit", "value": 1}]})
    assert r.status_code == 422 and r.json()["code"] == "use_edit"
    r = h.client.post(f"/ingest/proposals/{pid}/approve",
                      json={"comment": "ok", "items": [{"path": "/objectTypes/0", "action": "accept"}]})
    assert r.status_code == 201


def test_rejected_proposal_cannot_be_approved_and_double_approval_is_409(h):
    sid = mk_session(h)
    pid = h.baseline(sid)
    assert h.client.post(f"/ingest/proposals/{pid}/reject", json={"comment": "no"}).status_code == 201
    r = h.approve(pid)
    assert r.status_code == 409 and r.json()["code"] == "rejected"
    pid2 = h.baseline(sid)
    assert h.approve(pid2).status_code == 201
    assert h.approve(pid2).status_code == 409


def test_unique_index_allows_only_one_approval_row(h):
    sid = mk_session(h)
    pid = h.baseline(sid)
    assert h.approve(pid).status_code == 201
    c = h.conn()
    import psycopg
    from engine.ingest import store as S
    from engine.db.principal import Principal, scoped
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with c.transaction():
                with scoped(c, Principal(role="admin", user=h.user)):
                    S.insert_decision(c, pid, "approved", h.user, "admin", None, [])
    finally:
        c.close()


def test_import_refuses_when_rejected_after_approval(h):
    sid = mk_session(h)
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    assert h.client.post(f"/ingest/proposals/{pid}/reject", json={"comment": "changed my mind"}).status_code == 201
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "rejected_after_approval"
    assert h.schemas() and not (h.ws / h.client.get(f"/ingest/sessions/{sid}").json()["slug"]).exists()


# ------------------------------------------------------------------ L7
def test_jobs_belong_to_their_creator(make_harness):
    h = make_harness()
    sid = mk_session(h)
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    jid = h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"]
    other = {"x-user": "another-admin"}
    assert h.client.get(f"/ingest/jobs/{jid}", headers=other).status_code == 404
    assert h.client.get(f"/ingest/jobs/{jid}/stream", headers=other).status_code == 404
    assert h.client.post(f"/ingest/jobs/{jid}/cancel", headers=other).status_code == 404
    assert h.wait(jid)["status"] in ("done", "failed")


def test_job_event_and_retention_caps():
    from engine.ingest import jobs as J
    j = J.Job("model", "u")
    for i in range(2000):
        j.push("message", {"text": "x" * 5000})
    assert len(j.events) == J.MAX_EVENTS == 500
    assert all(len(str(e["payload"])) < 2200 for e in j.events)
    reg = J.JobRegistry()
    for i in range(60):
        reg.completed("baseline", "u1")
    reg.completed("baseline", "u2")
    assert sum(1 for x in reg.jobs.values() if x.user == "u1") == J.KEEP_PER_USER == 50
    assert sum(1 for x in reg.jobs.values() if x.user == "u2") == 1


def test_validation_slot_is_one_per_user(make_harness, monkeypatch):
    import engine.api.ingest_routes as R
    started, release = threading.Event(), threading.Event()

    def slow(profile, proposal, source, *, namespace=None, slug=None):
        started.set()
        release.wait(10)
        from engine.ingest.validate import validate
        return validate(profile, proposal, source, namespace=namespace, slug=slug)
    h = make_harness(validate_fn=slow)
    sid = mk_session(h)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", h.client.post(f"/ingest/sessions/{sid}/proposals",
                                                                           json={"mode": "baseline"})))
    t.start()
    assert started.wait(10)
    r = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "baseline"})
    assert r.status_code == 429 and r.json()["code"] == "busy"
    other = h.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "baseline"}, headers={"x-user": "someone-else"})
    assert other.status_code != 429 or other.json()["code"] != "busy"
    release.set()
    t.join(30)
    assert out["r"].status_code == 202


def test_upload_profiling_concurrency_is_capped(h):
    import engine.api.ingest_routes as R
    # fill the two profiling slots by hand through the module-level behaviour: three simultaneous uploads, slow profile
    from engine.ingest import profile as P
    release = threading.Event()
    real = R.profile_source
    inside = []

    def slow(src, caps=None):
        inside.append(1)
        release.wait(10)
        return real(src, caps=caps)
    R.profile_source = slow
    try:
        res = []
        ts = [threading.Thread(target=lambda i=i: res.append(h.upload(ZONES, f"z{i}.csv"))) for i in range(2)]
        for t in ts:
            t.start()
        t0 = time.time()
        while len(inside) < 2 and time.time() - t0 < 10:
            time.sleep(0.05)
        r3 = h.upload(ZONES, "z3.csv")
        assert r3.status_code == 429 and r3.json()["code"] == "busy"
        release.set()
        for t in ts:
            t.join(30)
        assert [r.status_code for r in res] == [201, 201]
    finally:
        R.profile_source = real


# ------------------------------------------------------------------ L8
def test_orphan_workspace_is_reported_and_retry_replaces_it(h):
    sid = mk_session(h)
    slug = h.client.get(f"/ingest/sessions/{sid}").json()["slug"]
    ghost = h.ws / slug
    ghost.mkdir(parents=True)
    (ghost / "ontology.yaml").write_text("version: 1\n")
    (ghost / "import_report.json").write_text("{}")
    listed = {w["slug"]: w for w in h.client.get("/workspaces").json()}
    assert listed[slug]["status"] == "orphan" and listed[slug]["api_base"] is None
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    job = h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])
    assert job["status"] == "done", job
    listed = {w["slug"]: w for w in h.client.get("/workspaces").json()}
    assert listed[slug]["status"] == "ready"
    assert (h.ws / slug / "mapping.json").exists()


def test_dropped_schema_after_completed_import_is_not_silently_replaced(h):
    import psycopg
    from psycopg import sql
    from engine.db import get_dsn
    sid = mk_session(h)
    pid = h.baseline(sid)
    did = h.approve(pid).json()["decision_id"]
    assert h.wait(h.client.post(f"/ingest/decisions/{did}/import").json()["job_id"])["status"] == "done"
    slug = h.client.get(f"/ingest/sessions/{sid}").json()["slug"]
    admin = psycopg.connect(get_dsn(), autocommit=True)
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier("ws_" + slug)))
    admin.close()
    assert {w["slug"]: w for w in h.client.get("/workspaces").json()}[slug]["status"] == "orphan"
    assert (h.ws / slug).exists()
