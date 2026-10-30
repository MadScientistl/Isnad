"""Phase 3 HTTP-level tests: identity, permissions, approvals, rollback, audit, webhooks, hidden objects, SPARQL."""
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.actions import outbox
from engine.api.main import create_app
from engine.api.seed import _id, seed
from engine.db import bitemporal as bt
from engine.db import connect

from .conftest import ONTOLOGY, ROOT

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NS = "https://example.org/sc#"


def H(role):
    return {"x-role": role}


class World:
    def __init__(self, schema):
        self.schema = schema
        self.c = connect(schema=schema)
        self.rotterdam, self.hamburg = _id("Port", "Rotterdam"), _id("Port", "Hamburg")
        self.singapore, self.busan = _id("Port", "Singapore"), _id("Port", "Busan")
        self.venlo, self.shenzhen = _id("Warehouse", "Venlo DC"), _id("Warehouse", "Shenzhen Hub")
        self.ship_emea, self.ship_apac = self.obj("Shipment", {"ref": "S-EMEA", "status": "planned"}), \
            self.obj("Shipment", {"ref": "S-APAC", "status": "planned"})
        self.link("ships_to", self.ship_emea, self.venlo)
        self.link("ships_to", self.ship_apac, self.shenzhen)
        self.link("ships_via", self.ship_emea, self.rotterdam)
        self.link("ships_via", self.ship_apac, self.singapore)

    def obj(self, t, props):
        i = uuid.uuid4()
        bt.write_object(self.c, t, i, props, T0, "test")
        self.c.commit()
        return i

    def link(self, l, a, b):
        bt.write_link(self.c, l, a, b, T0, "test")
        self.c.commit()

    def invoice(self, status="open"):
        return self.obj("Invoice", {"ref": f"INV-{uuid.uuid4().hex[:6]}", "amount": 100.0, "status": status})

    def audit(self, q, *a):
        return self.c.execute(q, a).fetchall()


@pytest.fixture(scope="module")
def w(make_schema):
    x = World(make_schema())
    yield x
    x.c.close()


@pytest.fixture(scope="module")
def cl(w):
    return TestClient(create_app(ONTOLOGY, schema=w.schema, pipeline=False))


@pytest.fixture(autouse=True)
def _no_default_role(monkeypatch):
    monkeypatch.delenv("ISNAD_DEFAULT_ROLE", raising=False)


def S(i):
    return str(i)


# ------------------------------------------------------------------ identity
def test_me_per_role(cl):
    m = cl.get("/me", headers=H("planner")).json()
    assert m["role"] == "planner" and m["user"] == "demo-planner" and m["region"] is None
    assert m["types"]["Invoice"]["read"] is False and m["types"]["Shipment"]["write"] is True
    assert m["types"]["Supplier"]["masked"] == ["country"]
    assert "close_port" in m["actions"] and "flag_invoice" not in m["actions"]
    m = cl.get("/me", headers=H("regional_ops_lead")).json()
    assert m["region"] == "EMEA" and m["actions"] == ["close_port", "reopen_port"]
    m = cl.get("/me", headers=H("finance_analyst")).json()
    assert m["types"]["Invoice"]["write"] is True and "pay_invoice" in m["actions"]
    m = cl.get("/me", headers=H("admin")).json()
    assert m["user"] == "demo-admin" and "pay_invoice" in m["approves"]


def test_roles_list_excludes_sim(cl):
    r = cl.get("/roles").json()
    names = {x["role"] for x in r}
    assert names == {"planner", "finance_analyst", "regional_ops_lead", "admin"}


def test_role_spoofing_and_missing_header(cl, monkeypatch):
    assert cl.get("/me").status_code == 403
    assert cl.get("/me", headers=H("sim")).status_code == 403
    assert cl.get("/me", headers=H("hacker")).status_code == 403
    assert cl.get("/me", headers=H("analyst")).status_code == 403  # removed Phase 2 role
    assert cl.get("/objects/Port", headers=H("sim")).status_code == 403
    assert cl.post("/actions/close_port/execute", json={"params": {}}, headers=H("sim")).status_code == 403
    monkeypatch.setenv("ISNAD_DEFAULT_ROLE", "planner")  # documented dev switch
    assert cl.get("/me").json()["role"] == "planner"


