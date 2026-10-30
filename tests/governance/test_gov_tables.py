import uuid
from pathlib import Path

import psycopg
import pytest

from engine.db import OWNER, Principal, apply_schema, current_principal, scoped
from engine.db.setup import GOVERNANCE_SQL

ALICE = Principal("planner", "alice", "EMEA")
BOB = Principal("finance_analyst", "bob", "")
ADMIN = Principal("admin", "root", "")
TABLES = ["audit_event", "proposal", "approval", "execution_diff", "rollback",
          "webhook_outbox", "webhook_delivery"]


def ins_audit(conn, actor, role="planner", **kw):
    aid = uuid.uuid4()
    conn.execute("INSERT INTO audit_event (action_id,name,actor,source,role) VALUES (%s,'x',%s,'api',%s)",
                 [aid, actor, role])
    return aid


def count(conn, t):
    return conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]


def test_ddl_idempotent(conn):
    conn.execute(GOVERNANCE_SQL.read_text())
    conn.execute(GOVERNANCE_SQL.read_text())
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='audit_event' "
        "AND table_schema=current_schema()")}
    assert {"role", "on_behalf_of", "proposal_id", "status", "tool_trace", "parent_execution_id"} <= cols


@pytest.mark.parametrize("who", ["owner", "app"])
def test_append_only(conn, who):
    aid = ins_audit(conn, "alice")
    ctx = scoped(conn, ADMIN if who == "app" else OWNER)
    with ctx:
        if who == "app":
            conn.execute("INSERT INTO audit_event (action_id,name,actor,source) VALUES (%s,'y','root','api')", [aid])
        for stmt in ["UPDATE audit_event SET name='z'", "DELETE FROM audit_event", "TRUNCATE audit_event"]:
            conn.execute("SAVEPOINT s")
            with pytest.raises(psycopg.Error):
                conn.execute(stmt)
            conn.execute("ROLLBACK TO s")
    assert count(conn, "audit_event") >= 1


@pytest.mark.parametrize("t", TABLES)
def test_all_tables_have_triggers(conn, t):
    n = conn.execute("SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal "
                     "AND tgname IN ('isnad_no_update', 'isnad_no_truncate')", [t]).fetchone()[0]
    assert n == 2   # (the outbox tables carry one more trigger: isnad_no_scn, see the review-fix tests)
    assert conn.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid=%s::regclass", [t]).fetchone()[0]


def test_app_has_no_update_grants(conn):
    for t in TABLES:
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not conn.execute("SELECT has_table_privilege('isnad_app', %s, %s)", [t, priv]).fetchone()[0]


def test_rls_per_role(conn):
    with scoped(conn, ALICE):
        a = ins_audit(conn, "alice")
    with scoped(conn, BOB):
        ins_audit(conn, "bob", "finance_analyst")
    with scoped(conn, ALICE):
        assert [r[0] for r in conn.execute("SELECT actor FROM audit_event").fetchall()] == ["alice"]
    with scoped(conn, BOB):
        assert {r[0] for r in conn.execute("SELECT actor FROM audit_event")} == {"bob"}
    with scoped(conn, ADMIN):
        assert {r[0] for r in conn.execute("SELECT actor FROM audit_event")} >= {"alice", "bob"}
    # diff visible iff execution visible
    conn.execute("INSERT INTO execution_diff (action_id,kind,type,key,op) VALUES (%s,'object','T','k','create')", [a])
    with scoped(conn, ALICE):
        assert count(conn, "execution_diff") == 1
    with scoped(conn, BOB):
        assert count(conn, "execution_diff") == 0


def test_proposal_approval_visibility(conn):
    pid = uuid.uuid4()
    with scoped(conn, ALICE):
        conn.execute("INSERT INTO proposal (id,action,proposer,proposer_role) VALUES (%s,'flag','alice','planner')", [pid])
    with scoped(conn, BOB):
        assert count(conn, "proposal") == 0
    with scoped(conn, Principal("finance_analyst", "bob", "", approves=("flag",))):
        assert count(conn, "proposal") == 1
        conn.execute("INSERT INTO approval (id,proposal_id,decision,approver,approver_role) VALUES (%s,%s,'approved','bob','finance_analyst')", [uuid.uuid4(), pid])
    with scoped(conn, ALICE):  # proposer sees the decision
        assert count(conn, "approval") == 1
    with scoped(conn, Principal("x", "carol")):
        assert count(conn, "approval") == 0
    with pytest.raises(psycopg.Error):
        conn.execute(
            "INSERT INTO approval (id,proposal_id,decision,approver,approver_role) VALUES (%s,%s,'rejected','a','r')", [uuid.uuid4(), pid])


