"""Regression tests for the Phase 3 security review (F1, F5, F6, F7)."""
import json
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg import sql

from engine.api.main import create_app
from engine.compiler import compile_ontology
from engine.db import connect, get_dsn
from engine.db.setup import apply_schema

from .conftest import ONTOLOGY, ROOT
from .test_phase2 import FakeRunner


def H(role):
    return {"x-role": role}


NON_ADMIN = ["planner", "finance_analyst", "regional_ops_lead"]


# ------------------------------------------------------------------ F1: sim endpoints
@pytest.mark.parametrize("role", NON_ADMIN)
def test_sim_control_and_inject_admin_only_uniform_error(make_schema, role):
    name = make_schema()
    r = FakeRunner()
    c = TestClient(create_app(ONTOLOGY, schema=name, pipeline=False, runner=r))
    calls = [
        ("/sim/control", {"step": True}), ("/sim/control", {"speed": "24"}), ("/sim/control", {"reset": True}),
        ("/sim/inject", {"kind": "port_closure", "target": str(uuid.uuid4())}),   # random / hidden / valid alike
        ("/sim/inject", {"kind": "invoice_anomaly", "target": str(uuid.uuid4())}),
        ("/sim/inject", {"kind": "shipment_delay"}),
    ]
    bodies = set()
    for path, body in calls:
        resp = c.post(path, json=body, headers=H(role))
        assert resp.status_code == 403, (path, body)
        bodies.add(resp.text)
    assert len(bodies) == 1  # identical for every target
    assert r.calls == []  # nothing reached the runner
    oc = connect(schema=name)
    assert oc.execute("SELECT count(*) FROM audit_event WHERE name LIKE 'sim\\_%'").fetchone()[0] == 0
    oc.close()


def test_sim_admin_actions_are_audited_with_actor(make_schema, monkeypatch):
    monkeypatch.delenv("ISNAD_ALLOW_RESET", raising=False)
    name = make_schema()
    c = TestClient(create_app(ONTOLOGY, schema=name, pipeline=False, runner=FakeRunner()))
    assert c.post("/sim/control", json={"step": True}, headers=H("admin")).status_code == 200
    port = str(uuid.uuid4())
    assert c.post("/sim/inject", json={"kind": "port_closure", "target": port}, headers=H("admin")).status_code == 200
    oc = connect(schema=name)
    rows = oc.execute("SELECT name, actor, role, on_behalf_of, source, params FROM audit_event "
                      "WHERE name LIKE 'sim\\_%' ORDER BY seq").fetchall()
    oc.close()
    assert [r[0] for r in rows] == ["sim_control", "sim_inject"]
    assert all(r[1] == "demo-admin" and r[2] == "admin" and r[3] == "demo-admin" for r in rows)
    assert rows[1][5]["target"] == port


def test_sim_state_readable_but_error_text_hidden(make_schema):
    name = make_schema()
    r = FakeRunner()
    st = r.state
    r.state = lambda: {**st(), "last_error": "internal: Port Singapore boom"}
    c = TestClient(create_app(ONTOLOGY, schema=name, pipeline=False, runner=r))
    assert c.get("/sim/state", headers=H("planner")).json()["last_error"] is None
    assert "Singapore" in c.get("/sim/state", headers=H("admin")).json()["last_error"]
    assert c.get("/sim/state").status_code == 403  # no role at all


# ------------------------------------------------------------------ F7: caps + valid_at
def test_params_cap_and_valid_at_admin_only(make_schema):
    name = make_schema()
    c = TestClient(create_app(ONTOLOGY, schema=name, pipeline=False))
    big = {"port": "x" * 70_000}
    assert c.post("/actions/close_port/execute", json={"params": big}, headers=H("planner")).status_code == 413
    assert c.post("/actions/close_port/propose", json={"params": big}, headers=H("planner")).status_code == 413
    old = "2020-01-01T00:00:00Z"
    assert c.post("/actions/close_port/execute", json={"params": {}, "valid_at": old},
                  headers=H("planner")).status_code == 403
    assert c.post("/actions/close_port/propose", json={"params": {}, "valid_at": old},
                  headers=H("planner")).status_code == 403


def test_errors_do_not_echo_postgres_text(make_schema):
    from engine.api.main import map_error
    for exc in (psycopg.errors.CheckViolation("new row for relation \"obj_port\" violates check (secret)"),
                psycopg.errors.UniqueViolation("Key (id)=(deadbeef) already exists"),
                psycopg.errors.ExclusionViolation("conflicting key value (secret-key)")):
        ae = map_error(exc)
        assert ae is not None and "secret" not in ae.body["detail"] and "deadbeef" not in ae.body["detail"]


# ------------------------------------------------------------------ F5: central masking
SECRET = "SECRET-CTRY-9"


