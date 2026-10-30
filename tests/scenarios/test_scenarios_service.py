"""Scenario service + scenario-aware action engine (Phase 5): create/limits, compare, merge, discard, expiry."""
from datetime import timedelta

import pytest
from psycopg.rows import dict_row

from scn_helpers import ADMIN, FIN, LEAD, PLANNER, PLANNER2, checksum, now
from engine import kpis, scenarios
from engine.actions import (CriteriaFailed, MergeConflict, PermissionDenied, ScenarioNotFound, decide, dry_run,
                            execute, propose, rollback)
from engine.db import bitemporal as bt
from engine.db import scoped
from engine.scenarios import ScenarioLimit


def mk(conn, who=PLANNER, name="s", **kw):
    return scenarios.create(conn, who, name, **kw)["id"]


def ex(conn, name, params, who=PLANNER, sid=0, **kw):
    return execute(conn, name, params, who, valid_at=now(), scenario_id=sid, **kw)


def rows(conn, q, *a):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, a)
        out = cur.fetchall()
    conn.rollback()
    return out


# ------------------------------------------------------------------ lifecycle
def test_create_get_list_resolve(conn, world):
    d = scenarios.create(conn, PLANNER, "Closure test", shared_roles=["finance_analyst"])
    assert d["status"] == "open" and d["owner_user"] == "pat" and d["owner_role"] == "planner"
    assert d["shared_roles"] == ["finance_analyst"] and d["override_rows"] == 0 and d["data_version"] == 0
    assert d["expires_at"] is not None and d["base_recorded"] is not None
    assert scenarios.get(conn, PLANNER, d["id"])["name"] == "Closure test"
    assert [x["id"] for x in scenarios.list_(conn, PLANNER)] == [d["id"]]
    ctx = scenarios.resolve(conn, PLANNER, d["id"])
    assert ctx.id == d["id"] and ctx.base_recorded == d["base_recorded"]
    # visibility: other planner / regional lead cannot see it, shared finance role and admin can
    for who in (PLANNER2, LEAD):
        assert scenarios.list_(conn, who) == []
        with pytest.raises(ScenarioNotFound):
            scenarios.get(conn, who, d["id"])
        with pytest.raises(ScenarioNotFound):
            scenarios.resolve(conn, who, d["id"])
    assert scenarios.resolve(conn, FIN, d["id"]).id == d["id"]
    assert scenarios.resolve(conn, ADMIN, d["id"]).id == d["id"]
    for bad in (0, 999, -1):
        with pytest.raises(ScenarioNotFound):
            scenarios.resolve(conn, PLANNER, bad)


def test_create_validation(conn, world):
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "  ")
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "x", shared_roles=["sim"])
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "x", shared_roles=["nobody"])
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "x", expires_in_h=0)
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "x", parent=1)


def test_limits(conn, world, monkeypatch):
    monkeypatch.setenv("AMARH_SCN_MAX_PER_USER", "2")
    monkeypatch.setenv("AMARH_SCN_MAX_PER_RUN", "1")
    monkeypatch.setenv("AMARH_SCN_MAX_GLOBAL", "3")
    a = mk(conn)
    mk(conn)
    with pytest.raises(ScenarioLimit):
        mk(conn)                                              # per user
    scenarios.discard(conn, PLANNER, a)
    mk(conn, name="again")                                    # a slot is free again
    r1 = mk(conn, ADMIN, run_id="run-1")
    with pytest.raises(ScenarioLimit):
        mk(conn, ADMIN, run_id="run-1")                       # per run (and global: 3 open)
    monkeypatch.setenv("AMARH_SCN_MAX_PER_RUN", "3")
    with pytest.raises(ScenarioLimit):
        mk(conn, PLANNER2)                                    # global cap counts other users' scenarios
    assert r1


