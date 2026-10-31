"""Ingest tables: append-only, FORCE RLS, admin-only; applied by engine.db.setup.apply_schema."""
import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine.db import Principal, connect, get_dsn  # noqa: E402
from engine.db.principal import scoped  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402

TABLES = ("ingest_session", "ingest_proposal", "ingest_decision")


@pytest.fixture
def schema():
    name = f"ing_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    apply_schema(admin)  # idempotent
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(schema):
    c = connect(schema=schema)
    yield c
    c.rollback()
    c.close()


def _session(conn, by="adam"):
    sid = uuid.uuid4()
    conn.execute("INSERT INTO ingest_session (id, created_by, source_kind, source_name, source_sha256, profile, "
                 "profile_hash) VALUES (%s,%s,'csv','x.zip','00',%s::jsonb,'h')", (sid, by, json.dumps({"a": 1})))
    return sid


def _proposal(conn, sid, seq=1, kind="baseline"):
    pid = uuid.uuid4()
    conn.execute("INSERT INTO ingest_proposal (id, session_id, seq, proposal, yaml, mapping, created_by, kind) "
                 "VALUES (%s,%s,%s,'{}'::jsonb,'y','{}'::jsonb,'adam',%s)", (pid, sid, seq, kind))
    return pid


def test_tables_exist_and_owner_roundtrip(conn):
    sid = _session(conn)
    pid = _proposal(conn, sid)
    did = uuid.uuid4()
    conn.execute("INSERT INTO ingest_decision (id, proposal_id, decision, approver, approver_role) "
                 "VALUES (%s,%s,'approved','bob','admin')", (did, pid))
    assert conn.execute("SELECT count(*) FROM ingest_decision").fetchone()[0] == 1


@pytest.mark.parametrize("t", TABLES)
def test_append_only(conn, t):
    sid = _session(conn)
    pid = _proposal(conn, sid)
    conn.execute("INSERT INTO ingest_decision (id, proposal_id, decision, approver, approver_role) "
                 "VALUES (%s,%s,'rejected','bob','admin')", (uuid.uuid4(), pid))
    for stmt in (f"UPDATE {t} SET {'created_by' if t != 'ingest_decision' else 'approver'} = 'z'", f"DELETE FROM {t}",
                 f"TRUNCATE {t}"):
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                conn.execute(stmt)


def test_check_constraints(conn):
    sid = _session(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            _proposal(conn, sid, 2, kind="bogus")
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("INSERT INTO ingest_decision (id, proposal_id, decision, approver, approver_role) "
                         "VALUES (%s,%s,'maybe','b','admin')", (uuid.uuid4(), _proposal(conn, sid, 3)))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            _proposal(conn, sid, 5)
            _proposal(conn, sid, 5)


def test_rls_forced_and_admin_only(conn):
    rows = conn.execute("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = ANY(%s) AND relnamespace = current_schema()::regnamespace",
                        (list(TABLES),)).fetchall()
    assert len(rows) == 3 and all(r[1] and r[2] for r in rows)
    sid = _session(conn)
    pid0 = _proposal(conn, sid)
    conn.commit()
    for role in ("planner", "finance_analyst", "regional_ops_lead", "sim"):
        with scoped(conn, Principal(role, "u")):
            for t in TABLES:
                assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
                with pytest.raises(psycopg.Error):
                    with conn.transaction():
                        if t == "ingest_session":
                            _session(conn, "u")
                        elif t == "ingest_proposal":
                            _proposal(conn, sid, 9)
                        else:
                            conn.execute("INSERT INTO ingest_decision (id, proposal_id, decision, approver, "
                                         "approver_role) VALUES (%s, %s, 'approved', 'u', 'x')",
                                         (uuid.uuid4(), pid0))
    with scoped(conn, Principal("admin", "adam")):
        assert conn.execute("SELECT count(*) FROM ingest_session").fetchone()[0] == 1
        _session(conn, "adam")
        assert conn.execute("SELECT count(*) FROM ingest_session").fetchone()[0] == 2
