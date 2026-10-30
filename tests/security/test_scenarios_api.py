"""Scenario isolation through the HTTP API (Phase 5): hidden objects and masked fields never leak through compare /
KPIs / diffs, ownership and sharing rules hold, nothing leaves a scenario (webhooks, main rows, main audit)."""
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.api import masking  # noqa: E402
from engine.api.main import create_app  # noqa: E402
from engine.api.seed import _id, seed  # noqa: E402
from engine.compiler import compile_ontology  # noqa: E402
from engine.db import bitemporal as bt, connect, get_dsn  # noqa: E402

ONTOLOGY = str(ROOT / "ontology" / "supply_chain.yaml")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def H(role, scn=None):
    h = {"x-role": role}
    if scn is not None:
        h["x-scenario"] = str(scn)
    return h


@pytest.fixture(scope="module")
def sch():
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    name = f"sca_{uuid.uuid4().hex[:10]}"
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    c = connect(schema=name)
    seed(c)
    c.close()
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture(scope="module")
def cl(sch):
    return TestClient(create_app(ONTOLOGY, schema=sch, pipeline=False))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AMARH_DEFAULT_ROLE", raising=False)
    monkeypatch.setenv("AMARH_OUTBOX_WORKER", "0")
    monkeypatch.setenv("AMARH_SCN_MAX_GLOBAL", "100")
    monkeypatch.setenv("AMARH_SCN_MAX_PER_USER", "50")


@pytest.fixture(scope="module")
def w(sch):
    """Hidden-from-lead / hidden-from-planner data: an APAC shipment (cargo 777) via Singapore, an invoice for an
    EMEA shipment, and a supplier whose country must stay masked for the planner."""
    c = connect(schema=sch)
    ids = {"apac_ship": uuid.uuid4(), "emea_ship": uuid.uuid4(), "inv": uuid.uuid4(), "sup": uuid.uuid4()}
    bt.write_object(c, "Shipment", ids["apac_ship"], {"ref": "SECRET-APAC", "status": "in_transit", "cargo_value": 777.0}, T0, "t")
    bt.write_link(c, "ships_via", ids["apac_ship"], _id("Port", "Singapore"), T0, "t")
    bt.write_link(c, "ships_to", ids["apac_ship"], _id("Warehouse", "Shenzhen Hub"), T0, "t")
    bt.write_object(c, "Shipment", ids["emea_ship"], {"ref": "EMEA-1", "status": "in_transit", "cargo_value": 1000.0}, T0, "t")
    bt.write_link(c, "ships_via", ids["emea_ship"], _id("Port", "Rotterdam"), T0, "t")
    bt.write_link(c, "ships_to", ids["emea_ship"], _id("Warehouse", "Venlo DC"), T0, "t")
    bt.write_object(c, "Invoice", ids["inv"], {"ref": "INV-SECRET", "amount": 4242.0, "status": "open"}, T0, "t")
    bt.write_link(c, "invoices_for", ids["inv"], ids["emea_ship"], T0, "t")
    bt.write_object(c, "Supplier", ids["sup"], {"name": "MaskCo", "country": "ZZ-SECRET"}, T0, "t")
    c.commit()
    c.close()
    return {k: str(v) for k, v in ids.items()}


@pytest.fixture
def mk(cl):
    made = []

    def make(role="admin", **body):
        r = cl.post("/scenarios", json={"name": "sec", **body}, headers=H(role))
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()["id"]
    yield make
    for sid in made:
        cl.delete(f"/scenarios/{sid}", headers=H("admin"))


def port(n):
    return str(_id("Port", n))


def run(cl, role, sid, name, params, expect=200):
    r = cl.post(f"/actions/{name}/execute", json={"params": params}, headers=H(role, sid))
    assert r.status_code == expect, r.text
    return r


def checksum(sch):
    c = connect(schema=sch)
    parts = []
    for t in ("obj_port", "obj_warehouse", "obj_shipment", "obj_invoice", "obj_supplier", "obj_event",
              "link_ships_via", "link_ships_to", "link_affects", "link_invoices_for", "link_billed_by"):
        parts.append(c.execute(sql.SQL("SELECT coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') FROM {} x "
                                       "WHERE scenario_id = 0").format(sql.Identifier(t))).fetchone()[0])
    for q in ("SELECT count(*) FROM audit_event WHERE scenario_id = 0", "SELECT count(*) FROM webhook_outbox",
              "SELECT count(*) FROM graph_outbox", "SELECT count(*) FROM proposal",
              "SELECT count(*) FROM inferred_support WHERE scenario_id = 0"):
        parts.append(str(c.execute(q).fetchone()[0]))
    c.close()
    return "|".join(parts)


