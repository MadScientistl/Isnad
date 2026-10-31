"""Scratch-schema lifecycle, host isolation, resource limits."""
import time

import psycopg
import pytest
from psycopg import sql

from engine.db import connect
from engine.ingest import materialize as M
from engine.ingest import validate as V

CUST = "id,name\n" + "".join(f"C{i},N{i}\n" for i in range(20))
ORD = "id,customer_id\n" + "".join(f"O{i},C{i % 20}\n" for i in range(50))


@pytest.fixture
def ds(tmp_path, env):
    env.write_csvs(tmp_path, {"customers.csv": CUST, "orders.csv": ORD})
    src = env.open(tmp_path)
    prof = env.profile(src)
    return src, prof, env.baseline(prof)


def host_snapshot(db):
    """Tables and row counts of the host schemas this process owns the search path for (public, and the default
    search_path schema). Other schemas belong to concurrent tests and are ignored."""
    rows = db.execute("SELECT table_schema, table_name FROM information_schema.tables "
                      "WHERE table_schema = 'public' ORDER BY 1,2").fetchall()
    db.rollback()
    return rows


def test_schema_dropped_after_success(ds, db, no_scratch_left):
    src, prof, p = ds
    rep = V.validate(prof, p, src)
    assert rep.ok and rep.stats["scratch_dropped"] is True and len(no_scratch_left) == 1


def test_schema_dropped_after_exception_in_loading(ds, db, no_scratch_left, monkeypatch):
    src, prof, p = ds

    def boom(*a, **k):
        raise RuntimeError("boom")
        yield
    monkeypatch.setattr(M, "iter_objects", boom)
    rep = V.validate(prof, p, src)                      # does not raise
    assert not rep.ok and rep.check("internal_error") and len(no_scratch_left) == 1


def test_schema_dropped_after_reasoner_exception(ds, db, no_scratch_left, monkeypatch):
    src, prof, p = ds
    import engine.reason as R
    monkeypatch.setattr(R, "validate_changed", lambda *a, **k: (_ for _ in ()).throw(MemoryError("x")))
    rep = V.validate(prof, p, src)
    assert not rep.ok and len(no_scratch_left) == 1


def test_time_budget_stops_and_cleans_up(ds, db, no_scratch_left, monkeypatch):
    src, prof, p = ds
    monkeypatch.setenv("ISNAD_INGEST_VALIDATE_BUDGET_S", "0.0001")
    rep = V.validate(prof, p, src)
    assert not rep.ok and rep.check("time_budget") and rep.seconds < 30
    assert len(no_scratch_left) <= 1


def test_statement_timeout_is_applied_to_the_scratch_connection(ds, db, no_scratch_left, monkeypatch):
    src, prof, p = ds
    seen = []
    real = V._create_scratch

    def spy(dsn):
        name, conn = real(dsn)
        seen.append(conn)
        return name, conn
    monkeypatch.setattr(V, "_create_scratch", spy)
    monkeypatch.setenv("ISNAD_INGEST_STATEMENT_TIMEOUT_MS", "12345")
    orig = V._load_and_check

    def probe(comp, mapping, source, caps, seed, conn, *a, **k):
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "12345ms"
        return orig(comp, mapping, source, caps, seed, conn, *a, **k)
    monkeypatch.setattr(V, "_load_and_check", probe)
    assert V.validate(prof, p, src).ok


def test_database_error_during_load_is_a_failed_check(ds, db, no_scratch_left, monkeypatch):
    src, prof, p = ds
    from engine.db import bitemporal as bt
    monkeypatch.setattr(bt, "bulk_load_objects", lambda *a, **k: (_ for _ in ()).throw(psycopg.errors.CheckViolation("nope")))
    rep = V.validate(prof, p, src)
    assert not rep.ok and rep.check("load_ok") and not rep.check("load_ok")["ok"]


def test_host_schema_untouched(ds, db):
    before = host_snapshot(db)
    src, prof, p = ds
    for _ in range(2):
        V.validate(prof, p, src)
    V.validate(prof, {"junk": 1}, src)
    assert host_snapshot(db) == before
    assert db.execute("SHOW search_path").fetchone()[0] == connect().execute("SHOW search_path").fetchone()[0]


def test_scratch_names_are_server_generated(ds, db, no_scratch_left):
    src, prof, p = ds
    V.validate(prof, p, src, slug='x"; DROP SCHEMA public; --', namespace="https://isnad.local/ws/evil#")
    assert all(V.SCRATCH_RE.fullmatch(n) for n in no_scratch_left)
    assert V._drop("public") == "refused: not a scratch schema name"
    assert V._drop('gen_abcd"; --') == "refused: not a scratch schema name"


def test_sweep_drops_only_old_stamped_gen_schemas(db):
    admin = connect()
    admin.autocommit = True
    old, fresh, nostamp, other = "gen_aaaaaaa1", "gen_aaaaaaa2", "gen_aaaaaaa3", "ing_keepme01"
    try:
        for n, stamp in ((old, time.time() - 7200), (fresh, time.time()), (nostamp, None), (other, time.time() - 9999)):
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(n)))
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(n)))
            if stamp is not None:
                admin.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(sql.Identifier(n), sql.Literal(f"isnad-scratch {stamp:.0f}")))
        dropped = V.sweep_stale_scratch(admin, max_age_s=3600)
        assert old in dropped and fresh not in dropped and nostamp not in dropped and other not in dropped
        left = {r[0] for r in admin.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                                            [[old, fresh, nostamp, other]]).fetchall()}
        assert left == {fresh, nostamp, other}
    finally:
        for n in (old, fresh, nostamp, other):
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(n)))
        admin.close()


def test_caps_respected_rows_per_type(tmp_path, env, no_scratch_left):
    big = "id,name\n" + "".join(f"C{i},N{i}\n" for i in range(500))
    ords = "id,customer_id\n" + "".join(f"O{i},C{i % 500}\n" for i in range(1500))
    env.write_csvs(tmp_path, {"customers.csv": big, "orders.csv": ords})
    src = env.open(tmp_path)
    prof = env.profile(src)
    rep = V.validate(prof, env.baseline(prof), src, caps={"max_sample": 60})
    assert rep.stats["rows_loaded"]["Orders"] == 60 and rep.stats["sample_cap"] == 60
    assert rep.stats["rows_loaded"]["Customers"] <= 120
    assert rep.ok