def test_unset_or_unknown_role_fails_closed(conn):
    ins_audit(conn, "alice")
    conn.execute("SET LOCAL ROLE isnad_app")
    assert count(conn, "audit_event") == 0
    with pytest.raises(psycopg.Error):
        conn.execute("INSERT INTO audit_event (action_id,name,actor,source) VALUES (gen_random_uuid(),'y','a','api')")
    conn.rollback()
    with scoped(conn, Principal("", "ghost")):
        assert count(conn, "audit_event") == 0
    conn.rollback()


def test_webhook_state_latest_row_and_skip_locked(conn):
    aid = ins_audit(conn, "alice")
    oid = uuid.uuid4()
    conn.execute("INSERT INTO webhook_outbox (id,action_id,hook,url,idempotency_key) VALUES (%s,%s,'h','u','k1')", [oid, aid])
    for st, att in (("pending", 0), ("retry", 1), ("delivered", 2)):
        conn.execute("INSERT INTO webhook_delivery (outbox_id,attempt,status) VALUES (%s,%s,%s)", [oid, att, st])
    rows = conn.execute("SELECT status FROM webhook_state WHERE outbox_id=%s", [oid]).fetchall()
    assert rows == [("delivered",)]
    conn.execute("SELECT * FROM webhook_state WHERE status IN ('pending','retry') FOR UPDATE SKIP LOCKED").fetchall()
    with scoped(conn, ALICE):
        assert conn.execute("SELECT status FROM webhook_state WHERE outbox_id=%s", [oid]).fetchall() == [("delivered",)]
    with scoped(conn, BOB):
        assert count(conn, "webhook_state") == 0


def test_scoped_sets_and_resets_identity(conn):
    assert current_principal(conn) is None
    with scoped(conn, ALICE):
        assert current_principal(conn) == ALICE
        assert conn.execute("SELECT current_user").fetchone()[0] == "isnad_app"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5s"
    assert current_principal(conn) is None
    assert conn.execute("SELECT current_user").fetchone()[0] != "isnad_app"
    with scoped(conn, OWNER):
        assert conn.execute("SELECT current_user").fetchone()[0] != "isnad_app"


def test_identity_never_leaks_across_transactions(conn):
    with scoped(conn, ALICE):
        pass
    # exit without reset on purpose: even a leaked in-flight scope dies with the transaction
    conn.execute("SET LOCAL ROLE isnad_app")
    conn.execute("SELECT set_config('isnad.role','admin',true), set_config('isnad.user','root',true)")
    conn.commit()
    assert current_principal(conn) is None
    assert conn.execute("SELECT current_user").fetchone()[0] != "isnad_app"
    for p in (ALICE, BOB, ALICE):  # pooled-style reuse
        with scoped(conn, p):
            assert current_principal(conn) == p
        conn.commit()
        assert current_principal(conn) is None


def test_scoped_requires_transaction(gov_schema):
    from engine.db import connect
    c = connect(schema=gov_schema, autocommit=True)
    with pytest.raises(RuntimeError):
        with scoped(c, ALICE):
            pass
    with c.transaction():
        with scoped(c, ALICE):
            assert current_principal(c) == ALICE
    assert current_principal(c) is None
    c.close()


def test_apply_schema_idempotent_and_drop_cascade(full_schema):
    from engine.db import connect
    c = connect(schema=full_schema)
    apply_schema(c)
    apply_schema(c)
    c.commit()
    c.close()


def test_masked_fallback_when_views_absent(gov_schema):
    from engine.db import bitemporal as bt, connect
    c = connect(schema=gov_schema)
    c.execute("CREATE TABLE obj_zz (id uuid, valid_during tstzrange, recorded_during tstzrange, "
              "changed_by text, action_id uuid, scenario_id int default 0)")
    assert bt.read_source(c, "obj_zz", True) == "obj_zz"
    c.rollback()
    c.close()


_has_rd = "rd_obj_" in (Path(__file__).resolve().parents[2] / "build" / "schema.sql").read_text()


@pytest.mark.skipif(not _has_rd, reason="compiler has not yet landed rd_obj_* views in build/schema.sql")
def test_masked_reads_use_views(full_schema):
    from engine.db import bitemporal as bt, connect
    c = connect(schema=full_schema)
    assert bt.read_source(c, "obj_carrier", True) == "rd_obj_carrier"
    assert bt.read_source(c, "obj_carrier", False) == "obj_carrier"
    assert bt.list_objects(c, "Carrier", masked=False) == []
    c.close()