def test_hidden_objects_absent_from_compare_diffs_and_kpis(cl, mk, w):
    sid = mk("admin", shared_roles=["regional_ops_lead", "planner", "finance_analyst"])
    run(cl, "admin", sid, "close_port", {"port": port("Singapore")})        # APAC: invisible to the EMEA lead
    run(cl, "admin", sid, "close_port", {"port": port("Rotterdam")})
    run(cl, "admin", sid, "flag_invoice", {"invoice": w["inv"], "anomaly": "dup"})  # Invoice: invisible to the planner
    adm = cl.get(f"/scenarios/{sid}/compare", headers=H("admin")).json()
    lead = cl.get(f"/scenarios/{sid}/compare", headers=H("regional_ops_lead")).json()
    plan = cl.get(f"/scenarios/{sid}/compare", headers=H("planner")).json()
    fin = cl.get(f"/scenarios/{sid}/compare", headers=H("finance_analyst")).json()
    # --- the lead: no APAC port, no APAC shipment value in KPIs, no invoice
    blob = json.dumps(lead)
    assert port("Singapore") not in blob and "Singapore" not in blob and "SECRET-APAC" not in blob
    assert w["apac_ship"] not in blob and w["inv"] not in blob and "INV-SECRET" not in blob
    assert {d["key"] for d in lead["diff"] if d["type"] == "Port"} == {port("Rotterdam")}
    assert lead["kpis"]["delta"]["ports_closed"] == 1 and adm["kpis"]["delta"]["ports_closed"] == 2
    assert adm["kpis"]["delta"]["value_at_risk"] - lead["kpis"]["delta"]["value_at_risk"] == pytest.approx(777.0)
    assert lead["kpis"]["delta"]["invoices_flagged"] == 0 and lead["kpis"]["delta"]["exposure_suppliers"] == 0
    # --- the planner sees APAC but never invoices
    pblob = json.dumps(plan)
    assert w["inv"] not in pblob and "INV-SECRET" not in pblob and "Invoice" not in {d["type"] for d in plan["diff"]}
    assert plan["kpis"]["delta"]["invoices_flagged"] == 0
    assert port("Singapore") in {d["key"] for d in plan["diff"]}
    assert plan["kpis"]["delta"]["value_at_risk"] >= 777.0
    # --- finance sees the invoice change
    assert w["inv"] in {d["key"] for d in fin["diff"]} and fin["kpis"]["delta"]["invoices_flagged"] == 1
    # the lead's reads of the scenario itself stay filtered too
    assert cl.get(f"/objects/Port/{port('Singapore')}", headers=H("regional_ops_lead", sid)).status_code == 404
    assert cl.get(f"/objects/Invoice/{w['inv']}", headers=H("planner", sid)).status_code == 404
    assert port("Singapore") not in {o["id"] for o in cl.get("/objects/Port", headers=H("regional_ops_lead", sid)).json()}
    assert w["apac_ship"] not in {o["id"] for o in cl.get("/objects/Shipment?limit=1000", headers=H("regional_ops_lead", sid)).json()}
    ev = cl.get("/events", headers=H("regional_ops_lead", sid)).json()
    assert port("Singapore") not in json.dumps(ev)
    sm = cl.get("/stats/summary", headers=H("regional_ops_lead", sid)).json()
    assert sm["ports"]["closed"] == 1 and sm["invoices"] == {"open": 0, "approved": 0, "paid": 0, "flagged": 0}


