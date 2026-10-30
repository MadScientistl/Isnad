import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine import versioning  # noqa: E402
from engine.db import connect, get_dsn  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402

BASE_YAML = (ROOT / "ontology" / "supply_chain.yaml").read_text()


@pytest.fixture(autouse=True)
def _self_approve(monkeypatch):
    """The existing tests use one admin for propose AND approve; the review fix (proposer != approver) has its own
    tests that delete this variable."""
    monkeypatch.setenv("AMARH_ONTOLOGY_SELF_APPROVE", "1")


@pytest.fixture
def raw():
    return yaml.safe_load(BASE_YAML)


@pytest.fixture
def dump():
    return lambda d: yaml.safe_dump(d, sort_keys=False)


@pytest.fixture
def vschema():
    name = f"ver_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    versioning.apply(admin)
    versioning.apply(admin)  # idempotent
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(vschema):
    c = connect(schema=vschema)
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def factory(vschema):
    return lambda: connect(schema=vschema)
