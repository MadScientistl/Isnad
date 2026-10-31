"""Smoke test for scripts/demo_phase6.py: runs the whole narrated demo in a subprocess with reduced parameters.

A subprocess keeps the demo's environment (ISNAD_AUTH=1, temp dirs) out of the pytest process. Needs the database up.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "demo_phase6.py"


def run_demo(*args, env_extra=None, timeout=300):
    env = {**os.environ, "PYTHONPATH": str(ROOT), **(env_extra or {})}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_live_without_key_exits_2_before_any_work():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    r = subprocess.run([sys.executable, str(SCRIPT), "--live"], cwd=ROOT, env={**env, "PYTHONPATH": str(ROOT)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "ANTHROPIC_API_KEY" in r.stderr


@pytest.mark.perf
def test_demo_end_to_end_reduced():
    r = run_demo("--days", "2", "--model", "mock")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    m = re.search(r"(\d+)/(\d+) steps passed", r.stdout)
    assert m and m.group(1) == m.group(2) and int(m.group(2)) == 7, r.stdout[-2000:]
    assert "FAIL" not in r.stdout
