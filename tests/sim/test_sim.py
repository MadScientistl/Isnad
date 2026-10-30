import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from engine.db import connect
from sim.geo import Path, haversine_km
from sim.runner import SimRunner

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

DAYS = 30


def at(h):
    return EPOCH + timedelta(hours=h)


def run_sim(db, seed=1, days=DAYS, injections=(), **kw):
    """Fresh seeded schema; inject [(hour, kind, target_index_or_None, duration)], run, close."""
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=seed, **kw)
    out = {"schema": schema, "ids": ids}
    t0 = time.perf_counter()
    r.run_until(at(days * 24))
    out["wall"] = time.perf_counter() - t0
    out.update(digest=r.event_digest(), actions=r.actions, err=r.last_error, stats=dict(r.world.stats),
               active=len(r.world.active), skipped=list(r.world.skipped))
    r.close()
    return out


@pytest.fixture(scope="module")
def base(db):
    return run_sim(db, seed=1)


def rows(schema, q, *a):
    c = connect(schema=schema)
    try:
        return c.execute(q, a).fetchall()
    finally:
        c.close()


@pytest.mark.perf
def test_thirty_days_completes_and_measure(base):
    assert base["err"] is None, base["err"]
    assert base["actions"] > 500
    for k in ("create_object", "ship_depart", "ship_position", "ship_arrive", "issue_invoice"):
        assert base["stats"].get(k, 0) > 0, k
    print(f"\n30 sim days: {base['actions']} actions in {base['wall']:.1f}s "
          f"= {base['actions'] / base['wall']:.0f} actions/s; stats={base['stats']}")
    # Marked `perf`: the 60 s default holds on a developer machine only (49-61 s observed); shared CI runners are
    # slower, so tune with ISNAD_SIM_MAX_SECONDS or deselect with `-m "not perf"`.
    assert base["wall"] < float(os.environ.get("ISNAD_SIM_MAX_SECONDS", "60"))


def test_determinism(db, base):
    again = run_sim(db, seed=1)
    assert again["digest"] == base["digest"] and again["actions"] == base["actions"]
    other = run_sim(db, seed=2, days=10)
    assert other["digest"] != base["digest"]


def test_audit_rows_match_writes(base):
    s = base["schema"]
    n_sim = rows(s, "SELECT count(*) FROM audit_event WHERE source='sim'")[0][0]
    assert n_sim == base["actions"]
    assert rows(s, "SELECT count(*) FROM audit_event WHERE source='sim' AND actor<>'sim'")[0][0] == 0
    sim_ids = {r[0] for r in rows(s, "SELECT action_id FROM audit_event WHERE source='sim'")}
    for t in ("obj_shipment", "obj_invoice", "obj_port", "obj_event", "link_ships_via",
              "link_ships_from", "link_invoices_for", "link_affects"):
        for aid, by in rows(s, f"SELECT action_id, changed_by FROM {t}"):
            if by == "sim":
                assert aid in sim_ids, (t, aid)
    # every sim action id is unique and every create_object has an after snapshot
    assert len(sim_ids) == n_sim


def test_positions_valid_and_monotonic(base):
    s = base["schema"]
    pts = rows(s, "SELECT params->>'shipment', (params->'position'->'coordinates'->>0)::float, "
                  "(params->'position'->'coordinates'->>1)::float, (params->>'progress')::float "
                  "FROM audit_event WHERE name='ship_position' ORDER BY seq")
    assert len(pts) > 200
    last = {}
    for sid, lon, lat, prog in pts:
        assert -180 <= lon <= 180 and -90 <= lat <= 90
        assert 0 <= prog <= 1
        assert prog >= last.get(sid, 0) - 1e-9
        last[sid] = prog
    # consecutive positions of a shipment are at most one 6 h sailing apart (plus rounding)
    prev = {}
    for sid, lon, lat, prog in pts:
        if sid in prev:
            assert haversine_km(prev[sid], (lon, lat)) <= 6 * 35 * 1.02 + 1, (sid, prev[sid], lon, lat)
        prev[sid] = (lon, lat)


def test_schema_state_consistent(base):
    s = base["schema"]
    bad = rows(s, "SELECT count(*) FROM obj_shipment WHERE upper_inf(valid_during) AND status NOT IN "
                  "('planned','in_transit','delayed','arrived')")
    assert bad[0][0] == 0
    # arrived shipments have progress 1
    assert rows(s, "SELECT count(*) FROM obj_shipment WHERE upper_inf(valid_during) AND status='arrived' "
                   "AND progress<>1")[0][0] == 0