def test_masked_fields_stay_masked_in_scenarios(cl, mk, w):
    sid = mk("admin", shared_roles=["planner"])
    run(cl, "admin", sid, "update_object", {"type": "Supplier", "id": w["sup"],
                                           "props": json.dumps({"name": "MaskCo", "country": "ZZ-SECRET2", "price_index": 1.5})})
    adm = cl.get(f"/scenarios/{sid}/compare", headers=H("admin")).json()
    assert "ZZ-SECRET2" in json.dumps(adm)                                    # the admin may read it
    p = cl.get(f"/scenarios/{sid}/compare", headers=H("planner")).json()
    d = [x for x in p["diff"] if x["type"] == "Supplier"][0]
    assert d["before"]["country"] is None and d["after"]["country"] is None
    assert d["after"]["price_index"] == 1.5 and "ZZ-SECRET" not in json.dumps(p)
    assert cl.get(f"/objects/Supplier/{w['sup']}", headers=H("planner", sid)).json()["country"] is None
    assert p["kpis"]["delta"]["extra_cost"] == pytest.approx(500.0)
    # executions / history of the scenario are masked for the planner as well
    ex = cl.get("/executions?limit=50", headers=H("planner", sid)).json()
    assert "ZZ-SECRET" not in json.dumps(ex)
    hist = cl.get(f"/objects/Supplier/{w['sup']}/history", headers=H("planner", sid)).json()
    assert hist and all(h["country"] is None for h in hist)
    # dry_run payloads written by scenario proposals: the new keys are masked on output
    om = compile_ontology(ONTOLOGY).model
    planner = type("P", (), {"role": "planner"})()
    snap = {"kind": "object", "type": "Supplier", "key": "k", "op": "modify",
            "before": {"country": "ZZ-SECRET"}, "after": {"country": "ZZ-SECRET2"}}
    out = masking.mask_out(om, planner, {"dry_run": {"diff": [snap], "scenario_diff": [snap], "main_diff": [snap],
                                                     "impact": {"delta": {"kpis": {"extra_cost": 1}}}}})
    assert "ZZ-SECRET" not in json.dumps(out) and out["dry_run"]["impact"]["delta"]["kpis"]["extra_cost"] == 1


def test_ownership_and_admin_override(cl, mk, w):
    sid = mk("planner")
    run(cl, "planner", sid, "close_port", {"port": port("Busan")})
    for role in ("finance_analyst", "regional_ops_lead"):
        assert cl.get(f"/scenarios/{sid}/compare", headers=H(role)).status_code == 404
        assert cl.post(f"/scenarios/{sid}/merge", json={}, headers=H(role)).status_code == 404
        assert cl.post("/actions/close_port/execute", json={"params": {"port": port("Busan")}},
                       headers=H(role, sid)).status_code == 404
        assert cl.put(f"/objects/Port/{port('Busan')}", json={"name": "x"}, headers=H(role, sid)).status_code == 404
    # the planner cannot touch an admin's scenario
    adm = mk("admin")
    assert cl.get(f"/scenarios/{adm}/compare", headers=H("planner")).status_code == 404
    assert cl.post(f"/scenarios/{adm}/merge", json={}, headers=H("planner")).status_code == 404
    assert cl.get(f"/scenarios/{adm}/compare?with={sid}", headers=H("admin")).status_code == 200
    assert cl.get(f"/scenarios/{sid}/compare?with={adm}", headers=H("planner")).status_code == 404   # b side hidden too
    assert cl.get(f"/scenarios/{sid}/compare", headers=H("admin")).status_code == 200
    assert cl.post(f"/scenarios/{sid}/merge", json={}, headers=H("admin")).status_code == 200      # admin may merge anyone's
    assert cl.get(f"/objects/Port/{port('Busan')}", headers=H("planner")).json()["status"] == "closed"
    cl.post("/actions/reopen_port/execute", json={"params": {"port": port("Busan")}}, headers=H("admin"))


def test_shared_role_is_read_only(cl, mk, w):
    sid = mk("planner", shared_roles=["finance_analyst"])
    assert cl.get(f"/scenarios/{sid}", headers=H("finance_analyst")).status_code == 200
    assert cl.get(f"/scenarios/{sid}/compare", headers=H("finance_analyst")).status_code == 200
    assert cl.get("/objects/Port?limit=1", headers=H("finance_analyst", sid)).status_code == 200
    r = cl.post("/actions/flag_invoice/execute", json={"params": {"invoice": w["inv"], "anomaly": "x"}},
                headers=H("finance_analyst", sid))
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"
    assert cl.post("/objects/Event", json={"kind": "n"}, headers=H("finance_analyst", sid)).status_code == 403
    assert cl.post(f"/scenarios/{sid}/merge", json={}, headers=H("finance_analyst")).status_code == 403
    assert cl.delete(f"/scenarios/{sid}", headers=H("finance_analyst")).status_code == 403
    assert cl.post(f"/scenarios/{sid}/simulate", json={"days": 1}, headers=H("finance_analyst")).status_code == 403
    assert cl.post("/actions/flag_invoice/propose", json={"params": {"invoice": w["inv"], "anomaly": "x"}},
                   headers=H("finance_analyst", sid)).status_code == 403
    assert cl.get(f"/scenarios/{sid}", headers=H("planner")).json()["override_rows"] == 0           # nothing got written