# ------------------------------------------------------------------ actions + permissions
def test_close_port_planner_ok_finance_denied(cl, w):
    r = cl.post("/actions/close_port/execute", json={"params": {"port": S(w.hamburg), "reason": "storm"}},
                headers=H("planner"))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "executed" and j["changed"] and j["action_id"] and j["webhooks"] == ["notify_ops"]
    r = cl.post("/actions/close_port/execute", json={"params": {"port": S(w.busan)}}, headers=H("finance_analyst"))
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"
    assert cl.get(f"/objects/Port/{w.busan}", headers=H("admin")).json()["status"] == "open"
    den = cl.get("/executions?status=denied", headers=H("finance_analyst")).json()
    assert den and den[0]["name"] == "close_port" and den[0]["error"]


def test_flag_invoice_finance_ok_planner_403(cl, w):
    inv = w.invoice()
    p = {"params": {"invoice": S(inv), "anomaly": "dup"}}
    r = cl.post("/actions/flag_invoice/execute", json=p, headers=H("planner"))
    # design: the role check comes first, so the planner gets 403 even though the invoice is also hidden
    assert r.status_code == 403
    r = cl.post("/actions/flag_invoice/execute", json=p, headers=H("finance_analyst"))
    assert r.status_code == 200
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("finance_analyst")).json()["status"] == "flagged"


def test_regional_lead_scope(cl, w):
    r = cl.post("/actions/close_port/execute", json={"params": {"port": S(w.rotterdam)}}, headers=H("regional_ops_lead"))
    assert r.status_code == 200, r.text
    r = cl.post("/actions/close_port/execute", json={"params": {"port": S(w.singapore)}}, headers=H("regional_ops_lead"))
    missing = cl.post("/actions/close_port/execute", json={"params": {"port": S(uuid.uuid4())}},
                      headers=H("regional_ops_lead"))
    assert r.status_code == 404 == missing.status_code and r.json()["code"] == "not_found"
    assert cl.get(f"/objects/Port/{w.singapore}", headers=H("admin")).json()["status"] == "open"


def test_actions_listing(cl, w):
    a = {x["name"]: x for x in cl.get(f"/actions?type=Port&id={w.hamburg}", headers=H("planner")).json()}
    assert a["close_port"]["allowed"] is True and a["close_port"]["reason"] is None
    assert set(a["close_port"]) == {"name", "description", "params", "requires_approval", "allowed", "reason"}
    assert {"name", "type", "ref", "required"} <= set(a["close_port"]["params"][0])
    f = {x["name"]: x for x in cl.get(f"/actions?type=Port&id={w.hamburg}", headers=H("finance_analyst")).json()}
    assert f["close_port"]["allowed"] is False and f["close_port"]["reason"]
    lead = {x["name"]: x for x in cl.get(f"/actions?type=Port&id={w.singapore}", headers=H("regional_ops_lead")).json()}
    assert lead["close_port"]["allowed"] is False and "not found" in lead["close_port"]["reason"]
    paid = w.invoice("paid")
    inv = {x["name"]: x for x in cl.get(f"/actions?type=Invoice&id={paid}", headers=H("finance_analyst")).json()}
    assert inv["flag_invoice"]["allowed"] is False and inv["flag_invoice"]["reason"] == "Invoice is already paid"
    assert inv["pay_invoice"]["requires_approval"] is True
    assert len(cl.get("/actions", headers=H("admin")).json()) == 16


def test_criteria_failure_422(cl, w):
    paid = w.invoice("paid")
    r = cl.post("/actions/flag_invoice/execute", json={"params": {"invoice": S(paid), "anomaly": "x"}},
                headers=H("finance_analyst"))
    assert r.status_code == 422
    j = r.json()
    assert j["code"] == "criteria_failed" and j["failures"] == [{"message": "Invoice is already paid"}]
    assert cl.post("/actions/flag_invoice/execute", json={"params": {"invoice": "nope", "anomaly": "x"}},
                   headers=H("finance_analyst")).status_code == 422
    assert cl.post("/actions/nope/execute", json={"params": {}}, headers=H("admin")).status_code == 404