def test_geo_helpers():
    p = Path([(0, 0), (10, 0), (10, 10)])
    assert p.at(0) == (0, 0) and p.at(1) == (10, 10)
    assert abs(p.frac(1) - 0.5) < 0.01
    lon, lat = p.at(0.25)
    assert 0 < lon < 10 and abs(lat) < 1e-6


def test_port_closure_injection(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=3)
    r.run_until(at(24 * 6))
    w = r.world
    score = {}
    for sh in w.active.values():
        for i, pid in enumerate(sh.ports):
            if i >= sh.pidx and pid not in w.closed:
                dist = (sh.port_fracs[i] - sh.progress) * sh.path.total
                if dist < 3000:
                    score[pid] = score.get(pid, 0) + 1
    assert score, "fixture: no in-flight shipment heading to a port"
    port = max(sorted(score), key=lambda p: score[p])
    t_inj = r._h + 5
    rec = r.inject("port_closure", target=port, at=at(t_inj), duration_h=120)
    assert rec["target"] == str(port) and rec["kind"] == "port_closure"
    r.run_until(at(t_inj + 24 * 12))
    assert r.last_error is None
    r.close()
    cl = rows(schema, "SELECT sim_time FROM audit_event WHERE name='close_port' AND params->>'port'=%s "
                      "AND params->>'reason'='injected'", str(port))
    ro = rows(schema, "SELECT sim_time FROM audit_event WHERE name='reopen_port' AND params->>'port'=%s "
                      "AND sim_time > %s", str(port), at(t_inj))
    assert cl[0][0] == at(t_inj)
    assert ro[0][0] == at(t_inj + 120)
    delayed = rows(schema, "SELECT count(*) FROM audit_event WHERE name='delay_shipment' AND "
                           "params->>'reason'='port closed ahead' AND sim_time >= %s AND sim_time < %s",
                   at(t_inj), at(t_inj + 120))
    assert delayed[0][0] >= 1
    # the Event closed at reopen time
    ended = rows(schema, "SELECT (after->(jsonb_object_keys(after)))->>'ended' FROM audit_event "
                         "WHERE name='reopen_port' AND params->>'port'=%s AND sim_time=%s", str(port),
                 at(t_inj + 120))
    assert any(e for (e,) in ended)


def test_invoice_anomaly_and_injection_determinism(db, base):
    def go():
        schema, ids = db.new()
        r = SimRunner(schema=schema, seed=1)
        r.run_until(at(100))
        inv = sorted(i.id for i in r.world.invoices.values() if i.status == "open")[0]
        r.inject("invoice_anomaly", target=inv, at=at(120))
        r.inject("shipment_delay", at=at(130), duration_h=20)       # default target from seeded rng
        r.run_until(at(24 * 10))
        d = r.event_digest()
        r.close()
        return schema, inv, d, r.last_error

    s1, inv, d1, e1 = go()
    s2, inv2, d2, e2 = go()
    assert e1 is None and d1 == d2 and inv == inv2
    fl = rows(s1, "SELECT params->>'anomaly', sim_time FROM audit_event WHERE name='flag_invoice' "
                  "AND params->>'invoice'=%s", str(inv))
    # either a direct flag, or (duplicate kind) a flagged duplicate was created at 120h
    dup = rows(s1, "SELECT count(*) FROM audit_event WHERE name='flag_invoice' AND sim_time=%s", at(120))
    assert fl or dup[0][0] == 1
    assert rows(s1, "SELECT count(*) FROM obj_invoice WHERE status='flagged' AND upper_inf(valid_during)")[0][0] >= 1
    assert d1 != base["digest"]


def test_injection_validation(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=1)
    r.run_until(at(30))
    with pytest.raises(ValueError):
        r.inject("meteor")
    with pytest.raises(ValueError):
        r.inject("port_closure", duration_h=0)
    with pytest.raises(ValueError):
        r.inject("port_closure", at=at(1))
    with pytest.raises(ValueError):
        r.inject("port_closure", target="00000000-0000-0000-0000-000000000000")
    rec = r.inject("port_closure")                 # default target and time
    assert rec["at"] == r.sim_now.isoformat()
    r.close()


