import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from engine.actions import ActionResult, apply_action
from engine.db import bitemporal as bt

T0 = datetime(2026, 1, 1, tzinfo=UTC)
P1 = {"type": "Point", "coordinates": [4.5, 51.9]}


def audit(conn, aid=None):
    q = "SELECT seq, action_id, name, actor, source, run_id, params, sim_time, before, after FROM audit_event"
    if aid:
        return conn.execute(q + " WHERE action_id=%s", [aid]).fetchall()
    return conn.execute(q + " ORDER BY seq").fetchall()


def act(conn, name, params, t=T0, **kw):
    return apply_action(conn, name, params, "tester", valid_at=t, **kw)


def mk(conn, type_, props, t=T0):
    i = uuid4()
    act(conn, "create_object", {"type": type_, "id": i, "props": props}, t)
    return i


def rows_with_action(conn, aid):
    n = 0
    for t in ("obj_shipment", "obj_port", "obj_event", "obj_invoice", "obj_supplier",
              "link_affects", "link_billed_by", "link_invoices_for"):
        n += conn.execute(f"SELECT count(*) FROM {t} WHERE action_id=%s", [aid]).fetchone()[0]
    return n


def test_create_update_delete_object_audit(conn):
    r = act(conn, "create_object", {"type": "Supplier", "props": json.dumps({"name": "A"})},
            source="rest", run_id="run1")
    assert isinstance(r, ActionResult) and r.changed
    (sid,) = r.created.values()
    a = audit(conn, r.action_id)
    assert len(a) == 1
    seq, aid, name, actor, source, run_id, params, sim, before, after = a[0]
    assert (name, actor, source, run_id, sim) == ("create_object", "tester", "rest", "run1", T0)
    assert params["type"] == "Supplier" and before[f"Supplier:{sid}"] is None
    assert after[f"Supplier:{sid}"]["name"] == "A"
    assert conn.execute("SELECT count(*) FROM obj_supplier WHERE action_id=%s", [r.action_id]).fetchone()[0] == 1

    r2 = act(conn, "update_object", {"type": "Supplier", "id": sid, "props": {"name": "B", "country": "NL"}},
             T0 + timedelta(hours=1))
    a2 = audit(conn, r2.action_id)[0]
    assert a2[8][f"Supplier:{sid}"]["name"] == "A" and a2[9][f"Supplier:{sid}"]["name"] == "B"
    assert bt.get_object(conn, "Supplier", sid, as_of_valid=T0)["name"] == "A"
    assert bt.get_object(conn, "Supplier", sid, as_of_valid=T0 + timedelta(hours=2))["country"] == "NL"

    r3 = act(conn, "delete_object", {"type": "Supplier", "id": sid}, T0 + timedelta(hours=2))
    assert audit(conn, r3.action_id)[0][9][f"Supplier:{sid}"] is None
    assert bt.get_object(conn, "Supplier", sid, as_of_valid=T0 + timedelta(hours=3)) is None
    assert len(audit(conn)) == 3


def test_noop_update_reports_unchanged_but_audited(conn):
    s = mk(conn, "Supplier", {"name": "A"})
    r = act(conn, "update_object", {"type": "Supplier", "id": s, "props": {"name": "A"}})
    assert r.changed is False and len(audit(conn, r.action_id)) == 1


