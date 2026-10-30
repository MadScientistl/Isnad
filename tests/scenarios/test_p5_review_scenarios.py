"""Phase 5 security-review fixes at the service / action-engine level: agent identity (H1), merge of agent work
as proposals (H1/M1), persisted ids and conflicts (M5), expiry and quotas (M3/M4d), evidence staleness (M6),
audited denials and lock (L6)."""
import time
from uuid import UUID

import psycopg
import pytest
import yaml
from psycopg.rows import dict_row

from scn_helpers import ADMIN, FIN, LEAD, PLANNER, PLANNER2, ROOT_YAML, checksum, now, owner_link, owner_obj
from engine import scenarios
from engine.actions import (MergeConflict, PermissionDenied, ScenarioNotFound, StaleProposal, decide, execute,
                            make_agent_principal, propose)
from engine.db import Principal, bitemporal as bt, connect, scoped
from engine.scenarios import ScenarioLimit, service

AGENT = make_agent_principal(PLANNER)        # agent:planner acting for pat
AGENT2 = make_agent_principal(PLANNER2)


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


def main_status(conn, type_, id_):
    with conn.transaction():
        with scoped(conn, ADMIN):
            r = bt.get_object(conn, type_, id_)
    conn.commit()
    return r


# ------------------------------------------------------------------ H1: agent identity
def test_make_agent_principal_shape():
    assert (AGENT.user, AGENT.role, AGENT.on_behalf_of) == ("agent:planner", "planner", "pat")
    assert make_agent_principal(AGENT) is AGENT


def test_agent_opens_and_works_in_the_launchers_scenario(conn, world):
    d = scenarios.create(conn, AGENT, "agent run", run_id="run-1")
    sid = d["id"]
    assert d["owner_user"] == "pat" and d["owner_role"] == "planner" and d["run_id"] == "run-1"
    # the human sees it as theirs; another planner does not; the other user's agent does not
    assert [x["id"] for x in scenarios.list_(conn, PLANNER)] == [sid]
    assert scenarios.list_(conn, PLANNER2) == []
    with pytest.raises(ScenarioNotFound):
        scenarios.resolve(conn, AGENT2, sid)
    assert scenarios.resolve(conn, AGENT, sid).id == sid
    r = ex(conn, "close_port", {"port": world["apac_port"]}, who=AGENT, sid=sid, source="agent", run_id="run-1")
    assert r.changed
    a = rows(conn, "SELECT actor, on_behalf_of, source, role, scenario_id FROM audit_event WHERE action_id = %s",
             r.action_id)
    assert a == [{"actor": "agent:planner", "on_behalf_of": "pat", "source": "agent", "role": "planner",
                  "scenario_id": sid}]
    cmp = scenarios.compare(conn, AGENT, sid)
    assert cmp["kpis"]["delta"]["ports_closed"] == 1
    # an agent identity has no admin powers: the admin's agent cannot even see pat's scenario
    with pytest.raises(ScenarioNotFound):
        scenarios.resolve(conn, make_agent_principal(ADMIN), sid)
    assert scenarios.discard(conn, AGENT, sid)["status"] == "discarded"


def test_agent_and_user_quotas_are_separate(conn, world, monkeypatch):
    monkeypatch.setenv("AMARH_SCN_MAX_PER_USER", "1")
    monkeypatch.setenv("AMARH_SCN_MAX_AGENT_PER_USER", "2")
    monkeypatch.setenv("AMARH_SCN_MAX_PER_RUN", "5")
    mk(conn, AGENT, run_id="r1")
    mk(conn, AGENT, run_id="r2")
    with pytest.raises(ScenarioLimit):
        mk(conn, AGENT, run_id="r3")                       # agent quota of the HUMAN owner
    mk(conn)                                               # the human's own quota (1) is untouched by agent scenarios
    with pytest.raises(ScenarioLimit):
        mk(conn)
    mk(conn, AGENT2, run_id="r4")                          # another human's agent quota is independent


