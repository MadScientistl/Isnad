"""Fixtures for the generalisation suite: a non-supply-chain ontology (library) served by the real API in
scratch schemas. Nothing here seeds supply-chain data."""
import re
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent import MockModel  # noqa: E402
from engine.api.main import create_app  # noqa: E402
from engine.api.seed import apply_schema  # noqa: E402
from engine.compiler import compile_ontology  # noqa: E402
from engine.db import connect, get_dsn  # noqa: E402

LIB = ROOT / "tests" / "generic" / "fixtures" / "library.yaml"


def H(role, scn=None):
    h = {"x-role": role}
    if scn:
        h["x-scenario"] = str(scn)
    return h


class Lib:
    """A running library API on its own schema (+ scripted agent model factory)."""

    def __init__(self, path: Path, schema: str):
        self.path, self.schema = path, schema
        self.comp = compile_ontology(path)
        self.scripts = []
        self.app = create_app(str(path), schema=schema, agent_model_factory=self._factory)
        self.c = TestClient(self.app)

    def _factory(self, _p):
        return MockModel(self.scripts.pop(0))

    def post(self, path, role="librarian", **kw):
        return self.c.post(path, headers=H(role, kw.pop("scn", None)), **kw)

    def get(self, path, role="librarian", **kw):
        return self.c.get(path, headers=H(role, kw.pop("scn", None)), **kw)

    def make(self, type_, role="librarian", **props):
        r = self.post(f"/objects/{type_}", role, json=props)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def wait_run(self, rid, role="librarian", timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            j = self.get(f"/agent/runs/{rid}", role).json()
            if j["run"]["status"] != "running":
                return j
            time.sleep(0.05)
        raise AssertionError("agent run did not finish")


@pytest.fixture(scope="session")
def _admin_conn():
    try:
        c = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    yield c
    c.close()


@pytest.fixture(scope="session")
def make_lib(_admin_conn, tmp_path_factory):
    created = []

    def make(with_kpis: bool = True) -> Lib:
        path = LIB
        if not with_kpis:  # same ontology without the `kpis:` block
            text = re.sub(r"(?ms)^kpis:.*?(?=^classes:)", "", LIB.read_text())
            assert "kpis:" not in text
            path = tmp_path_factory.mktemp("lib") / "library_nokpi.yaml"
            path.write_text(text)
        name = f"gen_{uuid.uuid4().hex[:10]}"
        _admin_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        created.append(name)
        c = connect(schema=name)
        apply_schema(c, compile_ontology(path).ddl())
        c.close()
        return Lib(path, name)

    yield make
    for name in created:
        _admin_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def lib(make_lib):
    return make_lib(True)


@pytest.fixture(scope="module")
def lib0(make_lib):
    """Library without declared KPIs."""
    return make_lib(False)