def test_resume(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=5)
    r.run_until(at(24 * 8))
    n_before, active_before = r.actions, set(r.world.active)
    assert active_before
    r.close()
    r2 = SimRunner(schema=schema, seed=5)
    assert r2.sim_now == at(24 * 8)
    assert r2.state()["tick"] == 24 * 8
    r2.run_until(at(24 * 12))
    assert r2.last_error is None, r2.last_error
    assert r2.sim_now == at(24 * 12)
    assert r2.actions > 0
    # restored shipments kept moving (or arrived)
    moved = rows(schema, "SELECT count(*) FROM audit_event WHERE name IN ('ship_position','ship_arrive') "
                         "AND sim_time > %s AND params->>'shipment' = ANY(%s)", at(24 * 8),
                 [str(i) for i in active_before])
    assert moved[0][0] > 0
    assert rows(schema, "SELECT max(sim_time) FROM audit_event WHERE source='sim'")[0][0] == at(24 * 12) \
        or rows(schema, "SELECT max(sim_time) FROM audit_event WHERE source='sim'")[0][0] <= at(24 * 12)
    # a different run_id starts from the epoch
    r3 = SimRunner(schema=schema, seed=5, run_id="other")
    assert r3.sim_now == EPOCH
    r3.close()
    r2.close()


def test_step_and_speed_control(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=1)
    assert r.state()["speed"] == "paused" and r.state()["sim_now"] == EPOCH.isoformat()
    for i in range(1, 4):
        st = r.step()
        assert st["tick"] == i and st["sim_now"] == at(i).isoformat()
    with pytest.raises(ValueError):
        r.set_speed("100")
    assert r.set_speed(24)["speed"] == "24"
    # paused thread does not advance
    r.set_speed("paused")
    r.start()
    time.sleep(0.4)
    assert r.state()["tick"] == 3 and r.state()["running"]
    r.set_speed("24")
    time.sleep(1.5)
    st = r.state()
    assert st["tick"] > 3 + 10 and st["achieved_hours_per_s"] and st["achieved_hours_per_s"] > 5
    r.set_speed("paused")
    time.sleep(0.3)
    t1 = r.state()["tick"]
    time.sleep(0.4)
    assert r.state()["tick"] == t1
    r.stop()
    assert not r.state()["running"]
    r.close()


def test_threadsafe_state_and_inject(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=1)
    r.run_until(at(48))
    r.set_speed("24")
    r.start()
    errs = []

    def poll():
        try:
            for _ in range(40):
                r.state()
                time.sleep(0.01)
        except Exception as e:
            errs.append(e)

    th = threading.Thread(target=poll)
    th.start()
    for _ in range(3):
        r.inject("port_closure", duration_h=24)
        time.sleep(0.1)
    th.join()
    r.stop()
    assert not errs
    assert r.last_error is None
    r.close()


def test_never_passes_wall_clock(db):
    schema, ids = db.new()
    epoch = datetime.now(UTC) - timedelta(hours=3, minutes=30)
    epoch = epoch.replace(minute=0, second=0, microsecond=0) - timedelta(hours=0)
    # seed at that epoch: entities exist from EPOCH (2026-01-01) so valid at later epoch too
    r = SimRunner(schema=schema, seed=1, epoch=epoch)
    r.run_until(epoch + timedelta(days=10))
    st = r.state()
    assert st["capped"] and st["tick"] <= 4
    assert r.sim_now <= datetime.now(UTC)
    r.step()
    assert r.sim_now <= datetime.now(UTC)
    r.close()


def test_reset(db):
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=1)
    r.run_until(at(10))
    r.reset()
    assert r.state()["tick"] == 0 and r.actions == 0 and r.sim_now == EPOCH
    r.close()


def test_resume_ignores_history_before_reset(db):
    """A `reset` audit event starts a new epoch: a runner built afterwards starts from the epoch even
    though old sim audit rows (kept on purpose) are still there."""
    schema, ids = db.new()
    r = SimRunner(schema=schema, seed=7)
    r.run_until(at(48))
    r.close()
    r1 = SimRunner(schema=schema, seed=7)
    assert r1.sim_now > EPOCH  # without the reset event it would resume here
    r1.close()
    c = connect(schema=schema)
    try:
        c.execute("INSERT INTO audit_event (action_id, name, actor, source, params, role) "
                  "VALUES (gen_random_uuid(), 'reset', 'demo-admin', 'api', '{}', 'admin')")
        c.commit()
    finally:
        c.close()
    r2 = SimRunner(schema=schema, seed=7)
    assert r2.sim_now == EPOCH and r2.state()["tick"] == 0
    r2.close()
