import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine.db import connect, get_dsn  # noqa: E402

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "build" / "schema.sql"


@pytest.fixture(scope="session")
def schema():
    name = f"t_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    admin.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    admin.execute(SCHEMA_SQL.read_text())
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(schema):
    c = connect(schema=schema)
    for t in ("obj_supplier", "obj_port", "obj_shipment", "obj_carrier", "obj_warehouse",
              "link_supplies", "link_ships_via", "link_operated_by", "graph_outbox"):
        c.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(t)))
    c.commit()
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def gschema():
    """Fresh (per test) schema with governance tables, scenario tables, eff_* views and RLS (Phase 5 scenario graph tests)."""
    from engine.db.setup import apply_schema
    name = f"tsg_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    admin.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()
