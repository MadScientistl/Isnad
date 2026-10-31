"""create_app wiring: mount gating (M3), health flags (M5), and the model factory receives the principal."""
import logging
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from engine.api.main import create_app
from engine.db import get_dsn
from engine.db.setup import apply_schema

ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY = str(ROOT / "ontology" / "supply_chain.yaml")
ZONES = (ROOT / "evals" / "ontogen" / "datasets" / "bikeshare" / "zones.csv").read_bytes()
ADMIN = {"x-role": "admin"}


@pytest.fixture
def aschema(monkeypatch, tmp_path):
    name = f"mnt_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    monkeypatch.delenv("ISNAD_AUTH", raising=False)
    monkeypatch.delenv("ISNAD_DEFAULT_ROLE", raising=False)
    monkeypatch.setenv("ISNAD_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ISNAD_WORKSPACES_DIR", str(tmp_path / "ws"))
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


def client(schema, **kw):
    return TestClient(create_app(ONTOLOGY, schema=schema, pipeline=False, **kw))


def test_workspace_process_exposes_neither_ingest_nor_workspaces(aschema, monkeypatch):
    monkeypatch.setenv("ISNAD_ALLOW_IMPORT", "0")
    c = client(aschema)
    for path in ("/ingest/sessions", "/ingest/sources", "/workspaces"):
        assert c.get(path, headers=ADMIN).status_code == 404, path
    h = c.get("/health").json()
    assert h["import_enabled"] is False and h["auth"] is False


def test_import_enabled_without_auth_warns_and_reports(aschema, monkeypatch, caplog):
    monkeypatch.setenv("ISNAD_ALLOW_IMPORT", "1")
    with caplog.at_level(logging.WARNING, logger="isnad.api"):
        c = client(aschema)
    assert any("ISNAD_AUTH off" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    h = c.get("/health").json()
    assert h["import_enabled"] is True and h["auth"] is False
    assert c.get("/ingest/sessions", headers=ADMIN).status_code == 200
    assert c.get("/workspaces", headers=ADMIN).status_code == 200
    assert c.get("/ingest/sessions", headers={"x-role": "planner"}).status_code == 404


def test_model_factory_is_called_with_the_principal_through_create_app(aschema, monkeypatch):
    monkeypatch.setenv("ISNAD_ALLOW_IMPORT", "1")
    seen = []

    def factory(principal):
        seen.append(principal)
        raise RuntimeError("stub: no model")           # a configured factory that cannot serve -> 503, not a TypeError
    c = client(aschema, agent_model_factory=factory)
    sid = c.post("/ingest/uploads", params={"filename": "zones.csv"}, content=ZONES, headers=ADMIN).json()["session_id"]
    r = c.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"}, headers=ADMIN)
    assert r.status_code == 503 and r.json()["code"] == "agent_unavailable"
    assert len(seen) == 1 and seen[0].role == "admin" and hasattr(seen[0], "user")


def test_working_model_factory_starts_a_job_through_create_app(aschema, monkeypatch):
    from agent.types import ModelTurn
    from engine.ingest.profile import baseline_proposal
    monkeypatch.setenv("ISNAD_ALLOW_IMPORT", "1")
    calls = []

    class M:
        name = "stub"

        def complete(self, *, system, messages, tools, max_tokens, on_text=None, timeout=None):
            from agent.types import ToolUse
            prof = holder["profile"]
            tu = ToolUse("tu1", "submit_proposal", {"proposal": baseline_proposal(prof)})
            return ModelTurn(content=[{"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}],
                             tool_uses=[tu], stop_reason="tool_use", usage={"input_tokens": 1, "output_tokens": 1})

    def factory(principal):
        calls.append(principal.role)
        return M()
    holder = {}
    c = client(aschema, agent_model_factory=factory)
    sid = c.post("/ingest/uploads", params={"filename": "zones.csv"}, content=ZONES, headers=ADMIN).json()["session_id"]
    holder["profile"] = c.get(f"/ingest/sessions/{sid}", headers=ADMIN).json()["profile"]
    r = c.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "model"}, headers=ADMIN)
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    for _ in range(300):
        j = c.get(f"/ingest/jobs/{jid}", headers=ADMIN).json()
        if j["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert j["status"] == "done" and calls == ["admin"]
