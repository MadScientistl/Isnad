"""Auth test fixtures: a seeded scratch schema, a users file, and an app factory with ISNAD_AUTH=1."""
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from .auth_helpers import ONTOLOGY, SECRET, write_users  # noqa: E402
from engine.api.main import create_app  # noqa: E402
from engine.api.seed import seed  # noqa: E402
from engine.db import connect, get_dsn  # noqa: E402


@pytest.fixture(scope="session")
def aschema():
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    name = f"auth_{uuid.uuid4().hex[:10]}"
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    c = connect(schema=name)
    seed(c)
    c.close()
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def users_file(tmp_path):
    p = tmp_path / "users.json"
    write_users(p)
    return p


@pytest.fixture
def auth_env(monkeypatch, users_file):
    monkeypatch.setenv("ISNAD_AUTH", "1")
    monkeypatch.setenv("ISNAD_AUTH_SECRET", SECRET)
    monkeypatch.setenv("ISNAD_USERS_FILE", str(users_file))
    monkeypatch.delenv("ISNAD_DEFAULT_ROLE", raising=False)
    for k in ("ISNAD_AUTH_TTL_H", "ISNAD_AUTH_MAX_FAILS_USER", "ISNAD_AUTH_MAX_FAILS_IP", "ISNAD_AUTH_LOCK_BASE_S"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture
def make_client(aschema, auth_env):
    def make(**kw):
        c = TestClient(create_app(ONTOLOGY, schema=aschema, pipeline=False, **kw))
        c.app.state.schema_name = aschema
        return c
    return make


@pytest.fixture
def client(make_client):
    return make_client()
