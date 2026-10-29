import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from engine.db import connect, get_dsn

FIXTURE = Path(__file__).parent / "fixtures" / "schema.sql"


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
    admin.execute(FIXTURE.read_text())
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(schema):
    c = connect(schema=schema)
    yield c
    c.rollback()
    c.execute("TRUNCATE obj_supplier, link_supplies, graph_outbox")
    c.commit()
    c.close()