def test_links(conn):
    s, sh = mk(conn, "Supplier", {"name": "A"}), mk(conn, "Shipment", {"ref": "S"})
    inv = mk(conn, "Invoice", {"ref": "I", "amount": 1.0})
    r = act(conn, "create_link", {"link": "invoices_for", "src": inv, "dst": sh})
    assert r.changed and bt.neighbours(conn, "invoices_for", inv, as_of_valid=T0) == [sh]
    assert audit(conn, r.action_id)[0][9][f"invoices_for:{inv}>{sh}"] == {"exists": True}
    assert rows_with_action(conn, r.action_id) == 1
    r = act(conn, "end_link", {"link": "invoices_for", "src": inv, "dst": sh}, T0 + timedelta(hours=1))
    assert bt.neighbours(conn, "invoices_for", inv, as_of_valid=T0 + timedelta(hours=2)) == []
    assert audit(conn, r.action_id)[0][8][f"invoices_for:{inv}>{sh}"] == {"exists": True}
    # abstract endpoint type: Facility resolves to Port/Warehouse
    port = mk(conn, "Port", {"name": "P"})
    assert act(conn, "create_link", {"link": "ships_from", "src": sh, "dst": port}).changed
    with pytest.raises(ValueError):
        act(conn, "create_link", {"link": "ships_from", "src": sh, "dst": s})  # supplier not a Facility


def test_shipment_lifecycle_and_asof(conn):
    eta = T0 + timedelta(days=5)
    s = mk(conn, "Shipment", {"ref": "S", "eta": eta, "status": "planned"})
    t1, t2, t3, t4 = (T0 + timedelta(hours=h) for h in (1, 2, 3, 4))
    r1 = act(conn, "ship_depart", {"shipment": s}, t1)
    r2 = act(conn, "ship_position", {"shipment": s, "position": P1, "progress": 0.4}, t2)
    r3 = act(conn, "delay_shipment", {"shipment": s, "delay_hours": 12, "reason": "storm"}, t3)
    r4 = act(conn, "ship_arrive", {"shipment": s}, t4)
    g = lambda t: bt.get_object(conn, "Shipment", s, as_of_valid=t)
    assert (g(T0)["status"], g(T0)["progress"]) == ("planned", None)
    assert (g(t1)["status"], g(t1)["progress"]) == ("in_transit", 0.0)
    assert g(t2)["position"] == P1 and g(t2)["progress"] == 0.4
    d = g(t3)
    assert d["status"] == "delayed" and d["delay_hours"] == 12 and d["eta"] == eta + timedelta(hours=12)
    assert d["position"] == P1  # untouched props survive
    assert (g(t4)["status"], g(t4)["progress"]) == ("arrived", 1.0)
    for r in (r1, r2, r3, r4):
        assert r.changed and rows_with_action(conn, r.action_id) >= 1
        assert len(audit(conn, r.action_id)) == 1
    a = audit(conn, r2.action_id)[0]
    assert a[8][f"Shipment:{s}"]["position"] is None and a[9][f"Shipment:{s}"]["position"] == P1
    assert a[7] == t2 and a[6]["position"] == P1


def test_delay_without_eta(conn):
    s = mk(conn, "Shipment", {"ref": "S"})
    act(conn, "delay_shipment", {"shipment": s, "delay_hours": 3}, T0 + timedelta(hours=1))
    r = bt.get_object(conn, "Shipment", s, as_of_valid=T0 + timedelta(hours=2))
    assert r["eta"] is None and r["delay_hours"] == 3


def test_port_close_reopen(conn):
    p = mk(conn, "Port", {"name": "P", "status": "open"})
    ev = uuid4()
    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=5)
    r = act(conn, "close_port", {"port": p, "reason": "strike", "duration_h": 4, "event_id": ev}, t1)
    assert r.created["Event"] == ev
    assert bt.get_object(conn, "Port", p, as_of_valid=t1)["status"] == "closed"
    e = bt.get_object(conn, "Event", ev, as_of_valid=t1)
    assert e["kind"] == "port_closure" and e["severity"] == "high" and e["started"] == t1
    assert "strike" in e["note"] and e["ended"] is None
    assert bt.neighbours(conn, "affects", ev, "out", as_of_valid=t1) == [p]
    assert rows_with_action(conn, r.action_id) == 3  # port, event, link
    r2 = act(conn, "reopen_port", {"port": p}, t2)
    assert bt.get_object(conn, "Port", p, as_of_valid=t2)["status"] == "open"
    assert bt.get_object(conn, "Event", ev, as_of_valid=t2)["ended"] == t2
    assert bt.get_object(conn, "Event", ev, as_of_valid=t1)["ended"] is None
    a = audit(conn, r2.action_id)[0]
    assert f"Event:{ev}" in a[8] and a[9][f"Event:{ev}"]["ended"] is not None
    # reopening again touches no event
    r3 = act(conn, "reopen_port", {"port": p}, t2 + timedelta(hours=1))
    assert not r3.changed and r3.before.keys() == {f"Port:{p}"}


