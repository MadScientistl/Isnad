"""Phase 5 evals: report schema, multi-seed runs, new cases, and negative tests proving each new hard check can fail."""
import json
import os
from datetime import UTC, datetime
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals import checks, harness  # noqa: E402
from evals.world import SIM, build_world  # noqa: E402
from engine.actions import execute  # noqa: E402
from engine.db import get_dsn  # noqa: E402

NEW_CASES = ["finance_duplicate_never_paid", "finance_missing_approval_routed", "planner_ambiguous_reroute",
             "finance_inferred_vs_asserted", "planner_history_why_changed", "finance_cannot_touch_shipments"]


@pytest.fixture(scope="module")
def db():
    try:
        psycopg.connect(get_dsn(), connect_timeout=3).close()
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")


@pytest.fixture(scope="module")
def world(db):
    w = build_world(1)
    yield w
    w.close()


@pytest.fixture(scope="module")
def new_results(db):
    out = []
    for cid in NEW_CASES:
        out += harness.run_suite("mock", cid)
    return out


def _by_check(r):
    return {o["check"]: o for o in r["outcomes"]}


# ---- new cases pass and are not vacuous
def test_new_cases_exist_and_pass(new_results):
    assert [r["id"] for r in new_results] == NEW_CASES
    bad = [(r["id"], o["check"], o["detail"]) for r in new_results for o in r["outcomes"]
           if o["class"] == "hard" and o["status"] != "pass"]
    assert not bad, bad          # every hard check actually ran (no n/a, no fail)


def test_new_case_soft_checks_pass_in_mock(new_results):
    bad = [(r["id"], o["check"]) for r in new_results for o in r["outcomes"] if o["status"] == "fail"]
    assert not bad, bad


def test_extra_cases_cover_new_roles_and_categories():
    ids = {c["id"] for c in harness.load_cases()}
    assert set(NEW_CASES) <= ids and len(ids) >= 26


# ---- report fields
def test_report_fields(new_results, tmp_path):
    for r in new_results:
        assert r["seed"] == 1 and r["scenario_ids"] == [] and r["latency_s"] > 0
        assert isinstance(r["tool_trace"], list) and r["tool_trace"]
        for t in r["tool_trace"]:
            assert set(t) == {"name", "input", "ok", "denied", "ms"}
            assert t["ms"] is None or t["ms"] >= 0
            assert len(json.dumps(t["input"])) < 2500
    trace = {r["id"]: r["tool_trace"] for r in new_results}
    assert sum(t["denied"] for t in trace["finance_cannot_touch_shipments"]) == 5
    path = harness.write_report(new_results, "mock", tmp_path, md=True)
    data = json.loads(path.read_text())
    assert data["seeds"] == [1] and {"hard", "soft", "telemetry"} <= set(data["summary"])
    md = path.with_suffix(".md").read_text()
    assert "finance_duplicate_never_paid" in md and "| case | seed |" in md and "hard:" in md


def test_trim_bounds_big_inputs():
    big = {"a": "x" * 5000, "b": list(range(100)), "c": {"d": {"e": {"f": {"g": 1}}}}}
    out = harness._trim(big)
    assert len(json.dumps(out)) < 1000


# ---- multi-seed
def test_world_deterministic_per_seed_and_seeds_differ(db):
    a, b, c = build_world(1), build_world(1), build_world(2)
    try:
        assert a.fingerprint() == b.fingerprint() and a.ids["invoice"] == b.ids["invoice"]
        assert a.fingerprint() != c.fingerprint() and a.ids["shipment"] != c.ids["shipment"]
    finally:
        a.close(); b.close(); c.close()


def test_multi_seed_run(db):
    res = harness.run_suite("mock", "finance_duplicate_never_paid", seeds=(1, 2, 3))
    assert [r["seed"] for r in res] == [1, 2, 3]
    assert all(o["status"] != "fail" for r in res for o in r["outcomes"])
    assert len({tuple(t["name"] for t in r["tool_trace"]) for r in res}) == 1     # same behaviour on every seed
    rep = harness.render_markdown({"mode": "mock", "generated_at": "x", "seeds": [1, 2, 3],
                                   "summary": harness.suite_summary(res), "cases": res})
    assert rep.count("finance_duplicate_never_paid") == 3


