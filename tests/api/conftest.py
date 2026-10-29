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


@pytest.fixture(scope="session")
def schema():
    name = f"api_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    conn = connect(schema=name)
    seed(conn)
    conn.close()
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture(scope="session")
def client(schema):
    return TestClient(create_app(str(ROOT / "ontology/supply_chain.yaml"), schema=schema))
