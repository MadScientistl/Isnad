"""Permission, RLS, audit, atomicity and diff behaviour of `execute` (all as isnad_app via scoped)."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from act_helpers import ADMIN, FIN, LEAD, PLANNER, SIM, T0
from engine.actions import (ApprovalRequired, CriteriaFailed, NotFound, PermissionDenied, dry_run,
                            execute)
from engine.db import bitemporal as bt
from engine.db import current_principal

T1 = T0 + timedelta(hours=1)


def denied_rows(conn):
    return conn.execute("SELECT name, actor, role, status, source, after FROM audit_event "
                        "WHERE status='denied' ORDER BY seq").fetchall()


def n_exec(conn):
    return conn.execute("SELECT count(*) FROM audit_event WHERE status='executed'").fetchone()[0]


def test_runs_as_isnad_app_with_identity(conn, world, monkeypatch):
    import engine.actions.core as core
    seen = {}

    def spy(c, p):
        seen["user"] = c.conn.execute("SELECT current_user").fetchone()[0]
        seen["pr"] = current_principal(c.conn)
        core._reopen_port(c, p)
    monkeypatch.setitem(core.HANDLERS, "reopen_port", lambda c, p: (spy(c, p), None)[1])
    execute(conn, "reopen_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    assert seen["user"] == "isnad_app" and seen["pr"].role == "planner" and seen["pr"].user == "pat"
    # identity is gone afterwards (same connection, same session)
    assert conn.execute("SELECT current_user").fetchone()[0] != "isnad_app"
    assert current_principal(conn) is None


def test_same_action_allowed_for_one_role_denied_for_other(conn, world):
    p = world["emea_port"]
    r = execute(conn, "close_port", {"port": p, "reason": "strike"}, PLANNER, valid_at=T1)
    assert r.changed
    with pytest.raises(PermissionDenied):
        execute(conn, "close_port", {"port": world["apac_port"]}, FIN, valid_at=T1)
    # and the reverse
    r = execute(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "dup"}, FIN, valid_at=T1)
    assert r.changed
    with pytest.raises(PermissionDenied):
        execute(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "x"}, PLANNER, valid_at=T1)
    d = denied_rows(conn)
    assert [(x[0], x[1], x[2], x[3], x[4]) for x in d] == [
        ("close_port", "fiona", "finance_analyst", "denied", "api"),
        ("flag_invoice", "pat", "planner", "denied", "api")]
    assert "may not execute" in d[0][5]["error"]


def test_denied_writes_nothing_else(conn, world):
    inv = world["inv"]
    with pytest.raises(PermissionDenied):
        execute(conn, "flag_invoice", {"invoice": inv, "anomaly": "x"}, PLANNER, valid_at=T1)
    assert n_exec(conn) == 0 and len(denied_rows(conn)) == 1
    assert bt.get_object(conn, "Invoice", inv, as_of_valid=T1, masked=False)["status"] == "open"
    assert conn.execute("SELECT count(*) FROM execution_diff").fetchone()[0] == 0
    # sim source: not audited as denied
    with pytest.raises(PermissionDenied):
        execute(conn, "update_object", {"type": "Supplier", "id": world["supplier"], "props": {"name": "x"}},
                SIM, valid_at=T1, source="sim")
    assert len(denied_rows(conn)) == 1


def test_instance_level_regional_lead(conn, world):
    r = execute(conn, "close_port", {"port": world["emea_port"], "reason": "fog"}, LEAD, valid_at=T1)
    assert r.changed
    with pytest.raises(NotFound) as e1:
        execute(conn, "close_port", {"port": world["apac_port"]}, LEAD, valid_at=T1)
    with pytest.raises(NotFound) as e2:
        execute(conn, "close_port", {"port": uuid4()}, LEAD, valid_at=T1)
    # hidden looks exactly like missing (apart from the id)
    assert str(e1.value).replace(str(world["apac_port"]), "X") == str(e2.value).replace(str(e2.value).split()[1], "X")
    assert bt.get_object(conn, "Port", world["apac_port"], as_of_valid=T1, masked=False)["status"] == "open"
    # lead cannot run planner-only actions
    with pytest.raises(PermissionDenied):
        execute(conn, "ship_depart", {"shipment": world["ship"]}, LEAD, valid_at=T1)
    # nor create a port outside its region through the generic action (not even listed)
    with pytest.raises(PermissionDenied):
        execute(conn, "create_object", {"type": "Port", "props": {"name": "X", "region": "APAC"}}, LEAD,
                valid_at=T1)


def test_write_rls_denies_cross_type_write(conn, world):
    """planner may execute update_object but has no write access to Supplier? (planner: read only)."""
    with pytest.raises(PermissionDenied):
        execute(conn, "update_object", {"type": "Supplier", "id": world["supplier"], "props": {"name": "Z"}},
                PLANNER, valid_at=T1)
    # finance analyst cannot write ports through generic update
    with pytest.raises(PermissionDenied):
        execute(conn, "update_object", {"type": "Port", "id": world["emea_port"], "props": {"name": "Z"}},
                FIN, valid_at=T1)
    assert len(denied_rows(conn)) == 2


def test_type_level_write_denied_before_instance_lookup(conn, world):
    """No existence oracle: planner (no Invoice access) gets the same denial for real and random ids."""
    for i in (world["inv"], uuid4()):
        with pytest.raises(PermissionDenied):
            execute(conn, "delete_object", {"type": "Invoice", "id": i}, PLANNER, valid_at=T1)


def test_criteria_failure_messages_and_audit(conn, world):
    sh = world["ship"]
    with pytest.raises(CriteriaFailed) as e:
        execute(conn, "delay_shipment", {"shipment": sh, "delay_hours": -2}, PLANNER, valid_at=T1)
    assert [f["message"] for f in e.value.failures] == ["delay_hours must be positive"]
    execute(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "a"}, FIN, valid_at=T1)
    d = denied_rows(conn)
    assert len(d) == 1 and d[0][0] == "delay_shipment" and d[0][3] == "denied"
    assert d[0][5]["failures"][0]["message"] == "delay_hours must be positive"
    assert bt.get_object(conn, "Shipment", sh, as_of_valid=T1)["status"] == "planned"


def test_criteria_runs_for_rule_actions_and_pay_requires_open(conn, world):
    from act_helpers import owner_obj
    paid = owner_obj(conn, "Invoice", {"ref": "P", "amount": 1.0, "status": "paid"})
    with pytest.raises(CriteriaFailed) as e:
        execute(conn, "flag_invoice", {"invoice": paid, "anomaly": "x"}, FIN, valid_at=T1)
    assert e.value.failures[0]["message"] == "Invoice is already paid"
    with pytest.raises(CriteriaFailed) as e:  # sim is approval-exempt but criteria still run
        execute(conn, "pay_invoice", {"invoice": paid}, SIM, valid_at=T1, source="sim")
    assert e.value.failures[0]["message"] == "Only open invoices can be paid"


def test_approval_gate_blocks_direct_execute(conn, world):
    with pytest.raises(ApprovalRequired) as e:
        execute(conn, "pay_invoice", {"invoice": world["inv"]}, FIN, valid_at=T1)
    assert e.value.action == "pay_invoice" and "admin" in e.value.approvers
    with pytest.raises(ApprovalRequired):  # no admin bypass either
        execute(conn, "pay_invoice", {"invoice": world["inv"]}, ADMIN, valid_at=T1)
    assert bt.get_object(conn, "Invoice", world["inv"], as_of_valid=T1, masked=False)["status"] == "open"
    r = execute(conn, "pay_invoice", {"invoice": world["inv"]}, SIM, valid_at=T1, source="sim")
    assert r.changed  # approval.exempt: [sim]


def test_atomicity_rule_failure_midway(conn, world, monkeypatch):
    """close_port writes port, event, then fails on the link: nothing remains, no executed audit row."""
    import engine.actions.core as core
    monkeypatch.setattr(core._Ctx, "link", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    n0 = conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]
    with pytest.raises(RuntimeError):
        execute(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    monkeypatch.undo()
    conn.rollback()
    assert n_exec(conn) == 0
    assert conn.execute("SELECT count(*) FROM execution_diff").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0] == n0
    assert bt.get_object(conn, "Port", world["emea_port"], as_of_valid=T1)["status"] == "open"
    assert conn.execute("SELECT count(*) FROM obj_event").fetchone()[0] == 0


def test_diff_completeness_and_content(conn, world):
    r = execute(conn, "close_port", {"port": world["emea_port"], "reason": "r"}, PLANNER, valid_at=T1)
    rows = conn.execute("SELECT kind, type, key, op, before, after FROM execution_diff WHERE action_id=%s",
                        [r.action_id]).fetchall()
    assert {(k, t, o) for k, t, _, o, _, _ in rows} == {("object", "Port", "modify"), ("object", "Event", "create"),
                                                       ("link", "affects", "create")}
    port = next(x for x in rows if x[1] == "Port")
    assert port[4]["status"] == "open" and port[5]["status"] == "closed"
    # every changed row in obj_/link_ tables carries an action_id that exists in audit_event
    for (t,) in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
                             "AND (table_name LIKE 'obj\\_%' OR table_name LIKE 'link\\_%') "
                             "AND table_type='BASE TABLE'").fetchall():
        orphans = conn.execute(f"SELECT count(*) FROM {t} WHERE action_id IS NOT NULL AND action_id NOT IN "
                               "(SELECT action_id FROM audit_event)").fetchone()[0]
        assert orphans == 0, t
    # each (type,key) touched by the action has a diff row; count rows with this action_id
    touched = 0
    for t in ("obj_port", "obj_event", "link_affects"):
        touched += conn.execute(f"SELECT count(DISTINCT {'id' if t.startswith('obj') else 'src, dst'}) FROM {t} "
                                "WHERE action_id=%s" if t.startswith("obj") else
                                f"SELECT count(*) FROM (SELECT DISTINCT src, dst FROM {t} WHERE action_id=%s) q",
                                [r.action_id]).fetchone()[0]
    assert touched == len(rows) == 3


def test_webhooks_enqueued_atomically_and_skipped_for_sim(conn, world):
    r = execute(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    assert r.webhooks == ["notify_ops"]
    (oid, key, url) = conn.execute("SELECT id, idempotency_key, url FROM webhook_outbox WHERE action_id=%s",
                                   [r.action_id]).fetchone()
    assert key == f"{r.action_id}:notify_ops"
    st = conn.execute("SELECT status, attempt FROM webhook_state WHERE outbox_id=%s", [oid]).fetchone()
    assert st == ("pending", 0)
    r2 = execute(conn, "reopen_port", {"port": world["emea_port"]}, SIM, valid_at=T1 + timedelta(hours=1),
                 source="sim")
    r3 = execute(conn, "close_port", {"port": world["emea_port"]}, SIM, valid_at=T1 + timedelta(hours=2),
                 source="sim")
    assert r3.webhooks == [] and r2.webhooks == []
    assert conn.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0] == 1


def test_dry_run_returns_diff_commits_nothing(conn, world):
    n0 = conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]
    r = dry_run(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    assert r.status == "dry_run" and r.changed and len(r.diff) == 3
    assert bt.get_object(conn, "Port", world["emea_port"], as_of_valid=T1)["status"] == "open"
    assert conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0] == n0
    assert conn.execute("SELECT count(*) FROM obj_event").fetchone()[0] == 0
    with pytest.raises(PermissionDenied):
        dry_run(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "x"}, PLANNER, valid_at=T1)
    # approval-gated actions can be dry-run (that is how a proposal previews)
    assert dry_run(conn, "pay_invoice", {"invoice": world["inv"]}, FIN, valid_at=T1).diff[0]["after"]["status"] == "paid"


def test_tool_trace_and_run_id_recorded(conn, world):
    tr = [{"tool": "get_port", "args": {"id": "x"}}]
    r = execute(conn, "reopen_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1, source="agent",
                run_id="run-9", tool_trace=tr)
    row = conn.execute("SELECT tool_trace, run_id, source, role, actor, on_behalf_of FROM audit_event "
                       "WHERE action_id=%s", [r.action_id]).fetchone()
    assert row == (tr, "run-9", "agent", "planner", "pat", None)


def test_audit_is_append_only_even_for_owner(conn, world):
    execute(conn, "reopen_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    import psycopg
    for stmt in ("UPDATE audit_event SET name='x'", "DELETE FROM audit_event", "TRUNCATE audit_event"):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute(stmt)
        conn.rollback()


def test_unknown_role_has_no_permissions(conn, world):
    from engine.db import Principal
    with pytest.raises(PermissionDenied):
        execute(conn, "reopen_port", {"port": world["emea_port"]}, Principal("ghost", "g", ""), valid_at=T1)