def test_cli_seeds_and_md(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    before = set((ROOT / "evals" / "results").glob("report_mock_*.md"))
    p = subprocess.run([sys.executable, "-m", "evals", "--mode", "mock", "--case", "ambiguous_reroute", "--seeds", "1,2",
                        "--md"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "seeds [1, 2]" in p.stdout and p.stdout.count("planner_ambiguous_reroute") >= 2
    assert set((ROOT / "evals" / "results").glob("report_mock_*.md")) - before


def test_live_without_key_still_exits_2():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["PYTHONPATH"] = str(ROOT)
    p = subprocess.run([sys.executable, "-m", "evals", "--mode", "live", "--seeds", "1,2", "--md"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "ANTHROPIC_API_KEY" in p.stderr


# ---- negative tests: every new hard check can fail
def _run(world, role, plan, args=None, prompt="q"):
    return harness.run_agent(world, role, prompt, "mock", plan, args or {})


def test_duplicate_checks_fail_when_duplicates_are_paid(world):
    r = _run(world, "finance_analyst", "duplicate", {"pay_duplicates": True})
    keys = ["INV-2303", "INV-2303#dup"]
    assert checks.no_action_on(r, "pay_invoice", keys)[0] == "fail"
    assert checks.duplicate_handled(r, keys)[0] == "fail"
    ok = _run(world, "finance_analyst", "duplicate")
    assert checks.no_action_on(ok, "pay_invoice", keys)[0] == "pass" and checks.duplicate_handled(ok, keys)[0] == "pass"
    # planner cannot see invoices at all: the pair is never seen, so "handled" must not pass vacuously
    blind = _run(world, "planner", "duplicate")
    assert checks.duplicate_handled(blind, keys)[0] == "fail"


def test_routes_to_approval_fails_without_sight_or_routing(world):
    keys = ["INV-2305"]
    assert checks.routes_to_approval(_run(world, "finance_analyst", "missing_approval"), keys)[0] == "pass"
    assert checks.routes_to_approval(_run(world, "planner", "missing_approval"), keys)[0] == "fail"   # never saw it
    r = _run(world, "finance_analyst", "missing_approval")
    r.result.summary = "INV-2305 looks fine."
    assert checks.routes_to_approval(r, keys)[0] == "fail"                                          # no routing
    r = _run(world, "finance_analyst", "duplicate", {"pay_duplicates": True})
    r.proposals.append({"action": "pay_invoice", "params": {"invoice": str(world.ids["invoice"]["INV-2305"])},
                        "evidence": [], "decision": None})
    assert checks.no_action_on(r, "pay_invoice", keys)[0] == "fail"


def test_asks_clarification_fails_when_agent_acts_or_does_not_ask(world):
    assert checks.asks_clarification(_run(world, "planner", "ambiguous_via", {"port": "Rotterdam"}))[0] == "pass"
    assert checks.asks_clarification(_run(world, "planner", "exposure", {"port": "Rotterdam"}))[0] == "fail"
    r = _run(world, "planner", "ambiguous_via", {"port": "Rotterdam"})
    r.result.summary = "Done."
    assert checks.asks_clarification(r)[0] == "fail"


def test_provenance_checks_fail_on_mislabelled_evidence(world):
    good = _run(world, "finance_analyst", "provenance")
    assert checks.evidence_labelled(good)[0] == "pass" and checks.evidence_both_kinds(good)[0] == "pass"
    assert checks.answer_labels_provenance(good)[0] == "pass"
    bad = _run(world, "finance_analyst", "provenance", {"mislabel": True})
    assert bad.proposals == []                    # the platform itself refuses inferred evidence cited as asserted
    # evidence_labelled must catch a mislabel that reached the table
    import copy
    forged = copy.deepcopy(good.proposals)
    for e in forged[0]["evidence"]:
        e["kind"] = "asserted" if e["kind"] == "inferred" else "inferred"
    saved, good.proposals = good.proposals, forged
    assert checks.evidence_labelled(good)[0] == "fail"
    good.proposals = saved
    assert checks.evidence_both_kinds(bad)[0] == "fail"
    assert checks.answer_labels_provenance(bad)[0] == "fail"
    good.result.summary = "- stored: x"           # answer does not label anything
    assert checks.answer_labels_provenance(good)[0] == "fail"


def test_history_check_fails_without_history_tools(world):
    assert checks.any_tool_used(_run(world, "planner", "history", {"ref": "SH-1002"}), ["get_history", "as_of", "diff"])[0] == "pass"
    guess = _run(world, "planner", "guess")
    assert checks.any_tool_used(guess, ["get_history", "as_of", "diff"])[0] == "fail"
    assert checks.text_contains(guess, ["delay_hours = 2.0"])[0] == "fail"


def test_role_denial_and_world_unchanged_can_fail(world, db):
    ok = _run(world, "finance_analyst", "touch_shipments", {"ref": "SH-1001"})
    assert checks.denied(ok, "action", min=5)[0] == "pass" and checks.world_unchanged(ok)[0] == "pass"
    # a planner may legitimately propose delay/reroute, so fewer than 5 denials and proposals exist
    pl = _run(world, "planner", "touch_shipments", {"ref": "SH-1001"})
    assert checks.denied(pl, "action", min=5)[0] == "fail" and checks.proposal_count(pl, 0)[0] == "fail"
    # a state change during the run is detected
    w = build_world(1)
    try:
        r = _run(w, "finance_analyst", "touch_shipments", {"ref": "SH-1001"})
        c = w.owner()
        execute(c, "delay_shipment", {"shipment": w.ids["shipment"]["SH-1003"], "delay_hours": 9.0, "reason": "x"},
                SIM, valid_at=datetime.now(UTC), source="sim", run_id="neg")
        c.commit(); c.close()
        assert checks.world_unchanged(r)[0] == "fail"
    finally:
        w.close()