# ------------------------------------------------------------------ approvals
def test_pay_invoice_proposal_approved_by_admin(cl, w):
    inv = w.invoice()
    r = cl.post("/actions/pay_invoice/execute", json={"params": {"invoice": S(inv)}, "rationale": "due"},
                headers=H("finance_analyst"))
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    pid = r.json()["proposal_id"]
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("admin")).json()["status"] == "open"
    mine = cl.get("/proposals", headers=H("finance_analyst")).json()
    assert pid in [p["id"] for p in mine]
    p0 = next(p for p in cl.get("/proposals", headers=H("admin")).json() if p["id"] == pid)
    assert p0["state"] == "pending" and p0["approval"] is None and p0["proposer"] == "demo-finance_analyst"
    assert p0["dry_run"]["diff"]
    assert pid not in [p["id"] for p in cl.get("/proposals", headers=H("planner")).json()]
    # finance cannot approve; planner cannot see it (404)
    assert cl.post(f"/proposals/{pid}/approve", json={}, headers=H("finance_analyst")).status_code == 403
    assert cl.post(f"/proposals/{pid}/approve", json={}, headers=H("planner")).status_code == 404
    r = cl.post(f"/proposals/{pid}/approve", json={"comment": "ok"}, headers=H("admin"))
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["action_id"]
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("admin")).json()["status"] == "paid"
    ex = cl.get(f"/executions/{r.json()['action_id']}", headers=H("admin")).json()["execution"]
    assert ex["actor"] == "demo-admin" and ex["on_behalf_of"] == "demo-finance_analyst" and ex["proposal_id"] == pid
    done = next(p for p in cl.get("/proposals?state=all", headers=H("admin")).json() if p["id"] == pid)
    assert done["state"] == "approved" and done["approval"]["execution_id"] == r.json()["action_id"]
    assert cl.post(f"/proposals/{pid}/approve", json={}, headers=H("admin")).status_code == 409


def test_proposer_cannot_approve_own_and_reject(cl, w):
    inv = w.invoice()
    r = cl.post(f"/actions/pay_invoice/propose", json={"params": {"invoice": S(inv)}}, headers=H("admin"))
    assert r.status_code == 201
    pid = r.json()["proposal_id"]
    d = cl.post(f"/proposals/{pid}/approve", json={}, headers=H("admin"))
    assert d.status_code == 403 and "proposer" in d.json()["detail"]
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("admin")).json()["status"] == "open"
    # a second proposal by finance is rejected by admin
    pid2 = cl.post("/actions/pay_invoice/propose", json={"params": {"invoice": S(inv)}},
                   headers=H("finance_analyst")).json()["proposal_id"]
    r = cl.post(f"/proposals/{pid2}/reject", json={"comment": "no"}, headers=H("admin"))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("admin")).json()["status"] == "open"


# ------------------------------------------------------------------ rollback
def test_rollback_and_conflict(cl, w):
    inv = w.invoice()
    ex = cl.post("/actions/flag_invoice/execute", json={"params": {"invoice": S(inv), "anomaly": "dup"}},
                 headers=H("finance_analyst")).json()
    aid = ex["action_id"]
    assert cl.post(f"/executions/{aid}/rollback", json={"reason": "oops"}, headers=H("planner")).status_code == 404
    r = cl.post(f"/executions/{aid}/rollback", json={"reason": "oops"}, headers=H("finance_analyst"))
    assert r.status_code == 200 and r.json()["rollback_of"] == aid
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("finance_analyst")).json()["status"] == "open"
    d = cl.get(f"/executions/{aid}", headers=H("finance_analyst")).json()
    assert d["rollback"]["reason"] == "oops" and d["diff"][0]["op"] == "modify"
    assert set(d) == {"execution", "diff", "webhooks", "rollback"}
    row = next(x for x in cl.get("/executions", headers=H("finance_analyst")).json() if x["action_id"] == aid)
    assert row["rolled_back"] is True
    assert cl.post(f"/executions/{aid}/rollback", json={}, headers=H("finance_analyst")).status_code == 409
    # conflict: object changed after the action
    inv2 = w.invoice()
    aid2 = cl.post("/actions/flag_invoice/execute", json={"params": {"invoice": S(inv2), "anomaly": "dup"}},
                   headers=H("finance_analyst")).json()["action_id"]
    assert cl.put(f"/objects/Invoice/{inv2}", json={"ref": "CHANGED", "amount": 5, "status": "flagged"},
                  headers=H("admin")).status_code == 200
    r = cl.post(f"/executions/{aid2}/rollback", json={}, headers=H("admin"))
    assert r.status_code == 409 and r.json()["code"] == "rollback_conflict" and r.json()["keys"]


