import itertools
import time
from datetime import UTC, datetime, timedelta

import pytest

from engine import kpis, scenarios
from engine.db import connect
from engine.db.principal import Principal
from sim import whatif
from sim.runner import SimRunner

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_n = itertools.count()


@pytest.fixture(scope="module")
def world(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=1)
    r.run_until(EPOCH + timedelta(days=6))
    r.close()
    return schema, ids


def fork(schema):
    c = connect(schema=schema)
    try:
        p = Principal("admin", f"wi-{next(_n)}", "")
        d = scenarios.create(c, p, "wi")
        return scenarios.resolve(c, p, d["id"]), p
    finally:
        c.close()


def go(schema, ctx, **kw):
    kw.setdefault("seed", 1)
    kw.setdefault("days", 3)
    kw.setdefault("injections", [])
    return whatif.run(lambda: connect(schema=schema), ctx, **kw)


def q(schema, sql, *a):
    c = connect(schema=schema)
    try:
        cur = c.execute(sql, a)
        r = cur.fetchall() if cur.description else []
        c.commit()
        return r
    finally:
        c.close()


def main_sums(schema):
    return (q(schema, "SELECT count(*), coalesce(sum(seq),0) FROM audit_event WHERE scenario_id=0")[0],
            q(schema, "SELECT count(*) FROM graph_outbox")[0],
            q(schema, "SELECT count(*), round(coalesce(sum(cargo_value),0)::numeric, 3), round(coalesce(sum(progress),0)::numeric, 3) FROM obj_shipment WHERE scenario_id=0")[0],
            q(schema, "SELECT count(*) FROM obj_supplier WHERE scenario_id=0")[0],
            q(schema, "SELECT count(*) FROM obj_port WHERE scenario_id=0")[0])


def test_determinism_and_isolation(world):
    schema, _ = world
    before = main_sums(schema)
    a, b = fork(schema), fork(schema)
    r1, r2 = go(schema, a[0]), go(schema, b[0])
    assert r1.last_error is None, r1.last_error
    assert r1.actions > 20 and r1.digest == r2.digest and r1.actions == r2.actions
    assert go(schema, fork(schema)[0], seed=2).digest != r1.digest
    assert main_sums(schema) == before
    assert q(schema, "SELECT count(*) FROM webhook_outbox")[0][0] == 0
    assert q(schema, "SELECT count(*) FROM audit_event WHERE scenario_id=%s AND source='sim' "
                     "AND run_id=%s", a[0].id, f"scenario:{a[0].id}:1")[0][0] == r1.actions
    assert q(schema, "SELECT clock FROM scenario_state WHERE scenario_id=%s", a[0].id)[0][0] == r1.sim_now


def test_beyond_wall_clock_and_cap(world, monkeypatch):
    schema, _ = world
    ctx, _p = fork(schema)
    assert r_now(schema, ctx) < datetime.now(UTC)
    # push the scenario clock far into the future of wall clock
    q(schema, "UPDATE scenario_state SET clock=%s WHERE scenario_id=%s", datetime.now(UTC) + timedelta(days=400), ctx.id)
    c = connect(schema=schema)
    try:
        from engine.db import scenario_admin
        ctx2 = scenario_admin.ctx_of(c, ctx.id)
    finally:
        c.close()
    monkeypatch.setenv("ISNAD_SCN_SIM_MAX_DAYS", "900")
    r = go(schema, ctx2, days=1)
    assert r.sim_now > datetime.now(UTC) + timedelta(days=399)
    monkeypatch.setenv("ISNAD_SCN_SIM_MAX_DAYS", "90")
    with pytest.raises(ValueError):
        go(schema, fork(schema)[0], days=91)
    with pytest.raises(ValueError):
        go(schema, fork(schema)[0], days=0)


def r_now(schema, ctx):
    c = connect(schema=schema)
    try:
        return whatif.start_time(c, ctx)
    finally:
        c.close()