def test_agent_scenario_without_on_behalf_of_is_refused(conn, world):
    with pytest.raises(ValueError):
        scenarios.create(conn, Principal("planner", "agent:planner", ""), "x", run_id="r")


def test_agent_cannot_merge(conn, world):
    sid = mk(conn, AGENT, run_id="r")
    ex(conn, "close_port", {"port": world["apac_port"]}, who=AGENT, sid=sid, source="agent")
    with pytest.raises(PermissionDenied):
        scenarios.merge(conn, AGENT, sid)
    assert main_status(conn, "Port", world["apac_port"])["status"] == "open"


# ------------------------------------------------------------------ merge: agent work becomes proposals
def test_merge_turns_agent_executions_into_proposals_even_when_not_gated(conn, world):
    sid = mk(conn, AGENT, run_id="run-9")
    ra = ex(conn, "close_port", {"port": world["apac_port"], "reason": "storm"}, who=AGENT, sid=sid,
            source="agent", run_id="run-9")
    rh = ex(conn, "ship_arrive", {"shipment": world["ship2"]}, who=PLANNER, sid=sid)          # human, same scenario
    res = scenarios.merge(conn, PLANNER, sid, "ship it")
    by = {m["name"]: m for m in res["merged"]}
    assert "proposal_id" in by["close_port"] and "action_id" in by["ship_arrive"]
    # the agent's close_port did NOT touch main; the human's ship_arrive did
    assert main_status(conn, "Port", world["apac_port"])["status"] == "open"
    assert main_status(conn, "Shipment", world["ship2"])["status"] == "arrived"
    p = rows(conn, "SELECT proposer, proposer_role, on_behalf_of, scenario_id, rationale, run_id, origin_action_id, "
                   "evidence, params FROM proposal")
    assert len(p) == 1
    p = p[0]
    assert (p["proposer"], p["proposer_role"], p["on_behalf_of"], p["scenario_id"]) == \
        ("agent:planner", "planner", "pat", 0)
    assert p["rationale"].startswith(f"Merged from scenario {sid}") and p["run_id"] == "run-9"
    assert p["origin_action_id"] == ra.action_id
    assert p["evidence"]["scenario_id"] == sid and p["evidence"]["origin_action_id"] == str(ra.action_id)
    assert "event_id" in p["params"]                                   # the generated id travels with the proposal
    # replayed human execution carries its origin
    h = rows(conn, "SELECT actor, source, origin_action_id, tool_trace FROM audit_event WHERE source = 'scenario_merge'")
    assert len(h) == 1 and h[0]["origin_action_id"] == rh.action_id
    assert h[0]["tool_trace"]["origin"]["actor"] == "pat" and h[0]["tool_trace"]["origin"]["scenario_id"] == sid
    # D3/P4: the launcher cannot approve; a different approver can, and the execution is audited with provenance
    pid = UUID(by["close_port"]["proposal_id"])
    with pytest.raises(PermissionDenied):
        decide(conn, pid, PLANNER, True)
    r = decide(conn, pid, ADMIN, True, "ok")
    assert main_status(conn, "Port", world["apac_port"])["status"] == "closed"
    a = rows(conn, "SELECT on_behalf_of, proposal_id FROM audit_event WHERE action_id = %s", r.action_id)
    assert a == [{"on_behalf_of": "pat", "proposal_id": pid}]


def test_merge_by_admin_still_makes_the_agent_the_proposer(conn, world):
    sid = mk(conn, AGENT, run_id="run-1")
    ex(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 2}, who=AGENT, sid=sid, source="agent")
    res = scenarios.merge(conn, ADMIN, sid)
    assert "proposal_id" in res["merged"][0]
    p = rows(conn, "SELECT proposer, on_behalf_of FROM proposal")
    assert p == [{"proposer": "agent:planner", "on_behalf_of": "pat"}]