def test_invoice_flow(conn):
    sh, sup = mk(conn, "Shipment", {"ref": "S"}), mk(conn, "Supplier", {"name": "A"})
    inv = uuid4()
    t1 = T0 + timedelta(hours=1)
    due = T0 + timedelta(days=30)
    r = act(conn, "issue_invoice", {"shipment": sh, "supplier": sup, "amount": 99.5, "due": due,
                                    "invoice_id": inv, "ref": "INV-1"}, t1)
    i = bt.get_object(conn, "Invoice", inv, as_of_valid=t1)
    assert (i["status"], i["currency"], i["amount"], i["ref"], i["due"]) == ("open", "USD", 99.5, "INV-1", due)
    assert bt.neighbours(conn, "invoices_for", inv, as_of_valid=t1) == [sh]
    assert bt.neighbours(conn, "billed_by", inv, as_of_valid=t1) == [sup]
    assert rows_with_action(conn, r.action_id) == 3
    t2, t3 = t1 + timedelta(hours=1), t1 + timedelta(hours=2)
    act(conn, "flag_invoice", {"invoice": inv, "anomaly": "duplicate"}, t2)
    f = bt.get_object(conn, "Invoice", inv, as_of_valid=t2)
    assert f["status"] == "flagged" and f["anomaly"] == "duplicate"
    act(conn, "pay_invoice", {"invoice": inv}, t3)
    assert bt.get_object(conn, "Invoice", inv, as_of_valid=t3)["status"] == "paid"
    # generated ref + id
    r = act(conn, "issue_invoice", {"shipment": sh, "supplier": sup, "amount": 1.0}, t1)
    (new,) = [v for k, v in r.created.items() if k == "Invoice"]
    assert bt.get_object(conn, "Invoice", new, as_of_valid=t1)["ref"].startswith("INV-")
    assert len(audit(conn)) >= 6


def test_failed_action_rolls_back_everything(conn):
    sh = mk(conn, "Shipment", {"ref": "S"})
    n_audit = len(audit(conn))
    n_out = conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]
    ghost = uuid4()
    # the invoice object is written, then the supplier check fails -> all undone
    with pytest.raises(ValueError):
        act(conn, "issue_invoice", {"shipment": sh, "supplier": ghost, "amount": 5.0})
    inv = uuid4()
    # mid-action failure after writes: link to a missing port in close_port path
    with pytest.raises(ValueError):
        act(conn, "close_port", {"port": ghost}, T0)
    assert len(audit(conn)) == n_audit
    assert conn.execute("SELECT count(*) FROM obj_invoice").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM obj_event").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0] == n_out
    # DB-level failure (NOT NULL violation) after earlier writes in the same action
    with pytest.raises(Exception):
        act(conn, "create_object", {"type": "Invoice", "id": inv, "props": {"amount": 1.0}})  # ref missing
    conn.rollback()
    assert len(audit(conn)) == n_audit
    assert conn.execute("SELECT count(*) FROM obj_invoice").fetchone()[0] == 0


def test_midway_failure_after_partial_writes(conn, monkeypatch):
    import engine.actions.core as core
    p = mk(conn, "Port", {"name": "P", "status": "open"})
    n_audit = len(audit(conn))

    def boom(*a, **k):
        raise RuntimeError("boom")
    orig = core._Ctx.link
    monkeypatch.setattr(core._Ctx, "link", boom)
    with pytest.raises(RuntimeError):
        act(conn, "close_port", {"port": p}, T0 + timedelta(hours=1))
    monkeypatch.setattr(core._Ctx, "link", orig)
    assert len(audit(conn)) == n_audit
    assert bt.get_object(conn, "Port", p, as_of_valid=T0 + timedelta(hours=2))["status"] == "open"
    assert conn.execute("SELECT count(*) FROM obj_event").fetchone()[0] == 0