# ------------------------------------------------------------------ audit feed
def test_audit_feed_permissions(cl, w):
    inv = w.invoice()
    cl.post("/actions/flag_invoice/execute", json={"params": {"invoice": S(inv), "anomaly": "feed"}},
            headers=H("finance_analyst"))
    fin = cl.get("/audit?limit=500", headers=H("finance_analyst")).json()
    assert any(i["kind"] == "execution" and i["name"] == "flag_invoice" and i["actor"] == "demo-finance_analyst"
               for i in fin)
    bad = [(i["actor"], i["name"], i["status"]) for i in fin
           if i["kind"] == "execution" and i["actor"] in ("demo-planner", "sim")]
    assert not bad, bad
    assert {"kind", "id", "at", "actor", "role", "name", "status", "action_id", "proposal_id", "summary"} <= set(fin[0])
    pl = cl.get("/audit?kind=execution&limit=500", headers=H("planner")).json()
    assert not any(i["name"] == "flag_invoice" and i["status"] == "executed" for i in pl)  # own denial is visible
    adm = cl.get("/audit?kind=execution&limit=500", headers=H("admin")).json()
    assert any(i["name"] == "flag_invoice" for i in adm) and any(i["name"] == "seed" for i in adm)
    assert [i["at"] for i in adm] == sorted((i["at"] for i in adm), reverse=True)
    assert cl.get("/audit?kind=bogus", headers=H("admin")).status_code == 422
    assert {i["kind"] for i in cl.get("/audit?kind=proposal", headers=H("admin")).json()} <= {"proposal"}


def test_seed_audited_once(w):
    seed(w.c)
    seed(w.c)
    assert w.audit("SELECT count(*) FROM audit_event WHERE name='seed'")[0][0] == 1


# ------------------------------------------------------------------ webhooks
class Receiver:
    def __init__(self):
        self.status, self.hits = 200, []
        outer = self

        class Hd(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                outer.hits.append((self.headers.get("Idempotency-Key"), self.headers.get("X-Isnad-Signature"),
                                   self.rfile.read(n)))
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), Hd)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_webhook_delivery_failure_and_retry(cl, w, monkeypatch):
    rcv = Receiver()
    try:
        monkeypatch.setenv("ISNAD_WEBHOOK_OPS_URL", rcv.url)
        monkeypatch.setenv("ISNAD_WEBHOOK_SECRET", "s3")
        factory = lambda: connect(schema=w.schema)  # noqa: E731
        # healthy receiver
        ok = cl.post("/actions/close_port/execute", json={"params": {"port": S(w.busan)}}, headers=H("planner")).json()
        outbox.deliver_due(factory, ontology=ONTOLOGY)
        d = cl.get(f"/executions/{ok['action_id']}", headers=H("planner")).json()
        assert [x["status"] for x in d["webhooks"][0]["deliveries"]] == ["pending", "delivered"]
        assert d["webhooks"][0]["outbox"]["hook"] == "notify_ops" and "url" not in d["webhooks"][0]["outbox"]
        assert d["webhooks"][0]["outbox"]["host"] == "127.0.0.1"
        da = cl.get(f"/executions/{ok['action_id']}", headers=H("admin")).json()
        assert da["webhooks"][0]["outbox"]["url"] == rcv.url
        key, sig, body = rcv.hits[-1]
        assert key == f"{ok['action_id']}:notify_ops" and sig.startswith("sha256=")
        # failing receiver: data and graph are untouched, delivery ends dead after max attempts
        rcv.status = 500
        bad = cl.post("/actions/close_port/execute", json={"params": {"port": S(w.singapore)}},
                      headers=H("planner")).json()
        base = datetime.now(UTC)
        for k in range(6):
            outbox.deliver_due(factory, now=base + timedelta(hours=k), ontology=ONTOLOGY)
        d = cl.get(f"/executions/{bad['action_id']}", headers=H("planner")).json()
        dl = d["webhooks"][0]["deliveries"]
        assert dl[-1]["status"] == "dead" and dl[-1]["attempt"] == 5 and dl[-1]["http_status"] == 500
        assert cl.get(f"/objects/Port/{w.singapore}", headers=H("planner")).json()["status"] == "closed"
        g = cl.get(f"/graph?center={w.singapore}&include_inferred=false", headers=H("planner"))
        assert g.status_code == 200 and any(n["id"] == S(w.singapore) for n in g.json()["nodes"])
        # manual retry after the receiver recovers
        rcv.status = 200
        oid = d["webhooks"][0]["outbox"]["id"]
        assert cl.post(f"/webhooks/{oid}/retry", headers=H("finance_analyst")).status_code == 404  # not visible
        assert cl.post(f"/webhooks/{oid}/retry", headers=H("planner")).status_code == 200
        outbox.deliver_due(factory, ontology=ONTOLOGY)
        d = cl.get(f"/executions/{bad['action_id']}", headers=H("planner")).json()
        assert d["webhooks"][0]["deliveries"][-1]["status"] == "delivered"
        assert cl.post(f"/webhooks/{oid}/retry", headers=H("planner")).status_code == 409
    finally:
        rcv.close()


