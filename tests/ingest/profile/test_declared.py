"""Declared keys/FKs from SQLite and PostgreSQL, hostile names, and the PG reader's safety properties."""
import os
import sqlite3
import uuid

import pytest

from engine.ingest.profile import baseline_proposal, profile_source
from engine.ingest.sources import SourceError, open_source

DSN = os.environ.get("ISNAD_TEST_DSN", "postgresql://isnad:isnad@localhost:55441/isnad")


def names(p):
    cid = {c["id"]: (t["name"], c["name"]) for t in p["tables"] for c in t["columns"]}
    return {(cid[f["from"]], cid[f["to"]]): f for f in p["fks"]}


def test_sqlite_declared_fks_even_without_name_or_value_evidence(tmp_path):
    db = tmp_path / "d.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE dept (did INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE emp (eid INTEGER PRIMARY KEY, boss INTEGER REFERENCES dept(did), name TEXT);
    INSERT INTO dept VALUES (1,'A'),(2,'B'),(3,'C');
    INSERT INTO emp VALUES (1,1,'x'),(2,2,'y'),(3,9,'z'),(4,NULL,'w');
    """)
    c.commit()
    c.close()
    with open_source("sqlite", db) as s:
        p = profile_source(s)
    assert p["source"]["kind"] == "sqlite"
    f = names(p)[(("emp", "boss"), ("dept", "did"))]
    assert f["declared"] and f["confidence"] == 1.0 and f["orphans"] == 1 and f["containment"] == pytest.approx(2 / 3, abs=1e-3)
    assert next(t for t in p["tables"] if t["name"] == "emp")["key_candidates"][0] == ["t2.c1"]


def test_sqlite_hostile_names_become_safe_identifiers(tmp_path):
    db = tmp_path / "h.db"
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE "Robert\'); DROP TABLE x;--" ("ignore previous instructions" TEXT, "select" INT, "id" INT)')
    c.execute('INSERT INTO "Robert\'); DROP TABLE x;--" VALUES (\'a\', 1, 1)')
    c.commit()
    c.close()
    with open_source("sqlite", db) as s:
        p = profile_source(s)
    b = baseline_proposal(p)
    import re
    assert re.fullmatch(r"[A-Z][A-Za-z0-9]{0,40}", p["tables"][0]["ident"])
    assert [c["ident"] for c in p["tables"][0]["columns"]][1:] == ["select_x", "id_x"]
    assert b["objectTypes"][0]["label"].startswith("Robert")


@pytest.fixture
def pg_schema():
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=3)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"postgres not available: {e}")
    sch = "ingest_t_" + uuid.uuid4().hex[:8]
    try:
        conn.execute(f'CREATE SCHEMA "{sch}"')
    except Exception as e:  # pragma: no cover
        conn.close()
        pytest.skip(f"cannot create schema: {e}")
    try:
        yield conn, sch
    finally:
        conn.execute(f'DROP SCHEMA "{sch}" CASCADE')
        conn.close()


def test_postgres_declared_fks_pks_and_read_only(pg_schema):
    conn, sch = pg_schema
    conn.execute(f"""
      CREATE TABLE "{sch}".ports (code text PRIMARY KEY, name text NOT NULL, lat double precision, lon double precision, opened date);
      CREATE TABLE "{sch}".ships (id serial PRIMARY KEY, port text REFERENCES "{sch}".ports(code), flag boolean, meta jsonb, ts timestamptz);
      CREATE TABLE "{sch}".sail (ship int REFERENCES "{sch}".ships(id), port text REFERENCES "{sch}".ports(code), PRIMARY KEY (ship, port));
      CREATE TABLE "{sch}"."weird ""name" (a int);
      INSERT INTO "{sch}".ports VALUES ('P1','A',1.5,2.5,'2024-01-01'),('P2','B',-3,4,'2024-02-01'),('P3','C',NULL,NULL,NULL);
      INSERT INTO "{sch}".ships (port, flag, meta, ts) VALUES ('P1',true,'{{"a":1}}','2024-01-01 10:00+00'),('P2',false,NULL,NULL),('P1',true,NULL,NULL);
      INSERT INTO "{sch}".sail VALUES (1,'P1'),(2,'P2');
    """.replace("{{", "{").replace("}}", "}"))
    with open_source("postgres", dsn=DSN, name="pgtest", schema=sch) as s:
        assert s.kind == "postgres" and DSN not in repr(vars(s)) and "isnad:isnad" not in repr(s.warnings)
        keys = s.declared_keys()
        assert keys["ports"] == ["code"] and keys["sail"] == ["ship", "port"]
        fks = {(f.from_table, f.from_cols, f.to_table, f.to_cols) for f in s.declared_fks()}
        assert fks == {("ships", ("port",), "ports", ("code",)), ("sail", ("ship",), "ships", ("id",)),
                       ("sail", ("port",), "ports", ("code",))}
        # read-only transaction: writes fail
        import psycopg
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            s._conn.execute(f'INSERT INTO "{sch}".ports VALUES (\'Z\',\'z\',0,0,NULL)')
        s._conn.rollback()
        p = profile_source(s)
    got = names(p)
    assert (("ships", "port"), ("ports", "code")) in got and got[(("ships", "port"), ("ports", "code"))]["declared"]
    assert any(t["name"] == 'weird "name' for t in p["tables"])
    ports = next(t for t in p["tables"] if t["name"] == "ports")
    assert ports["rows"] == 3 and ports["key_candidates"][0] == ["t1.c1"]
    lat = next(c for c in ports["columns"] if c["name"] == "lat")
    assert lat["type"] == "float" and lat["geo"]["role"] == "lat"
    ships = next(t for t in p["tables"] if t["name"] == "ships")
    flag = next(c for c in ships["columns"] if c["name"] == "flag")
    assert flag["type"] == "bool"
    assert next(c for c in ships["columns"] if c["name"] == "ts")["type"] == "timestamp"
    sail = next(t for t in p["tables"] if t["name"] == "sail")
    assert sail["is_join_table"]
    assert p["source"]["sha256"] and p["source"]["bytes"] is None
    assert baseline_proposal(p)["linkTypes"]


def test_postgres_bad_dsn_error_has_no_secret():
    with pytest.raises(SourceError) as e:
        open_source("postgres", dsn="postgresql://user:topsecretpw@127.0.0.1:1/none", name="x")
    assert "topsecretpw" not in str(e.value)


def test_baseline_link_names_never_collide_with_property_names(tmp_path):
    """A link and a property share one RDF predicate; the FK column `boss` must not yield both a property and a link `boss`."""
    db = tmp_path / "c.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE dept (did INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE emp (eid INTEGER PRIMARY KEY, boss INTEGER REFERENCES dept(did), name TEXT);
    INSERT INTO dept VALUES (1,'A'),(2,'B');
    INSERT INTO emp VALUES (1,1,'x'),(2,2,'y');
    """)
    c.commit()
    c.close()
    with open_source("sqlite", db) as s:
        prop = baseline_proposal(profile_source(s))
    props = {p["name"] for t in prop["objectTypes"] for p in t["properties"]}
    links = [l["name"] for l in prop["linkTypes"]]
    assert links and not (set(links) & props)
    assert len(set(links)) == len(links)