def test_expiry(conn, world):
    sid = mk(conn, expires_in_h=1)
    other = mk(conn, name="long", expires_in_h=100)
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 5}, sid=sid)
    assert scenarios.expire_due(conn) == []
    assert scenarios.expire_due(conn, now=now() + timedelta(hours=2)) == [sid]
    assert scenarios.get(conn, PLANNER, sid)["status"] == "expired"
    with pytest.raises(ScenarioNotFound):
        scenarios.resolve(conn, PLANNER, sid)
    assert scenarios.resolve(conn, PLANNER, other).id == other
    assert rows(conn, "SELECT count(*) AS n FROM obj_shipment WHERE scenario_id = %s", sid)[0]["n"] == 0
    assert rows(conn, "SELECT count(*) AS n FROM audit_event WHERE scenario_id = %s", sid)[0]["n"] == 1  # audit stays


# ------------------------------------------------------------- engine in scenario
def test_execute_in_scenario_is_isolated_and_audited(conn, world):
    before = checksum(conn)
    sid = mk(conn)
    r = ex(conn, "close_port", {"port": world["emea_port"], "reason": "strike"}, sid=sid)
    assert r.changed and r.webhooks == []
    assert checksum(conn) == before                                   # main: byte-identical (incl. outboxes)
    a = rows(conn, "SELECT scenario_id, name, status FROM audit_event WHERE action_id = %s", r.action_id)
    assert a == [{"scenario_id": sid, "name": "close_port", "status": "executed"}]
    d = rows(conn, "SELECT scenario_id FROM execution_diff WHERE action_id = %s", r.action_id)
    assert d and all(x["scenario_id"] == sid for x in d)
    assert rows(conn, "SELECT count(*) AS n FROM webhook_outbox")[0]["n"] == 0
    # read inside / outside
    ctx = scenarios.resolve(conn, PLANNER, sid)
    with conn.transaction():
        with scoped(conn, PLANNER, ctx):
            assert bt.get_object(conn, "Port", world["emea_port"], scenario_id=sid)["status"] == "closed"
    conn.commit()
    with conn.transaction():
        with scoped(conn, PLANNER):
            assert bt.get_object(conn, "Port", world["emea_port"])["status"] == "open"
    conn.commit()


def test_same_permissions_in_scenario(conn, world):
    sid = mk(conn)
    with pytest.raises(PermissionDenied):                              # planner may not flag invoices
        ex(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "x"}, sid=sid)
    d = rows(conn, "SELECT status, scenario_id FROM audit_event WHERE name = 'flag_invoice'")
    assert d == [{"status": "denied", "scenario_id": sid}]             # denial audited WITH the scenario id
    with pytest.raises(CriteriaFailed):
        ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": -1}, sid=sid)
    with pytest.raises(ScenarioNotFound):                              # not visible: PLANNER2 cannot use it
        ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 1}, who=PLANNER2, sid=sid)
    with pytest.raises(ScenarioNotFound):
        ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 1}, sid=4242)
    # regional lead cannot reach the APAC port from inside a scenario either (RLS identical)
    lsid = mk(conn, LEAD, name="lead")
    with pytest.raises(Exception):
        ex(conn, "close_port", {"port": world["apac_port"]}, who=LEAD, sid=lsid)


def test_shared_role_is_read_only(conn, world):
    sid = mk(conn, shared_roles=["finance_analyst"])
    with pytest.raises(PermissionDenied):
        ex(conn, "pay_invoice", {"invoice": world["inv"]}, who=FIN, sid=sid)
    with pytest.raises(PermissionDenied):
        scenarios.discard(conn, FIN, sid)
    with pytest.raises(PermissionDenied):
        scenarios.merge(conn, FIN, sid)
    assert scenarios.compare(conn, FIN, sid)["diff"] == []            # but it can read / compare


def test_no_approval_gate_inside_scenario_but_gate_on_main(conn, world):
    sid = mk(conn, FIN, name="fin")
    from engine.actions import ApprovalRequired
    with pytest.raises(ApprovalRequired):
        ex(conn, "pay_invoice", {"invoice": world["inv"]}, who=FIN)
    r = ex(conn, "pay_invoice", {"invoice": world["inv"]}, who=FIN, sid=sid)
    assert r.changed and r.status == "executed"


