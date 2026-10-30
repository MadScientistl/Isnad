"""The Phase 4 demo script: mock core path runs end to end; --live without a key refuses without any network."""
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from engine.db import get_dsn

ROOT = Path(__file__).resolve().parents[2]


def _env(drop_key=False):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    if drop_key:
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def test_demo_mock_runs_end_to_end():
    try:
        psycopg.connect(get_dsn(), connect_timeout=3).close()
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    p = subprocess.run([sys.executable, "scripts/demo_phase4.py"], cwd=ROOT, env=_env(True), capture_output=True,
                       text=True, timeout=240)
    out = p.stdout
    assert p.returncode == 0, out[-2000:] + p.stderr[-2000:]
    for needle in ("MOCK mode", "regional_ops_lead asks about Singapore", "Scenario 2", "Scenario 3",
                   "reroute_shipment", "flag_invoice", "HTTP 403", "HTTP 409", "on_behalf_of=demo-planner",
                   "executions performed by an agent: 0", "hidden objects behave like missing ones",
                   "denied", "INFERRED"):
        assert needle in out, needle
    assert "Traceback" not in out + p.stderr


def test_demo_live_without_key_refuses_and_runs_nothing():
    p = subprocess.run([sys.executable, "scripts/demo_phase4.py", "--live"], cwd=ROOT, env=_env(True),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    assert "ANTHROPIC_API_KEY" in p.stderr
    assert p.stdout == ""          # no world was built, nothing printed to stdout