def test_server_generated_ids_are_replayed_unchanged(conn, world):
    sid = mk(conn)
    r = ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    ev = r.created["Event"]
    audited = rows(conn, "SELECT params FROM audit_event WHERE action_id = %s", r.action_id)[0]["params"]
    assert audited["event_id"] == str(ev)
    fs = mk(conn, FIN, name="fin")
    ri = ex(conn, "issue_invoice", {"shipment": world["ship"], "supplier": world["supplier"], "amount": 5.0},
            who=FIN, sid=fs)
    inv = ri.created["Invoice"]
    assert rows(conn, "SELECT params FROM audit_event WHERE action_id = %s", ri.action_id)[0]["params"][
        "invoice_id"] == str(inv)
    scenarios.merge(conn, PLANNER, sid)
    scenarios.merge(conn, FIN, fs)
    assert main_status(conn, "Event", ev)["kind"] == "port_closure"            # same id on main
    assert main_status(conn, "Invoice", inv)["amount"] == 5.0


def _ontology_without(tmp_path, action):
    raw = yaml.safe_load(ROOT_YAML.read_text())
    del raw["actions"][action]
    for r in raw["roles"].values():
        if isinstance(r.get("actions"), list) and action in r["actions"]:
            r["actions"].remove(action)
        ag = r.get("agent")
        if isinstance(ag, dict) and isinstance(ag.get("allowlist"), list) and action in ag["allowlist"]:
            ag["allowlist"].remove(action)
    p = tmp_path / "o.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return str(p)


def test_merge_reports_an_execution_whose_action_no_longer_exists(conn, world, tmp_path):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    before = checksum(conn)
    with pytest.raises(MergeConflict) as e:
        scenarios.merge(conn, PLANNER, sid, ontology=_ontology_without(tmp_path, "close_port"))
    assert e.value.conflicts[0]["kind"] == "action" and e.value.conflicts[0]["type"] == "close_port"
    assert checksum(conn) == before
    assert scenarios.get(conn, PLANNER, sid)["status"] == "open"


def test_merge_reports_a_replay_that_fails_on_main(conn, world):
    other_port = owner_obj(conn, "Port", {"name": "Hamburg", "status": "open", "region": "EMEA"})
    sid = mk(conn)
    ex(conn, "reroute_shipment", {"shipment": world["ship2"], "new_port": other_port}, sid=sid)
    # main: the target port closes after the fork -> reroute's criteria now fail on main (the port is not a touched key)
    ex(conn, "close_port", {"port": other_port})
    before = checksum(conn)
    with pytest.raises(MergeConflict) as e:
        scenarios.merge(conn, PLANNER, sid)
    c = e.value.conflicts
    assert c[0]["kind"] == "action" and c[0]["type"] == "reroute_shipment" and "cannot be replayed" in c[0]["reason"]
    assert checksum(conn) == before


def test_merge_detects_links_added_in_main_that_a_reroute_would_end(conn, world):
    new_port = owner_obj(conn, "Port", {"name": "Hamburg", "status": "open", "region": "EMEA"})
    third = owner_obj(conn, "Port", {"name": "Antwerp", "status": "open", "region": "EMEA"})
    scn = mk(conn, name="reroute")
    ex(conn, "reroute_shipment", {"shipment": world["ship"], "new_port": new_port}, sid=scn)
    owner_link(conn, "ships_via", world["ship"], third, t=now())            # main: a NEW link on the same shipment
    with pytest.raises(MergeConflict) as e:
        scenarios.merge(conn, PLANNER, scn)
    keys = {(c["kind"], c["key"]) for c in e.value.conflicts}
    assert ("link", f"{world['ship']}>{third}") in keys