def test_dry_run_in_scenario_has_impact(conn, world):
    sid = mk(conn)
    before = checksum(conn)
    r = dry_run(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=now(), scenario_id=sid)
    assert r.impact["before"]["kpis"]["value_at_risk"] == 0.0
    assert r.impact["after"]["kpis"]["value_at_risk"] == 1000.0
    assert r.impact["delta"]["kpis"]["value_at_risk"] == 1000.0
    assert r.impact["delta"]["kpis"]["ports_closed"] == 1
    assert checksum(conn) == before
    c = scenarios.compare(conn, PLANNER, sid)
    assert c["diff"] == [] and c["kpis"]["delta"]["value_at_risk"] == 0       # the dry run left no trace


def test_rollback_inside_scenario(conn, world):
    sid = mk(conn)
    r = ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 4}, sid=sid)
    with pytest.raises(Exception):                                     # main cannot roll back a scenario execution
        rollback(conn, r.action_id, PLANNER, "x")
    rb = rollback(conn, r.action_id, PLANNER, "undo", scenario_id=sid)
    assert rb.changed
    row = rows(conn, "SELECT scenario_id FROM rollback WHERE original_action_id = %s", r.action_id)
    assert row == [{"scenario_id": sid}]
    assert scenarios.compare(conn, PLANNER, sid)["kpis"]["delta"]["shipments_delayed"] == 0


def test_reroute_cost_rule(conn, world):
    sid = mk(conn)
    new = world["apac_port"]
    ex(conn, "reroute_shipment", {"shipment": world["ship"], "new_port": new}, sid=sid)
    c = scenarios.compare(conn, PLANNER, sid)
    mod = [d for d in c["diff"] if d["type"] == "Shipment"][0]
    assert mod["op"] == "modify" and mod["after"]["extra_cost"] == pytest.approx(20.0)   # 0.02 * 1000
    assert (mod["before"].get("extra_cost") or 0) == 0
    assert c["kpis"]["delta"]["extra_cost"] == pytest.approx(20.0)
    # no-op reroute (already via that port) adds nothing
    ex(conn, "reroute_shipment", {"shipment": world["ship"], "new_port": new}, sid=sid)
    assert scenarios.compare(conn, PLANNER, sid)["kpis"]["delta"]["extra_cost"] == pytest.approx(20.0)


# ------------------------------------------------------------------- compare
def test_compare_orientation_kpis_and_two_scenarios(conn, world):
    s1, s2 = mk(conn, name="a"), mk(conn, name="b")
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=s1)
    ex(conn, "delay_shipment", {"shipment": world["ship2"], "delay_hours": 3}, sid=s2)
    c = scenarios.compare(conn, PLANNER, s1)
    assert c["a"]["id"] == s1 and c["b"] == {"id": "main"} and not c["truncated"]
    ports = [d for d in c["diff"] if d["type"] == "Port"]
    assert len(ports) == 1 and ports[0]["before"]["status"] == "open" and ports[0]["after"]["status"] == "closed"
    assert ports[0]["op"] == "modify" and ports[0]["kind"] == "object"
    assert any(d["type"] == "Event" and d["op"] == "create" and d["before"] is None for d in c["diff"])
    assert any(d["kind"] == "link" and d["type"] == "affects" and d["op"] == "create" for d in c["diff"])
    assert c["kpis"]["a"]["value_at_risk"] == 1000.0 and c["kpis"]["b"]["value_at_risk"] == 0.0
    assert c["kpis"]["delta"]["value_at_risk"] == 1000.0               # delta = a - b
    assert c["kpis"]["a"]["exposure_shipments"] == 1
    c2 = scenarios.compare(conn, PLANNER, s1, s2)
    assert c2["b"]["id"] == s2
    assert c2["kpis"]["delta"]["value_at_risk"] == 1000.0 - 500.0
    assert c2["kpis"]["delta"]["shipments_delayed"] == -1
    cr = scenarios.compare(conn, PLANNER, s2, s1)                       # reversed orientation flips signs
    assert cr["kpis"]["delta"]["value_at_risk"] == -(1000.0 - 500.0)
    # truncation
    small = scenarios.compare(conn, PLANNER, s1, limit=1)
    assert small["truncated"] and len(small["diff"]) == 1
    with pytest.raises(ScenarioNotFound):
        scenarios.compare(conn, PLANNER2, s1)
    with pytest.raises(ScenarioNotFound):
        scenarios.compare(conn, PLANNER, s1, 4242)


