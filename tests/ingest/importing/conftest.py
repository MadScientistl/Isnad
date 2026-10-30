"""Fixtures for the import path: a standalone FastAPI app around `ingest_routes.make_router` with a fake principal."""
import io
import sys
import uuid
import zipfile
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from engine.api import ingest_routes  # noqa: E402
from engine.db import Principal, connect, get_dsn  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402
from engine.ingest import workspaces as W  # noqa: E402

BIKE = ROOT / "evals" / "ontogen" / "datasets" / "bikeshare"
CSV_NAMES = ["bikes", "clusters", "maintenance", "riders", "stations", "trips", "zones"]


def bike_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in CSV_NAMES:
            z.write(BIKE / f"{n}.csv", f"{n}.csv")
    return buf.getvalue()


def csv_rows(name: str) -> int:
    return sum(1 for _ in open(BIKE / f"{name}.csv", newline="")) - 1


@pytest.fixture(scope="session")
def host_schema():
    name = f"ing_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


class Harness:
    def __init__(self, host_schema, tmp_path, monkeypatch, **router_kw):
        self.host_schema, self.tmp = host_schema, tmp_path
        self.ingest, self.ws = tmp_path / "ingest", tmp_path / "workspaces"
        self.user = f"adm-{uuid.uuid4().hex[:8]}"
        monkeypatch.setenv("ISNAD_ALLOW_IMPORT", "1")
        monkeypatch.delenv("ISNAD_AUTH", raising=False)
        monkeypatch.setenv("ISNAD_AGENT_RUNS_PER_MIN", "100")
        monkeypatch.setenv("ISNAD_AGENT_MAX_CONCURRENT", "3")
        monkeypatch.setenv("ISNAD_INGEST_SESSIONS_PER_USER", "100")
        monkeypatch.setenv("ISNAD_INGEST_DIR", str(self.ingest))
        monkeypatch.setenv("ISNAD_WORKSPACES_DIR", str(self.ws))
        self.app = FastAPI()

        def get_principal(x_role: str = Header(default="admin"), x_user: str = Header(default="")):
            return Principal(role=x_role, user=x_user or self.user)

        def wconn():
            c = connect(schema=host_schema)
            try:
                yield c
            finally:
                c.close()

        kw = dict(ontology=None, schema_name=host_schema, ingest_root=self.ingest, workspaces_root=self.ws)
        kw.update(router_kw)
        self.router = ingest_routes.make_router(get_principal, wconn, **kw)
        self.app.include_router(self.router)
        self.client = TestClient(self.app)

    def conn(self):
        return connect(schema=self.host_schema)

    def sessions(self):
        c = self.conn()
        try:
            return c.execute("SELECT id, source_name FROM ingest_session WHERE created_by = %s", (self.user,)).fetchall()
        finally:
            c.close()

    def schemas(self):
        return [W.schema_for(W.session_slug(n, i)) for i, n in self.sessions()]

    def cleanup(self):
        admin = psycopg.connect(get_dsn(), autocommit=True)
        try:
            for s in self.schemas():
                admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(s)))
        finally:
            admin.close()

    # ---- flow helpers
    def upload(self, data: bytes, filename: str, **kw):
        return self.client.post("/ingest/uploads", params={"filename": filename}, content=data, **kw)

    def baseline(self, sid: str) -> str:
        r = self.client.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "baseline"})
        assert r.status_code == 202, r.text
        return r.json()["proposal_id"]

    def approve(self, pid: str, comment="reviewed roles and data"):
        return self.client.post(f"/ingest/proposals/{pid}/approve", json={"comment": comment})

    def wait(self, job_id: str, timeout=120):
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.client.get(f"/ingest/jobs/{job_id}").json()
            if j["status"] in ("done", "failed", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("job timeout")


@pytest.fixture
def make_harness(host_schema, tmp_path, monkeypatch):
    made = []

    def make(**kw):
        h = Harness(host_schema, tmp_path, monkeypatch, **kw)
        made.append(h)
        return h
    yield make
    for h in made:
        h.cleanup()


@pytest.fixture
def h(make_harness):
    return make_harness()