# ------------------------------------------------------------------ M3 / M4d
def test_ttl_is_capped_server_side(conn, world, monkeypatch):
    monkeypatch.setenv("AMARH_SCN_TTL_H", "2")
    d = scenarios.create(conn, PLANNER, "long", expires_in_h=100000)
    left = (d["expires_at"] - d["created_at"]).total_seconds() / 3600
    assert 1.9 < left < 2.1
    monkeypatch.setenv("AMARH_SCN_TTL_H", "100000")
    assert service.limits()["ttl_h"] == 720.0
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "nan", expires_in_h=float("nan"))
    with pytest.raises(ValueError):
        scenarios.create(conn, PLANNER, "inf", expires_in_h=float("inf"))


def test_expired_scenario_is_closed_on_use_without_the_janitor(conn, world, monkeypatch):
    sid = mk(conn, expires_in_h=0.00001)                      # 36 ms
    time.sleep(0.15)
    with pytest.raises(ScenarioNotFound):
        scenarios.resolve(conn, PLANNER, sid)
    with pytest.raises(ScenarioNotFound):
        ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    with pytest.raises(ScenarioNotFound):
        scenarios.compare(conn, PLANNER, sid)
    assert scenarios.get(conn, PLANNER, sid)["name"] == "s"   # governance history stays readable
    # the lazy janitor on create releases the quota
    monkeypatch.setenv("AMARH_SCN_MAX_PER_USER", "1")
    service._expire_stamp.clear()
    assert mk(conn, name="next")
    assert scenarios.get(conn, PLANNER, sid)["status"] == "expired"


def test_owner_who_loses_access_keeps_the_fork_time_snapshot_until_expiry(conn, world):
    """Documented snapshot-vs-revocation decision: a scenario is a snapshot; the owner's own open scenario keeps the
    fork-time version of an object even if the owner's RLS later stops showing the main row."""
    sid = mk(conn, LEAD, name="lead")                          # EMEA lead
    ctx = scenarios.resolve(conn, LEAD, sid)
    with conn.transaction():
        with scoped(conn, LEAD, ctx):
            assert bt.get_object(conn, "Port", world["emea_port"], scenario_id=sid) is not None
    conn.commit()
    # main moves the port out of the lead's region AFTER the fork: main hides it, the snapshot still shows it
    bt.write_object(conn, "Port", world["emea_port"], {"name": "Rotterdam", "status": "open", "region": "APAC"},
                    now(), "setup")
    conn.commit()
    with conn.transaction():
        with scoped(conn, LEAD):
            assert bt.get_object(conn, "Port", world["emea_port"]) is None
        with scoped(conn, LEAD, ctx):
            assert bt.get_object(conn, "Port", world["emea_port"], scenario_id=sid) is not None
    conn.commit()


def test_expire_run_discards_unreferenced_agent_scenarios(conn, world):
    keep = mk(conn, AGENT, run_id="run-x", name="evidence")
    drop = mk(conn, AGENT, run_id="run-x", name="scratch")
    other = mk(conn, AGENT, run_id="run-y", name="other run")
    ex(conn, "close_port", {"port": world["emea_port"]}, who=AGENT, sid=keep, source="agent")
    ex(conn, "close_port", {"port": world["apac_port"]}, who=AGENT, sid=drop, source="agent")
    pid = propose(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 1}, AGENT, "t",
                  valid_at=now(), scenario_id=keep, run_id="run-x", source="agent")
    assert pid
    assert service.expire_run(conn, "run-x") == [drop]
    assert scenarios.get(conn, PLANNER, drop)["status"] == "discarded"
    assert scenarios.get(conn, PLANNER, keep)["status"] == "open"
    assert scenarios.get(conn, PLANNER, other)["status"] == "open"
    assert rows(conn, "SELECT count(*) AS n FROM obj_port WHERE scenario_id = %s", drop)[0]["n"] == 0
    assert service.expire_run(conn, "run-x") == []
    assert service.expire_run(conn, "") == []