def test_compare_is_filtered_and_masked_per_caller(conn, world):
    sid = mk(conn, ADMIN, name="adm", shared_roles=["regional_ops_lead", "planner", "finance_analyst"])
    ex(conn, "close_port", {"port": world["apac_port"]}, who=ADMIN, sid=sid)         # APAC: hidden from the EMEA lead
    ex(conn, "close_port", {"port": world["emea_port"]}, who=ADMIN, sid=sid)
    ex(conn, "flag_invoice", {"invoice": world["inv"], "anomaly": "dup"}, who=ADMIN, sid=sid)   # hidden from planner
    adm = scenarios.compare(conn, ADMIN, sid)
    assert {d["key"] for d in adm["diff"] if d["type"] == "Port"} == {str(world["apac_port"]), str(world["emea_port"])}
    lead = scenarios.compare(conn, LEAD, sid)
    keys = {d["key"] for d in lead["diff"]}
    assert str(world["apac_port"]) not in keys and str(world["emea_port"]) in keys
    assert "Invoice" not in {d["type"] for d in lead["diff"]}
    assert lead["kpis"]["delta"]["ports_closed"] == 1                          # the APAC closure is not counted
    plan = scenarios.compare(conn, PLANNER, sid)                                 # shared: sees ports, never invoices
    assert "Invoice" not in {d["type"] for d in plan["diff"]} and plan["kpis"]["delta"]["invoices_flagged"] == 0
    fin = scenarios.compare(conn, FIN, sid)
    assert "Invoice" in {d["type"] for d in fin["diff"]}
    assert fin["kpis"]["delta"]["invoices_flagged"] == 1


# --------------------------------------------------------------------- merge
def test_merge_happy_path(conn, world):
    sid = mk(conn)
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 6}, sid=sid)
    ex(conn, "close_port", {"port": world["emea_port"], "reason": "strike"}, sid=sid)
    res = scenarios.merge(conn, PLANNER, sid, "ship it")
    assert [m["name"] for m in res["merged"]] == ["delay_shipment", "close_port"]
    assert all("action_id" in m for m in res["merged"])
    ex_rows = rows(conn, "SELECT name, source, run_id, scenario_id, actor FROM audit_event WHERE source = 'scenario_merge' "
                         "ORDER BY seq")
    assert [(r["name"], r["run_id"], r["scenario_id"], r["actor"]) for r in ex_rows] == [
        ("delay_shipment", f"scenario:{sid}", 0, "pat"), ("close_port", f"scenario:{sid}", 0, "pat")]
    with conn.transaction():
        with scoped(conn, PLANNER):
            assert bt.get_object(conn, "Shipment", world["ship"])["status"] == "delayed"
            assert bt.get_object(conn, "Port", world["emea_port"])["status"] == "closed"
    conn.commit()
    assert rows(conn, "SELECT count(*) AS n FROM webhook_outbox")[0]["n"] == 1          # close_port notify_ops: main write
    assert scenarios.get(conn, PLANNER, sid)["status"] == "merged"
    with pytest.raises(ScenarioNotFound):
        scenarios.merge(conn, PLANNER, sid)                                              # already closed
    assert rows(conn, "SELECT count(*) AS n FROM obj_shipment WHERE scenario_id = %s", sid)[0]["n"] == 0
    ev = rows(conn, "SELECT kind FROM scenario_event WHERE scenario_id = %s ORDER BY seq", sid)
    assert [e["kind"] for e in ev][-1] == "merged"