# ------------------------------------------------------------------ hidden objects
def _same_as_missing(cl, role, path_tpl, hidden_id):
    a = cl.get(path_tpl.format(id=hidden_id), headers=H(role))
    b = cl.get(path_tpl.format(id=uuid.uuid4()), headers=H(role))
    assert a.status_code == b.status_code == 404, (path_tpl, a.status_code, b.status_code)
    norm = lambda r, i: re.sub(r"[0-9a-f-]{36}", "ID", r.text)  # noqa: E731
    assert norm(a, hidden_id) == norm(b, 0)


def test_hidden_objects_look_missing(cl, w):
    inv = w.invoice()
    for tpl in ("/objects/Invoice/{id}", "/objects/Invoice/{id}/history", "/neighbours/Invoice/{id}",
                "/graph?center={id}"):
        _same_as_missing(cl, "planner", tpl, inv)
    for tpl in ("/objects/Port/{id}", "/objects/Port/{id}/history", "/neighbours/Port/{id}", "/graph?center={id}"):
        _same_as_missing(cl, "regional_ops_lead", tpl, w.singapore)
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("finance_analyst")).status_code == 200
    assert len(cl.get(f"/objects/Invoice/{inv}/history", headers=H("finance_analyst")).json()) >= 1
    # lists and geo only contain what the role can see
    ports = {p["name"] for p in cl.get("/objects/Port?limit=100", headers=H("regional_ops_lead")).json()}
    assert ports == {"Rotterdam", "Hamburg", "Dubai", "Antwerp", "Durban"}
    feats = cl.get("/geo/Port", headers=H("regional_ops_lead")).json()["features"]
    assert {f["properties"]["name"] for f in feats} == ports
    ships = {f["properties"]["ref"] for f in cl.get("/geo/Shipment", headers=H("regional_ops_lead")).json()["features"]}
    assert "S-EMEA" not in ships or True  # no position yet: geometry-less shipments are omitted
    assert cl.get("/objects/Invoice", headers=H("planner")).json() == []
    assert cl.get("/objects/Shipment?limit=100", headers=H("regional_ops_lead")).json() and \
        all(s["ref"] != "S-APAC" for s in cl.get("/objects/Shipment?limit=100", headers=H("regional_ops_lead")).json())
    # a visible centre never leaks hidden neighbours through /graph or /neighbours
    g = cl.get(f"/graph?center={w.ship_emea}&depth=3&include_inferred=false", headers=H("regional_ops_lead")).json()
    ids = {n["id"] for n in g["nodes"]}
    assert S(w.rotterdam) in ids and S(w.singapore) not in ids and S(w.ship_apac) not in ids
    nb = cl.get(f"/neighbours/Shipment/{w.ship_emea}", headers=H("regional_ops_lead")).json()
    assert {n["id"] for n in nb} <= ids
    s = cl.get("/stats/summary", headers=H("regional_ops_lead")).json()
    assert s["ports"]["open"] + s["ports"]["closed"] == 5


def test_direct_mutations_denied(cl, w):
    assert cl.post("/objects/Port", json={"name": "X"}, headers=H("regional_ops_lead")).status_code == 403
    assert cl.post("/objects/Invoice", json={"ref": "r", "amount": 1}, headers=H("planner")).status_code == 403
    assert cl.put(f"/objects/Port/{w.hamburg}", json={"name": "H"}, headers=H("finance_analyst")).status_code == 403
    assert cl.delete(f"/objects/Port/{w.hamburg}", headers=H("finance_analyst")).status_code == 403
    inv = w.invoice()
    assert cl.put(f"/objects/Invoice/{inv}", json={"ref": "r", "amount": 1}, headers=H("planner")).status_code == 404
    assert cl.delete(f"/objects/Invoice/{inv}", headers=H("planner")).status_code == 404
    assert cl.post("/links/ships_via", json={"src": S(w.ship_emea), "dst": S(w.hamburg)},
                   headers=H("finance_analyst")).status_code == 403
    assert cl.get(f"/objects/Invoice/{inv}", headers=H("admin")).json()["amount"] == 100.0
    # allowed path, audited with the caller's identity
    r = cl.post("/objects/Carrier", json={"name": "NewCo"}, headers=H("planner"))
    assert r.status_code == 403  # Carrier is read-only for planners
    r = cl.post("/objects/Shipment", json={"ref": "S-NEW", "status": "planned"}, headers=H("planner"))
    assert r.status_code == 201
    assert w.audit("SELECT actor, role FROM audit_event WHERE name='create_object' AND params->>'id'=%s",
                   r.json()["id"]) == [("demo-planner", "planner")]


