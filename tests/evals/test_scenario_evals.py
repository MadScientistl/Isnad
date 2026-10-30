"""Phase 5 scenario eval cases: they pass, report scenario ids, and every new hard check can fail."""
import copy
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine.actions import execute  # noqa: E402
from engine.db import get_dsn  # noqa: E402
from evals import checks, harness  # noqa: E402
from evals.world import SIM, build_world  # noqa: E402

SCN_CASES = ["scenario_disruption_reroute", "scenario_forbidden_lead", "scenario_forbidden_finance",
             "scenario_hidden_lead", "scenario_double_execution", "scenario_inferred_finance",
             "scenario_injection_contained", "scenario_injection_obeyed", "scenario_merge_conflict", "scenario_cap",
             "scenario_foreign_user"]


@pytest.fixture(scope="module")
def db():
    try:
        psycopg.connect(get_dsn(), connect_timeout=3).close()
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")


@pytest.fixture
def world(db):
    w = build_world(1)
    yield w
    w.close()


@pytest.fixture(scope="module")
def results(db):
    return harness.run_suite("mock", "scenario_", seeds=(1,))


def _run(world, role, plan, args=None):
    return harness.run_agent(world, role, "q", "mock", plan, args or {})


def test_all_scenario_cases_present_and_hard_checks_pass(results):
    assert [r["id"] for r in results] == SCN_CASES
    bad = [(r["id"], o["check"], o["detail"]) for r in results for o in r["outcomes"]
           if o["class"] == "hard" and o["status"] != "pass"]
    assert not bad, bad
    soft = [(r["id"], o["check"], o["detail"]) for r in results for o in r["outcomes"] if o["status"] == "fail"]
    assert not soft, soft


def test_report_lists_scenario_ids(results, tmp_path):
    by = {r["id"]: r for r in results}
    assert len(by["scenario_disruption_reroute"]["scenario_ids"]) == 1
    assert len(by["scenario_cap"]["scenario_ids"]) == 3
    names = [t["name"] for t in by["scenario_disruption_reroute"]["tool_trace"]]
    assert "scenario_open" in names and "scenario_apply" in names and "scenario_compare" in names
    path = harness.write_report(results, "mock", tmp_path, md=True)
    assert "scenario_cap" in path.with_suffix(".md").read_text()


def test_scenario_cases_are_mock_or_have_live_twin():
    cases = {c["id"]: c for c in harness.load_cases("scenario_")}
    assert set(cases) == set(SCN_CASES)
    live = [c["id"] for c in cases.values() if "live" in (c.get("modes") or ["mock", "live"])]
    assert live == ["scenario_disruption_reroute"]
    assert any(a["class"] == "soft" for a in cases["scenario_disruption_reroute"]["asserts"])


# ---- negative tests: each new check can fail
def test_scenario_used_and_impact_present_fail_without_scenario(world):
    ok = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam"})
    assert checks.scenario_used(ok)[0] == "pass" and checks.impact_present(ok)[0] == "pass"
    skipped = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam", "skip_scenario": True})
    assert skipped.proposals and checks.scenario_used(skipped)[0] == "fail"
    assert checks.impact_present(skipped)[0] == "fail"
    forged = copy.deepcopy(ok.proposals)
    forged[0]["dry_run"].pop("impact")
    saved, ok.proposals = ok.proposals, forged
    assert checks.impact_present(ok)[0] == "fail"
    ok.proposals = saved
    ok.proposals[0]["dry_run"]["impact"]["delta"]["kpis"] = {k: 0 for k in ok.proposals[0]["dry_run"]["impact"]["delta"]["kpis"]}
    assert checks.impact_present(ok)[0] == "fail"          # an all-zero impact is not an impact


def test_main_unchanged_detects_main_writes(world, db):
    r = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam"})
    assert checks.main_unchanged(r)[0] == "pass" and checks.executed_only_in_scenario(r)[0] == "pass"
    forged = list(r.executions) + [{"name": "reroute_shipment", "status": "executed", "scenario_id": 0}]
    saved, r.executions = r.executions, forged
    assert checks.main_unchanged(r)[0] == "fail" and checks.executed_only_in_scenario(r)[0] == "fail"
    r.executions = saved
    c = world.owner()
    execute(c, "delay_shipment", {"shipment": world.ids["shipment"]["SH-1003"], "delay_hours": 9.0, "reason": "x"},
            SIM, valid_at=datetime.now(UTC), source="sim", run_id="neg")
    c.commit(); c.close()
    assert checks.main_unchanged(r)[0] == "fail"


def test_scenario_proposals_pending_fails_when_decided_or_misattributed(world):
    r = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam"})
    assert checks.scenario_proposals_pending(r)[0] == "pass"
    r.proposals[0]["decision"] = "approved"
    assert checks.scenario_proposals_pending(r)[0] == "fail"
    r.proposals[0]["decision"], r.proposals[0]["on_behalf_of"] = None, "someone-else"
    assert checks.scenario_proposals_pending(r)[0] == "fail"


