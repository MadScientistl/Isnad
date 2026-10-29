import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
import uuid

import psycopg
import pytest
from psycopg import sql

from engine.db import connect, get_dsn
from engine.db.setup import GOVERNANCE_SQL

BUILD = _P(__file__).resolve().parents[2] / "build" / "schema.sql"


def _make(prefix, full):
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    admin.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    if full:
        admin.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        admin.execute(GOVERNANCE_SQL.read_text())
        admin.execute(BUILD.read_text())
    else:
        admin.execute(GOVERNANCE_SQL.read_text())
    return admin, name


@pytest.fixture(scope="session")
def gov_schema():
    admin, name = _make("gv", False)
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture(scope="session")
def full_schema():
    admin, name = _make("gf", True)
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(gov_schema):
    c = connect(schema=gov_schema)
    yield c
    c.rollback()
    c.close()