@pytest.mark.parametrize("name,params", [
    ("nope", {}),
    ("ship_depart", {}),                                   # missing required
    ("ship_depart", {"shipment": str(uuid4()), "x": 1}),   # unknown param
    ("ship_depart", {"shipment": "not-a-uuid"}),
    ("ship_position", {"shipment": str(uuid4()), "position": {"type": "LineString", "coordinates": []}, "progress": 1}),
    ("ship_position", {"shipment": str(uuid4()), "position": P1, "progress": "x"}),
    ("create_object", {"type": "Nope", "props": {}}),
    ("create_object", {"type": "Supplier", "props": "[1]"}),
    ("create_object", {"type": "Supplier", "props": "{bad"}),
    ("create_object", {"type": "Supplier", "props": {"bogus": 1}}),
    ("create_link", {"link": "nope", "src": str(uuid4()), "dst": str(uuid4())}),
    ("ship_depart", {"shipment": str(uuid4())}),           # missing object
])
def test_validation_errors(conn, name, params):
    n = len(audit(conn))
    with pytest.raises(ValueError):
        act(conn, name, params)
    conn.rollback()
    assert len(audit(conn)) == n


def test_create_existing_rejected(conn):
    s = mk(conn, "Supplier", {"name": "A"})
    with pytest.raises(ValueError):
        act(conn, "create_object", {"type": "Supplier", "id": s, "props": {"name": "B"}})


def test_string_params_coerced(conn):
    s = mk(conn, "Shipment", {"ref": "S"})
    r = act(conn, "ship_position", {"shipment": str(s), "position": json.dumps(P1), "progress": 1},
            T0 + timedelta(hours=5))
    assert r.changed
    assert bt.get_object(conn, "Shipment", s, as_of_valid=T0 + timedelta(hours=6))["progress"] == 1.0


def test_ontology_arg_variants(conn):
    from engine.compiler import compile_ontology
    from engine.actions.core import DEFAULT_ONTOLOGY
    s = mk(conn, "Shipment", {"ref": "S"})
    c = compile_ontology(DEFAULT_ONTOLOGY)
    assert act(conn, "ship_depart", {"shipment": s}, T0 + timedelta(hours=1), ontology=c).changed
    assert act(conn, "ship_arrive", {"shipment": s}, T0 + timedelta(hours=2), ontology=str(DEFAULT_ONTOLOGY)).changed


def test_audit_append_only_and_ordering(conn):
    for i in range(3):
        mk(conn, "Supplier", {"name": f"s{i}"})
    seqs = [a[0] for a in audit(conn)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert conn.execute("SELECT count(*) FROM pg_indexes WHERE tablename='audit_event' AND schemaname = current_schema() "
                        "AND indexname LIKE '%sim_time%'").fetchone()[0] == 1


def test_position_update_resumes_delayed_shipment(conn):
    s = mk(conn, "Shipment", {"ref": "S", "status": "planned"})
    t = [T0 + timedelta(hours=h) for h in (1, 2, 3)]
    act(conn, "ship_depart", {"shipment": s}, t[0])
    act(conn, "delay_shipment", {"shipment": s, "delay_hours": 6}, t[1])
    assert bt.get_object(conn, "Shipment", s, as_of_valid=t[1])["status"] == "delayed"
    act(conn, "ship_position", {"shipment": s, "position": P1, "progress": 0.5}, t[2])
    g = bt.get_object(conn, "Shipment", s, as_of_valid=t[2])
    assert g["status"] == "in_transit" and g["delay_hours"] == 6