def test_no_scenario_leak_fails_on_leak_or_unexercised_compare(world):
    r = _run(world, "regional_ops_lead", "scenario_hidden", {"days": 1})
    hidden = [str(world.ids["shipment"]["SH-1005"]), "Chicago DC"]
    assert checks.no_scenario_leak(r, hidden)[0] == "pass"
    visible_id = str(world.ids["shipment"]["SH-1001"])
    assert checks.no_scenario_leak(r, [visible_id])[0] == "fail"       # a term that IS in the transcript is caught
    exp = _run(world, "planner", "exposure", {"port": "Rotterdam"})     # no scenario_compare at all
    assert checks.no_scenario_leak(exp, hidden)[0] == "fail"


def test_merge_conflict_reported_fails(world):
    r = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam", "propose": False})
    r.post = harness.run_post_hooks(r, ["merge_conflict"])
    assert checks.merge_conflict_reported(r)[0] == "pass"
    for mutate in ({"merge": {"status": 200, "code": None}}, {"merge_fp": ("a", "b")}, {"merge_exec": ["x"]},
                   {"merge_rows": (1, 2)}):
        p = copy.deepcopy(r.post)
        p.update(mutate)
        r.post, saved = p, r.post
        assert checks.merge_conflict_reported(r)[0] == "fail", mutate
        r.post = saved
    r.post = {}
    assert checks.merge_conflict_reported(r)[0] == "na"


def test_merge_conflict_hook_writes_nothing_on_main(world):
    r = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam", "propose": False})
    post = harness.run_post_hooks(r, ["merge_conflict"])
    assert post["merge"]["status"] == 409 and post["merge"]["code"] == "merge_conflict"
    assert post["merge_exec"] == [] and post["merge_fp"][0] == post["merge_fp"][1]


def test_scenario_cap_check_fails_below_cap(world):
    assert checks.scenario_cap_enforced(_run(world, "planner", "scenario_cap", {"n": 4}), 3)[0] == "pass"
    assert checks.scenario_cap_enforced(_run(world, "planner", "scenario_cap", {"n": 3}), 3)[0] == "fail"


def test_denied_in_scenario_fails_without_denials_or_scenario(world):
    r = _run(world, "regional_ops_lead", "scenario_forbidden", {"ref": "SH-1001"})
    assert checks.denied_in_scenario(r, "action", min=4)[0] == "pass"
    assert checks.denied_in_scenario(r, "tool", min=1)[0] == "fail"
    exp = _run(world, "finance_analyst", "touch_shipments", {"ref": "SH-1001"})    # denials, but no scenario
    assert checks.denied_in_scenario(exp, "action", min=1)[0] == "fail"


def test_foreign_scenario_check_fails_when_marker_visible(world):
    harness.SETUPS["foreign_scenario"](world)
    foreign = world.ids["scn"]["foreign"]
    r = harness.run_agent(world, "planner", "q", "mock", "scenario_foreign", {"ref": "SH-1001", "foreign": str(foreign)})
    assert checks.foreign_scenario_unreachable(r, foreign, "777.5")[0] == "pass"
    assert checks.foreign_scenario_unreachable(r, foreign, "SH-1001")[0] == "fail"       # marker present in transcript
    r.scenario_ids = r.scenario_ids + [foreign]
    assert checks.foreign_scenario_unreachable(r, foreign, "777.5")[0] == "fail"


def test_scenario_unaffected_fails_when_scenario_gains_executions(world):
    r = _run(world, "planner", "scenario_reroute", {"port": "Rotterdam"})
    r.post = harness.run_post_hooks(r, ["approve_twice"])
    assert checks.scenario_unaffected(r)[0] == "pass" and checks.approved_once(r)[0] == "pass"
    r.post["scn_exec_after"] += 1
    assert checks.scenario_unaffected(r)[0] == "fail"
    r.post = {}
    assert checks.scenario_unaffected(r)[0] == "na"


def test_inferred_and_history_checks_fail_outside_scenario(world):
    ok = _run(world, "finance_analyst", "scenario_inferred")
    assert checks.inferred_in_scenario(ok)[0] == "pass" and checks.history_in_scenario(ok)[0] == "pass"
    plain = _run(world, "finance_analyst", "provenance")        # inferred edges read on main, no scenario
    assert checks.inferred_in_scenario(plain)[0] == "fail"
    assert checks.history_in_scenario(plain)[0] == "fail"


def test_hidden_changes_omitted_precondition(world):
    r = _run(world, "regional_ops_lead", "scenario_hidden", {"days": 1})
    assert checks.hidden_changes_omitted(r)[0] in ("pass", "fail")      # reported (soft); must not crash
    assert checks.hidden_changes_omitted(_run(world, "planner", "exposure", {"port": "Rotterdam"}))[0] == "na"
