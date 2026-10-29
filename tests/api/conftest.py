import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.api.main import create_app
from engine.api.seed import seed
from engine.db import connect, get_dsn

ONTOLOGY = str(ROOT / "ontology/supply_chain.yaml")


@pytest.fixture(scope="session")
def make_schema():
    """Factory: creates a seeded scratch schema; all are dropped at session end."""
    created = []
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")

    def make() -> str:
        name = f"api_{uuid.uuid4().hex[:10]}"
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        created.append(name)
        conn = connect(schema=name)
        seed(conn)
        conn.close()
        return name

    yield make
    for name in created:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture(scope="session")
def schema(make_schema):
    return make_schema()


@pytest.fixture(scope="session")
def client(schema):
    return TestClient(create_app(ONTOLOGY, schema=schema, pipeline=False))


@pytest.fixture()
def conn(schema):
    c = connect(schema=schema)
    yield c
    c.close()