# ------------------------------------------------------------------ M6
def test_evidence_that_changed_after_the_proposal_makes_approval_stale(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid)
    pid = propose(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 8}, PLANNER, "tested",
                  valid_at=now(), scenario_id=sid)
    dv = rows(conn, "SELECT dry_run FROM proposal WHERE id = %s", pid)[0]["dry_run"]["scenario_data_version"]
    assert dv == scenarios.get(conn, PLANNER, sid)["data_version"]
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)          # the evidence moves on
    with pytest.raises(StaleProposal) as e:
        decide(conn, pid, ADMIN, True, "ok")
    assert e.value.changed == ["scenario"]
    assert main_status(conn, "Shipment", world["ship"])["status"] == "in_transit"      # nothing executed
    assert decide(conn, pid, ADMIN, False, "re-propose") is None                        # still rejectable


def test_frozen_evidence_of_a_discarded_scenario_keeps_working(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid)
    pid = propose(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 8}, PLANNER, "t",
                  valid_at=now(), scenario_id=sid)
    scenarios.discard(conn, PLANNER, sid)
    assert decide(conn, pid, ADMIN, True, "ok").changed


def test_an_approver_reads_the_proposal_but_not_the_evidence_scenario(conn, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid)
    pid = propose(conn, "delay_shipment", {"shipment": world["ship"], "delay_hours": 8}, PLANNER, "t",
                  valid_at=now(), scenario_id=sid)
    approver = Principal("regional_ops_lead", "lead-2", "EMEA", ("delay_shipment",))
    nobody = Principal("regional_ops_lead", "lead-3", "EMEA", ())
    with conn.transaction():
        with scoped(conn, approver):
            assert conn.execute("SELECT count(*) FROM proposal WHERE id = %s", [pid]).fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM scenario WHERE id = %s", [sid]).fetchone()[0] == 0
            assert conn.execute("SELECT amarh_scenario_visible(%s)", [sid]).fetchone()[0] is False
        with scoped(conn, nobody):
            assert conn.execute("SELECT count(*) FROM proposal WHERE id = %s", [pid]).fetchone()[0] == 0
    conn.commit()


# ------------------------------------------------------------------ L6
def test_denied_write_by_a_shared_viewer_is_audited_in_the_scenario(conn, world):
    sid = mk(conn, shared_roles=["finance_analyst"])
    with pytest.raises(PermissionDenied):
        ex(conn, "pay_invoice", {"invoice": world["inv"]}, who=FIN, sid=sid)
    d = rows(conn, "SELECT actor, status, scenario_id FROM audit_event WHERE status = 'denied'")
    assert d == [{"actor": "fiona", "status": "denied", "scenario_id": sid}]
    # a caller who cannot see the scenario learns nothing and leaves no row in it
    with pytest.raises(ScenarioNotFound):
        ex(conn, "close_port", {"port": world["apac_port"]}, who=PLANNER2, sid=sid)
    assert rows(conn, "SELECT count(*) AS n FROM audit_event WHERE status = 'denied'")[0]["n"] == 1


def test_discard_and_merge_share_one_lock_and_are_exclusive(conn, aschema, world):
    sid = mk(conn)
    ex(conn, "close_port", {"port": world["apac_port"]}, sid=sid)
    c2 = connect(schema=aschema)
    try:
        c2.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"scenario-merge:{sid}"])
        for fn in (lambda: scenarios.discard(conn, PLANNER, sid), lambda: scenarios.merge(conn, PLANNER, sid)):
            with pytest.raises(psycopg.errors.LockNotAvailable):   # waits on the same advisory lock (lock_timeout)
                fn()
            conn.rollback()
    finally:
        c2.rollback()
        c2.close()
    scenarios.discard(conn, PLANNER, sid)
    with pytest.raises(ScenarioNotFound):
        scenarios.merge(conn, PLANNER, sid)
    sid2 = mk(conn)
    ex(conn, "close_port", {"port": world["emea_port"]}, sid=sid2)
    scenarios.merge(conn, PLANNER, sid2)
    with pytest.raises(ScenarioNotFound):
        scenarios.discard(conn, PLANNER, sid2)