def kp(schema, sid):
    c = connect(schema=schema)
    try:
        return kpis.compute(c, whatif.ADMIN, scenario_id=sid)
    finally:
        c.close()


def test_port_closure_effects(world):
    schema, ids = world
    base = go(schema, fork(schema)[0])
    r = go(schema, fork(schema)[0], injections=[{"kind": "port_closure", "target": str(p), "duration_h": 200}
                                                for p in ids["ports"][:3]])
    assert r.stats.get("close_port", 0) >= 3
    assert r.kpis["ports_closed"] > base.kpis["ports_closed"]
    assert r.kpis["value_at_risk"] >= base.kpis["value_at_risk"]
    assert r.digest != base.digest


def test_supplier_delay_and_price_shock(world):
    schema, _ = world
    base = go(schema, fork(schema)[0], days=2)
    sd = go(schema, fork(schema)[0], days=2, injections=[{"kind": "supplier_delay", "duration_h": 200}])
    assert sd.stats.get("delay_shipment", 0) > base.stats.get("delay_shipment", 0), sd.skipped
    assert sd.kpis["delay_hours_total"] > base.kpis["delay_hours_total"]
    ps = go(schema, fork(schema)[0], days=2, injections=[{"kind": "price_shock", "magnitude": 2.0}])
    assert ps.last_error is None, ps.last_error
    assert ps.kpis["extra_cost"] > base.kpis["extra_cost"], (ps.kpis, ps.skipped)
    again = go(schema, fork(schema)[0], days=2, injections=[{"kind": "price_shock", "magnitude": 2.0}])
    assert again.digest == ps.digest


def test_same_fork_two_scenarios_compare(world):
    schema, ids = world
    a, pa = fork(schema)
    b, pb = fork(schema)
    ra = go(schema, a, injections=[{"kind": "port_closure", "target": str(ids["ports"][0]), "duration_h": 72}])
    rb = go(schema, b, injections=[{"kind": "price_shock", "magnitude": 3.0}])
    assert ra.digest != rb.digest
    assert ra.kpis != rb.kpis
    c = connect(schema=schema)
    try:
        cmp = scenarios.compare(c, whatif.ADMIN, a.id, b.id)
        assert cmp["diff"]
    finally:
        c.close()


def test_14_day_timing(world):
    schema, _ = world
    ctx, _p = fork(schema)
    t0 = time.perf_counter()
    r = go(schema, ctx, days=14)
    print(f"\n14-day what-if: {time.perf_counter() - t0:.1f}s, {r.actions} actions")
    assert r.last_error is None and r.actions > 100


def test_deadline_stops_cleanly_with_partial_result(world):
    schema, _ = world
    ctx, _p = fork(schema)
    r = go(schema, ctx, days=5, deadline=time.monotonic() - 1)
    assert r.partial and "time budget" in (r.last_error or "")
    assert r.sim_now < EPOCH + timedelta(days=6, hours=24 * 5 - 1) and r.actions == 0 or r.partial


def test_discarded_scenario_returns_a_clean_error(world):
    schema, _ = world
    ctx, p = fork(schema)
    c = connect(schema=schema)
    try:
        scenarios.discard(c, p, ctx.id)
    finally:
        c.close()
    r = go(schema, ctx, days=1)
    assert r.partial and r.last_error and ("ScenarioNotFound" in r.last_error or "ScenarioLimit" in r.last_error)


def test_sim_slots_are_shared_and_bounded(monkeypatch):
    from sim import limits
    monkeypatch.setenv("ISNAD_SCN_MAX_SIM_JOBS", "2")
    limits._sem = None
    assert limits.acquire_sim_slot(0) and limits.acquire_sim_slot(0) and not limits.acquire_sim_slot(0)
    limits.release_sim_slot()
    assert limits.acquire_sim_slot(0)
    limits.release_sim_slot(); limits.release_sim_slot()
    limits._sem = None
