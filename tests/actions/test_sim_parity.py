"""The Phase 3 engine must not change what the simulation does: same seed, same event digest as Phase 2."""
import importlib.util
from datetime import timedelta
from pathlib import Path

from engine.db import connect
from sim.runner import SimRunner

ROOT = Path(__file__).resolve().parents[2]

# Computed on the Phase 2 commit 7c5678c (git worktree, its own engine): seed 1, 3 sim days,
# entities from tests/sim/conftest.py::seed_entities.
PHASE2_DIGEST_SEED1_3D = "7aeef41e2a2c28f383a73215794837c7ff64fc4c11543c786ea553466bb2febb"
PHASE2_ACTIONS_SEED1_3D = 111


def _sim_conftest():
    spec = importlib.util.spec_from_file_location("sim_conftest_for_parity", ROOT / "tests" / "sim" / "conftest.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_sim_digest_matches_phase2():
    sc = _sim_conftest()
    db = sc.Db()
    try:
        schema, _ = db.new()
        r = SimRunner(schema=schema, seed=1)
        r.run_until(sc.EPOCH + timedelta(days=3))
        assert r.last_error is None, r.last_error
        assert (r.event_digest(), r.actions) == (PHASE2_DIGEST_SEED1_3D, PHASE2_ACTIONS_SEED1_3D)
        r.close()
        c = connect(schema=schema)
        try:
            rows = c.execute("SELECT DISTINCT role, actor, status FROM audit_event WHERE source='sim'").fetchall()
            assert rows == [("sim", "sim", "executed")]
            assert c.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM execution_diff").fetchone()[0] > 100
        finally:
            c.close()
    finally:
        db.close()