def test_merge_conflict_aborts_everything(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)                       # no conflict on this one
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 6}, sid=sid)
    ex(conn, "ship_arrive", {"shipment": world["ship"]})                                 # main moves on: status changed
    before = checksum(conn)
    with pytest.raises(MergeConflict) as e:
        scenarios.merge(conn, PLANNER, sid)
    cs = e.value.conflicts
    assert [(c["kind"], c["type"], c["key"]) for c in cs] == [("object", "Shipment", str(world["ship"]))]
    assert "status" in cs[0]["reason"]
    assert checksum(conn) == before                                                      # NOTHING was written
    assert scenarios.get(conn, PLANNER, sid)["status"] == "open"
    assert rows(conn, "SELECT count(*) AS n FROM audit_event WHERE source = 'scenario_merge'")[0]["n"] == 0


def test_volatile_props_do_not_conflict(conn, world):
    sid = mk(conn)
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 2}, sid=sid)
    ex(conn, "ship_position", {"shipment": world["ship"], "position": {"type": "Point", "coordinates": [1, 2]},
                               "progress": 0.5})                                         # main sim-like update
    res = scenarios.merge(conn, PLANNER, sid)
    assert res["merged"][0]["name"] == "delay_shipment"


def test_sim_executions_are_not_replayed(conn, world):
    from engine.actions import SIM
    sid = mk(conn)
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 2}, sid=sid)
    execute(conn, "ship_arrive", {"shipment": world["ship2"]}, SIM, valid_at=now(), source="sim", scenario_id=sid)
    res = scenarios.merge(conn, PLANNER, sid)
    assert [m["name"] for m in res["merged"]] == ["delay_shipment"]
    with conn.transaction():
        with scoped(conn, PLANNER):
            assert bt.get_object(conn, "Shipment", world["ship2"])["status"] == "in_transit"
    conn.commit()


def test_rolled_back_actions_are_not_replayed(conn, world):
    sid = mk(conn)
    r = ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 2}, sid=sid)
    rollback(conn, r.action_id, PLANNER, "oops", scenario_id=sid)
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    assert [m["name"] for m in scenarios.merge(conn, PLANNER, sid)["merged"]] == ["close_port"]


def test_merge_approval_gated_action_becomes_proposal(conn, world):
    sid = mk(conn, FIN, name="fin")
    ex(conn, "pay_invoice", {"invoice": world["inv"]}, who=FIN, sid=sid)
    res = scenarios.merge(conn, FIN, sid, "pay it")
    assert len(res["merged"]) == 1 and "proposal_id" in res["merged"][0] and "action_id" not in res["merged"][0]
    p = rows(conn, "SELECT action, proposer, proposer_role, scenario_id, rationale FROM proposal")
    assert p == [{"action": "pay_invoice", "proposer": "fiona", "proposer_role": "finance_analyst",
                  "scenario_id": 0, "rationale": f"Merged from scenario {sid}: pay it"}]
    with conn.transaction():
        with scoped(conn, FIN):
            assert bt.get_object(conn, "Invoice", world["inv"])["status"] == "open"          # not executed
    conn.commit()
    r = decide(conn, res["merged"][0]["proposal_id"], ADMIN, True, "ok")                     # normal approval path
    assert r.changed