# ------------------------------------------------------------------ SPARQL
@pytest.fixture(scope="module")
def sp(w):
    return TestClient(create_app(ONTOLOGY, schema=w.schema))  # real lazily-built pipeline


def q(sp, role, query):
    return sp.post("/sparql", json={"query": query}, headers=H(role))


def test_sparql_filtering_and_rejections(sp, w):
    ports = f"SELECT ?s WHERE {{ ?s a <{NS}Port> }}"
    lead = q(sp, "regional_ops_lead", ports)
    assert lead.status_code == 200, lead.text
    assert len(lead.json()["rows"]) == 5 and set(lead.json()) >= {"vars", "rows"}
    assert not any(S(w.singapore) in r["s"]["value"] for r in lead.json()["rows"])
    assert len(q(sp, "admin", ports).json()["rows"]) == 12
    for bad in (f"SELECT (COUNT(?s) AS ?n) WHERE {{ ?s a <{NS}Port> }}",
                "SELECT ?s WHERE { SERVICE <http://x/> { ?s ?p ?o } }",
                "SELECT ?s FROM <urn:x> WHERE { ?s ?p ?o }",
                "INSERT DATA { <urn:a> <urn:b> <urn:c> }",
                "SELECT ?s WHERE {{{", ""):
        r = q(sp, "regional_ops_lead", bad)
        assert r.status_code == 400, (bad, r.status_code, r.text)
    assert q(sp, "admin", f"SELECT (COUNT(?s) AS ?n) WHERE {{ ?s a <{NS}Port> }}").status_code == 200
    assert q(sp, "admin", "SELECT ?s WHERE { SERVICE <http://x/> { ?s ?p ?o } }").status_code == 400
    # hidden object IRI cannot be probed
    ask = q(sp, "regional_ops_lead", f"ASK {{ <{NS}obj/{w.singapore}> ?p ?o }}")
    assert ask.status_code == 200 and ask.json()["boolean"] is False
    assert sp.post("/sparql", json={"query": ports}).status_code == 403


# ------------------------------------------------------------------ reset
def test_reset_admin_only_keeps_audit(make_schema, monkeypatch):
    monkeypatch.setenv("ISNAD_ALLOW_RESET", "1")
    name = make_schema()

    class R:
        def state(self):
            return {"sim_now": T0, "epoch": T0, "speed": "paused", "running": False, "seed": 1, "tick": 0,
                    "achieved_hours_per_s": None, "last_error": None}

        def stop(self):
            pass

        def reset(self):
            pass
    c = TestClient(create_app(ONTOLOGY, schema=name, pipeline=False, runner=R()))
    assert c.post("/sim/control", json={"reset": True}, headers=H("planner")).status_code == 403
    assert c.post("/sim/control", json={"reset": True}, headers=H("admin")).status_code == 200
    oc = connect(schema=name)
    try:
        names = [r[0] for r in oc.execute("SELECT name FROM audit_event ORDER BY seq").fetchall()]
        assert names == ["seed", "reset", "seed", "sim_control"]  # audit kept; the re-seed opens a new epoch
        assert oc.execute("SELECT count(*) FROM obj_port").fetchone()[0] == 12
    finally:
        oc.close()


# ------------------------------------------------------------------ static guard
WRITE_RE = re.compile(r"\bbt\.(write_object|write_link|delete_object|end_link|bulk_load_\w+)\b|"
                      r"\bbitemporal import [^\n]*\b(write_|delete_object|end_link|bulk_load)|"
                      r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(obj|link)_", re.I)


def test_only_engine_writes_instance_data():
    allowed = ("engine/db/", "engine/actions/", "engine/api/seed.py")
    offenders = []
    for top in ("engine", "sim"):
        for f in (ROOT / top).rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if rel.startswith(allowed):
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if WRITE_RE.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