@pytest.fixture(scope="module")
def mask_app():
    d = yaml.safe_load(Path(ONTOLOGY).read_text())
    for rn in ("maskwriter", "maskapprover"):
        d["roles"][rn] = {"description": "test role: writes Supplier but cannot see country",
                          "types": {"Supplier": {"read": True, "write": True, "mask": ["country"]}},
                          "links": [], "actions": ["create_object", "update_object", "set_country"]
                          if rn == "maskwriter" else ["set_country"],
                          **({} if rn == "maskwriter" else {"approves": ["set_country"]})}
    d["actions"]["set_country"] = {
        "description": "test: set a supplier country (approval gated)",
        "params": {"supplier": "uuid!", "country": "string!"},
        "roles": ["maskwriter", "maskapprover", "admin"], "approval": {"required": True, "approvers": ["maskapprover", "admin"]},
        "bind": {"s": {"type": "Supplier", "id": "$supplier"}},
        "rules": [{"modify": {"type": "Supplier", "id": "$supplier", "set": {"country": "$country"}}}],
        "rollback": "compensate"}
    for an in ("create_object", "update_object"):
        d["actions"][an]["roles"] = list(d["actions"][an]["roles"]) + ["maskwriter"]
    p = Path(tempfile.mkdtemp()) / "onto.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    name = f"rf_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin, compile_ontology(p).ddl())
    app = TestClient(create_app(str(p), schema=name, pipeline=False))
    yield app, name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


def test_masked_values_never_in_any_response(mask_app):
    c, name = mask_app
    sid = str(uuid.uuid4())
    seen: list[str] = []

    def call(method, path, role, **kw):
        r = c.request(method, path, headers=H(role), **kw)
        seen.append((role, path, r.text))
        return r

    # maskwriter writes the masked column (allowed), via the generic actions
    r = call("POST", "/actions/create_object/execute", "maskwriter",
             json={"params": {"type": "Supplier", "id": sid, "props": json.dumps({"name": "S", "country": SECRET})}})
    assert r.status_code == 200, r.text
    r = call("POST", "/actions/update_object/execute", "maskwriter",
             json={"params": {"type": "Supplier", "id": sid, "props": json.dumps({"name": "S2", "country": SECRET + "b"})}})
    assert r.status_code == 200, r.text
    upd = r.json()["action_id"]
    # approval-gated action: proposal dry_run holds the raw value internally
    r = call("POST", "/actions/set_country/propose", "maskwriter",
             json={"params": {"supplier": sid, "country": SECRET + "c"}, "rationale": "x"})
    assert r.status_code == 201, r.text
    pid = r.json()["proposal_id"]
    stored = psycopg.connect(get_dsn(), autocommit=True)
    stored.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    raw = stored.execute("SELECT dry_run::text FROM proposal WHERE id=%s", [pid]).fetchone()[0]
    stored.close()
    assert SECRET in raw  # stored values are internal and raw ...
    for role in ("maskwriter", "maskapprover"):  # ... but every output is role-masked
        call("GET", "/proposals?state=all", role)
    r = call("POST", f"/proposals/{pid}/approve", "maskapprover", json={})
    assert r.status_code == 200, r.text
    aid = r.json()["action_id"]
    for role in ("maskwriter", "maskapprover"):
        for path in ("/executions", f"/executions/{upd}", f"/executions/{aid}", "/audit", "/events",
                     "/audit?kind=proposal", f"/objects/Supplier/{sid}", f"/objects/Supplier/{sid}/history"):
            call("GET", path, role)
        call("POST", f"/executions/{upd}/rollback", role, json={"reason": "t"})
        call("POST", f"/executions/{aid}/rollback", role, json={"reason": "t"})
    leaks = [(role, path) for role, path, text in seen if SECRET in text]
    assert not leaks, [(r_, p_, t[:700]) for r_, p_, t in seen if (r_, p_) in leaks][:3]
    # admin still sees the real values
    adm = c.get(f"/executions/{aid}", headers=H("admin")).text
    assert SECRET + "c" in adm


# ------------------------------------------------------------------ F2: SPARQL cap + generic errors
def test_sparql_concurrency_cap_and_generic_errors(make_schema, monkeypatch):
    import threading
    import time

    import engine.sync.sparql as S

    class Stub:
        def sync(self):
            pass

        def maybe_reason(self, force=False):
            pass

    monkeypatch.setattr(S, "run_query", lambda *a, **k: (time.sleep(0.8), {"vars": [], "rows": []})[1])
    name = make_schema()
    c = TestClient(create_app(ONTOLOGY, schema=name, pipeline=Stub()))
    codes = []

    def go():
        codes.append(c.post("/sparql", json={"query": "SELECT * WHERE {?s ?p ?o}"}, headers=H("planner")).status_code)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert codes.count(200) == 4 and codes.count(429) == 4, codes
    monkeypatch.undo()
    q = "SELECT * WHERE { # '''\n SERVICE <http://127.0.0.1:1/x> { ?s ?p ?o }\n # '''\n}"
    r = c.post("/sparql", json={"query": q}, headers=H("admin"))
    assert r.status_code == 400 and "127.0.0.1" not in r.text