def test_merge_requires_owner_or_admin(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    with pytest.raises(ScenarioNotFound):
        scenarios.merge(conn, PLANNER2, sid)
    assert scenarios.merge(conn, ADMIN, sid)["merged"][0]["name"] == "close_port"          # admin may merge anyone's
    adm = rows(conn, "SELECT actor, role FROM audit_event WHERE source = 'scenario_merge'")
    assert adm == [{"actor": "adam", "role": "admin"}]


def test_conflict_after_partial_overlap_writes_nothing(conn, world):
    """Two touched keys, only one conflicts: the clean one is NOT applied either."""
    from engine.actions import SIM
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    ex(conn, "ship_arrive", {"shipment": world["ship2"]}, sid=sid)
    execute(conn, "ship_arrive", {"shipment": world["ship2"]}, SIM, valid_at=now(), source="sim")   # main: same key moved
    before = checksum(conn)
    with pytest.raises(MergeConflict) as e:
        scenarios.merge(conn, PLANNER, sid)
    assert {c["key"] for c in e.value.conflicts} == {str(world["ship2"])}
    assert checksum(conn) == before


# ------------------------------------------------------------------- discard
def test_discard_purges_and_leaves_main_identical(conn, world):
    before = checksum(conn)
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid)
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 3}, sid=sid)
    assert scenarios.get(conn, PLANNER, sid)["override_rows"] > 0
    with pytest.raises(ScenarioNotFound):
        scenarios.discard(conn, PLANNER2, sid)
    assert scenarios.discard(conn, PLANNER, sid) == {"id": sid, "status": "discarded"}
    assert checksum(conn) == before
    for t in ("obj_port", "obj_shipment", "obj_event", "link_affects", "scenario_override"):
        assert rows(conn, f"SELECT count(*) AS n FROM {t} WHERE scenario_id = %s", sid)[0]["n"] == 0
    with pytest.raises(ScenarioNotFound):
        scenarios.resolve(conn, PLANNER, sid)
    with pytest.raises(ScenarioNotFound):
        scenarios.discard(conn, PLANNER, sid)
    assert rows(conn, "SELECT count(*) AS n FROM audit_event WHERE scenario_id = %s", sid)[0]["n"] == 2   # audit stays
    # admin can discard someone else's
    s2 = mk(conn)
    assert scenarios.discard(conn, ADMIN, s2)["status"] == "discarded"


# -------------------------------------------------------------------- propose
def test_propose_from_scenario_carries_impact_and_executes_on_main(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid)
    pid = propose(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 8}, PLANNER, "tested",
                  valid_at=now(), scenario_id=sid)
    row = rows(conn, "SELECT scenario_id, dry_run, base_versions FROM proposal WHERE id = %s", pid)[0]
    assert row["scenario_id"] == sid
    dr = row["dry_run"]
    assert dr["scenario_id"] == sid and dr["diff"]
    assert dr["impact"]["before"]["kpis"]["ports_closed"] == 0 and dr["impact"]["after"]["kpis"]["ports_closed"] == 1
    assert dr["impact"]["delta"]["kpis"]["shipments_delayed"] == 1
    assert checksum(conn).count("|")                                    # smoke
    # nothing happened on main until approval; approval executes on MAIN as admin (the scenario stays intact)
    with conn.transaction():
        with scoped(conn, PLANNER):
            assert bt.get_object(conn, "Shipment", world["ship"])["status"] == "in_transit"
    conn.commit()
    r = decide(conn, pid, ADMIN, True, "ok")
    assert r.changed
    a = rows(conn, "SELECT scenario_id, source FROM audit_event WHERE action_id = %s", r.action_id)
    assert a == [{"scenario_id": 0, "source": "api"}]
    ev = rows(conn, "SELECT kind FROM scenario_event WHERE scenario_id = %s ORDER BY seq", sid)
    assert "proposal" in [e["kind"] for e in ev]
    # a PLANNER2 cannot use someone else's scenario as evidence
    with pytest.raises(ScenarioNotFound):
        propose(conn, "delay_shipment", {"shipment": world["ship2"], "delay_hours": 1}, PLANNER2, "x",
                valid_at=now(), scenario_id=sid)


def test_propose_stale_is_judged_against_main(conn, world):
    from engine.actions import StaleProposal
    sid = mk(conn)
    pid = propose(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 8}, PLANNER, "t",
                  valid_at=now(), scenario_id=sid)
    ex(conn, "ship_arrive", {"shipment": world["ship"]})                 # main changes after the proposal
    with pytest.raises(StaleProposal):
        decide(conn, pid, ADMIN, True, "ok")


def test_kpis_series_in_scenario(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid)
    t = now()
    s = kpis.series(conn, PLANNER, sid, "value_at_risk", t - timedelta(days=2), t, 24)
    assert [p["v"] for p in s["points"]][-1] == 1000.0 and s["points"][0]["v"] == 0.0
    m = kpis.series(conn, PLANNER, 0, "value_at_risk", t - timedelta(days=2), t, 24)
    assert {p["v"] for p in m["points"]} == {0.0}
    assert kpis.compute(conn, PLANNER, 0)["exposure_shipments"] == 0
