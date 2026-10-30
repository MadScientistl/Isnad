import json
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
DATASETS = ROOT / "evals" / "ontogen" / "datasets"
CASSETTES = ROOT / "evals" / "ontogen" / "cassettes"


@pytest.fixture(scope="session")
def db():
    from engine.db import connect
    try:
        c = connect()
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    yield c
    c.close()


@pytest.fixture
def no_scratch_left(db, monkeypatch):
    """Every scratch schema created by validate() during the test must be gone afterwards (only this test's schemas
    are checked: other processes may validate concurrently)."""
    from engine.ingest import validate as V
    made = []
    real = V._create_scratch

    def spy(*a, **k):
        name, conn = real(*a, **k)
        made.append(name)
        return name, conn
    monkeypatch.setattr(V, "_create_scratch", spy)
    yield made
    left = set(V.scratch_schemas(db)) & set(made)
    assert left == set(), left


class Env:
    """Helpers: profile a directory, baseline proposal as dict, column id lookup, cassette proposals."""

    @staticmethod
    def open(path):
        from engine.ingest.sources import open_source
        return open_source("dir", path)

    @staticmethod
    def profile(src):
        from engine.ingest.profile import profile_source
        return profile_source(src)

    @staticmethod
    def baseline(profile):
        from engine.ingest.profile import baseline_proposal
        return json.loads(json.dumps(baseline_proposal(profile)))

    @staticmethod
    def col(profile, table, column):
        for t in profile["tables"]:
            if t["name"] == table:
                for c in t["columns"]:
                    if c["name"] == column:
                        return c["id"]
        raise KeyError((table, column))

    @staticmethod
    def cassette(name, profile):
        from agent.ontogen.cassette import Resolver
        data = json.loads((CASSETTES / f"{name}.json").read_text())
        r = Resolver(dict(profile), profile["profile_hash"])
        uses = [u for t in data["turns"] for u in t.get("tool_uses", []) if u["name"] == "submit_proposal"]
        return r.walk(uses[-1]["input"]["proposal"])

    @staticmethod
    def write_csvs(tmp_path, files: dict):
        for n, text in files.items():
            (tmp_path / n).write_text(text)
        return tmp_path


@pytest.fixture
def env():
    return Env