def test_webhooks_never_enqueued_from_a_scenario(cl, mk, w, sch):
    sid = mk("admin")
    c = connect(schema=sch)
    n0 = c.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0]
    c.commit()
    r1 = run(cl, "admin", sid, "close_port", {"port": port("Dubai")})                # notify_ops
    r2 = run(cl, "admin", sid, "flag_invoice", {"invoice": w["inv"], "anomaly": "dup"})  # notify_finance
    assert r1.json()["webhooks"] == [] and r2.json()["webhooks"] == []
    assert c.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0] == n0
    assert c.execute("SELECT count(*) FROM webhook_delivery WHERE outbox_id IN (SELECT id FROM webhook_outbox "
                     "WHERE action_id = ANY(%s))", [[uuid.UUID(r1.json()["action_id"]), uuid.UUID(r2.json()["action_id"])]]
                     ).fetchone()[0] == 0
    c.close()
    # the same action on main does enqueue (control)
    m = cl.post("/actions/close_port/execute", json={"params": {"port": port("Dubai")}}, headers=H("admin"))
    assert m.status_code == 200 and m.json()["webhooks"] == ["notify_ops"]
    cl.post("/actions/reopen_port/execute", json={"params": {"port": port("Dubai")}}, headers=H("admin"))


def test_main_is_byte_identical_after_scenario_activity(cl, mk, w, sch):
    before = checksum(sch)
    sid = mk("admin")
    run(cl, "admin", sid, "close_port", {"port": port("Hamburg")})
    run(cl, "admin", sid, "flag_invoice", {"invoice": w["inv"], "anomaly": "dup"})
    run(cl, "admin", sid, "reroute_shipment", {"shipment": w["emea_ship"], "new_port": port("Hamburg")}, expect=422)  # closed
    assert cl.put(f"/objects/Shipment/{w['emea_ship']}", json={"ref": "EDIT", "status": "delayed"}, headers=H("admin", sid)).status_code == 200
    cl.post("/actions/delay_shipment/propose", json={"params": {"shipment": w["emea_ship"], "delay_hours": 2}},
            headers=H("admin", sid))
    cl.get(f"/scenarios/{sid}/compare", headers=H("admin"))
    after_mid = checksum(sch)
    # the proposal row is evidence on main (counts), everything else identical
    b, a = before.split("|"), after_mid.split("|")
    prop_idx = 14                                                              # index of the proposal count
    assert [x for i, x in enumerate(a) if i != prop_idx] == [x for i, x in enumerate(b) if i != prop_idx]
    assert int(a[prop_idx]) == int(b[prop_idx]) + 1
    c = connect(schema=sch)
    n = c.execute("SELECT count(*) FROM audit_event WHERE scenario_id = %s", [sid]).fetchone()[0]
    c.close()
    assert n >= 3                                                              # scenario-tagged audit rows exist
    cl.delete(f"/scenarios/{sid}", headers=H("admin"))
    assert checksum(sch).split("|")[:prop_idx] == b[:prop_idx]                 # discard leaves main byte-identical


def test_forged_scenario_headers(cl, mk, w):
    sid = mk("admin")
    run(cl, "admin", sid, "close_port", {"port": port("Busan")})
    for bad in ("1 OR 1=1", "1;DROP TABLE scenario", "0x1", "1.0", " ", "+1", "1_0", "-1", "9999999999999"):
        r = cl.get("/objects/Port?limit=1", headers={**H("planner"), "x-scenario": bad})
        assert r.status_code in (200, 400), (bad, r.status_code)
        if bad.strip():
            assert r.status_code == 400
    # a planner cannot reach an admin's scenario by id, nor learn whether it exists
    for i in (sid, sid + 1, 10**9):
        assert cl.get("/objects/Port?limit=1", headers=H("planner", i)).status_code == 404
