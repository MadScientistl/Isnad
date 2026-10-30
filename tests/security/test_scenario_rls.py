"""Row-level security of the scenario overlay (Phase 5): no role can read or write another scenario's rows,
a forged isnad.scenario fails closed, masking and filters apply to scenario-effective data."""
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from conftest import PRINCIPALS
from engine.db import OWNER, Principal, bitemporal as bt, connect, scenario_admin, scoped

PLAN = PRINCIPALS["planner"]
PLAN2 = Principal("planner", "u-plan2", "")
LEAD = PRINCIPALS["regional_ops_lead"]
FIN = PRINCIPALS["finance_analyst"]
ADMIN = PRINCIPALS["admin"]
CORE = {"valid_during", "recorded_during", "changed_by", "action_id", "scenario_id", "id", "valid_from", "valid_to",
        "recorded_from", "recorded_to"}


@pytest.fixture
def c(world):
    conn = connect(schema=world.schema)
    yield conn
    conn.rollback()
    conn.close()


def now():
    return datetime.now(UTC) - timedelta(minutes=5)


def mk(c, owner, shared=()):
    r = scenario_admin.create_base(c, name="rls", owner_user=owner.user, owner_role=owner.role, shared_roles=shared)
    return scenario_admin.ctx_of(c, r["id"])


def put(c, who, ctx, type_, id_, props):
    with scoped(c, who, ctx):
        return bt.write_object(c, type_, id_, props, now(), who.user, scenario_id=ctx.id)


def props_of(c, type_, id_):
    row = bt.get_object(c, type_, id_, masked=False)
    return {k: v for k, v in row.items() if k not in CORE}


def names(c, who, ctx, view, col="name"):
    with scoped(c, who, ctx):
        return {r[0] for r in c.execute(f"SELECT {col} FROM {view}").fetchall()}


def test_other_user_cannot_read_a_scenario(c, world):
    ctx = mk(c, PLAN)
    sh = uuid.uuid4()
    put(c, PLAN, ctx, "Shipment", sh, {"ref": "SCN-ONLY", "status": "planned"})
    assert names(c, PLAN, ctx, "rd_eff_obj_shipment", "ref") >= {"SCN-ONLY", "S1"}
    for who in (PLAN2, FIN, LEAD):                                   # not owner, not admin, not shared
        with scoped(c, who, ctx):                                    # forged scenario context
            assert c.execute("SELECT count(*) FROM rd_eff_obj_shipment").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM eff_obj_shipment").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM obj_shipment WHERE scenario_id = %s", [ctx.id]).fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM scenario_override").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM scenario WHERE id = %s", [ctx.id]).fetchone()[0] == 0
            assert c.execute("SELECT isnad_scenario_visible(%s)", [ctx.id]).fetchone()[0] is False
    assert "SCN-ONLY" in names(c, ADMIN, ctx, "rd_eff_obj_shipment", "ref")      # admin sees every scenario


def test_scenario_rows_never_appear_on_main(c, world):
    ctx = mk(c, PLAN)
    put(c, PLAN, ctx, "Shipment", uuid.uuid4(), {"ref": "SCN-ONLY", "status": "planned"})
    for who in (PLAN, ADMIN):
        assert "SCN-ONLY" not in names(c, who, None, "rd_cur_obj_shipment", "ref")
        assert "SCN-ONLY" not in names(c, who, None, "rd_obj_shipment", "ref")
        assert "SCN-ONLY" not in names(c, who, None, "obj_shipment", "ref")
        assert "SCN-ONLY" not in names(c, who, None, "rd_eff_obj_shipment", "ref")    # no scenario set = main
        assert "SCN-ONLY" not in names(c, who, ctx, "rd_cur_obj_shipment", "ref")     # cur_* stays main-only
    # even the owner, with no scenario selected, sees nothing of it
    with scoped(c, PLAN):
        assert c.execute("SELECT count(*) FROM obj_shipment WHERE scenario_id = %s", [ctx.id]).fetchone()[0] == 0


def test_cannot_write_into_someone_elses_scenario(c, world):
    ctx = mk(c, PLAN, shared=("finance_analyst",))
    for who in (PLAN2, FIN):                                         # PLAN2: invisible, FIN: shared = read-only
        with scoped(c, who, ctx), pytest.raises((psycopg.errors.InsufficientPrivilege, ValueError, psycopg.Error)):
            with c.transaction():
                bt.write_object(c, "Port", uuid.uuid4(), {"name": "X", "region": "EMEA", "status": "open"},
                                now(), who.user, scenario_id=ctx.id)
    # writing scenario rows from the main context (GUC unset) is refused too
    with scoped(c, PLAN), pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute("INSERT INTO obj_port (id, name, valid_during, recorded_during, changed_by, scenario_id) "
                      "VALUES (%s, 'x', tstzrange(now() - interval '1 day', NULL), tstzrange(now(), NULL), 'u', %s)",
                      [uuid.uuid4(), ctx.id])
    assert c.execute("SELECT count(*) FROM obj_port WHERE scenario_id = %s", [ctx.id]).fetchone()[0] == 0


def test_shared_role_can_read_but_not_write(c, world):
    ctx = mk(c, PLAN, shared=("finance_analyst",))
    sh = uuid.uuid4()
    put(c, PLAN, ctx, "Shipment", sh, {"ref": "SHARED", "status": "planned"})
    assert "SHARED" in names(c, FIN, ctx, "rd_eff_obj_shipment", "ref")
    assert "SHARED" not in names(c, PLAN2, ctx, "rd_eff_obj_shipment", "ref")


def test_closed_scenario_disappears_but_audit_remains(c, world):
    ctx = mk(c, PLAN)
    put(c, PLAN, ctx, "Shipment", uuid.uuid4(), {"ref": "GONE", "status": "planned"})
    aid = uuid.uuid4()
    with scoped(c, PLAN, ctx):
        c.execute("INSERT INTO audit_event (action_id, name, actor, source, role, scenario_id) "
                  "VALUES (%s,'delay_shipment','u-plan','api','planner',%s)", [aid, ctx.id])
        c.execute("INSERT INTO scenario_event (scenario_id, kind, actor) VALUES (%s,'discarded','u-plan')", [ctx.id])
    assert "GONE" not in names(c, PLAN, ctx, "rd_eff_obj_shipment", "ref")
    assert "GONE" not in names(c, ADMIN, ctx, "rd_eff_obj_shipment", "ref")
    with scoped(c, PLAN):
        assert c.execute("SELECT count(*) FROM audit_event WHERE action_id = %s", [aid]).fetchone()[0] == 1
    with scoped(c, PLAN2):
        assert c.execute("SELECT count(*) FROM audit_event WHERE action_id = %s", [aid]).fetchone()[0] == 0


def test_audit_rows_of_a_scenario_are_scoped(c, world):
    a, b = mk(c, PLAN), mk(c, PLAN2)
    aid = uuid.uuid4()
    with scoped(c, PLAN, a):
        c.execute("INSERT INTO audit_event (action_id, name, actor, source, role, scenario_id) "
                  "VALUES (%s,'x','u-plan','api','planner',%s)", [aid, a.id])
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("INSERT INTO audit_event (action_id, name, actor, source, role, scenario_id) "
                      "VALUES (%s,'x','u-plan','api','planner',%s)", [uuid.uuid4(), b.id])   # not the scoped scenario
    with scoped(c, PLAN2):
        assert c.execute("SELECT count(*) FROM audit_event WHERE action_id = %s", [aid]).fetchone()[0] == 0
    with scoped(c, ADMIN):
        assert c.execute("SELECT count(*) FROM audit_event WHERE action_id = %s", [aid]).fetchone()[0] == 1


def test_inferred_support_of_a_scenario_is_scoped(c, world):
    ctx = mk(c, PLAN)
    sup = world["sup_a"]
    c.execute("INSERT INTO inferred_support (scenario_id, link, src, dst, derivation_no, support) VALUES (%s,'supplies',%s,%s,0,%s)",
              [ctx.id, sup, world["sup_c"], [sup, world["sup_c"]]])
    with scoped(c, PLAN, ctx):
        assert c.execute("SELECT count(*) FROM inferred_support WHERE scenario_id = %s", [ctx.id]).fetchone()[0] == 1
    with scoped(c, PLAN):                                           # same user, main context
        assert c.execute("SELECT count(*) FROM inferred_support WHERE scenario_id = %s", [ctx.id]).fetchone()[0] == 0
    with scoped(c, PLAN2, ctx):                                     # forged
        assert c.execute("SELECT count(*) FROM inferred_support WHERE scenario_id = %s", [ctx.id]).fetchone()[0] == 0


def test_masking_holds_on_rd_eff(c, world):
    ctx = mk(c, ADMIN, shared=("planner", "regional_ops_lead"))
    s = world["sup_a"]
    p = props_of(c, "Supplier", s)
    p["country"] = "FR"
    put(c, ADMIN, ctx, "Supplier", s, p)
    for who in (PLAN, LEAD):
        with scoped(c, who, ctx):
            rows = c.execute("SELECT name, country FROM rd_eff_obj_supplier WHERE id = %s", [s]).fetchall()
            assert rows == [("A", None)]
    with scoped(c, ADMIN, ctx):
        assert c.execute("SELECT country FROM rd_eff_obj_supplier WHERE id = %s", [s]).fetchall() == [("FR",)]
    with scoped(c, PLAN, ctx):                    # the fork-time snapshot (written unmasked by admin) is not readable
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("SELECT base FROM scenario_override")
        ov = bt.list_overrides(c, ctx.id)
        assert ov and all(o["base"] is None for o in ov)
        assert c.execute("SELECT count(*) FROM scenario_override").fetchone()[0] == len(ov)
    with scoped(c, ADMIN, ctx):
        assert any(o["base"] and o["base"]["props"]["country"] == "DE" for o in bt.list_overrides(c, ctx.id))


def test_regional_lead_reads_and_updates_scenario_only_object(c, world):
    ctx = mk(c, LEAD)
    pid = uuid.uuid4()
    put(c, LEAD, ctx, "Port", pid, {"name": "ScnPort", "region": "EMEA", "status": "open"})
    assert "ScnPort" in names(c, LEAD, ctx, "rd_eff_obj_port")
    put(c, LEAD, ctx, "Port", pid, {"name": "ScnPort", "region": "EMEA", "status": "closed"})     # UPDATE
    with scoped(c, LEAD, ctx):
        assert c.execute("SELECT status FROM rd_eff_obj_port WHERE id = %s", [pid]).fetchall() == [("closed",)]
        assert bt.get_object(c, "Port", pid, scenario_id=ctx.id)["status"] == "closed"
    assert "ScnPort" not in names(c, LEAD, None, "rd_cur_obj_port")
    # the row filter applies to scenario rows as to main rows: a non-EMEA port cannot be created
    with scoped(c, LEAD, ctx), pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
        bt.write_object(c, "Port", uuid.uuid4(), {"name": "Nope", "region": "APAC", "status": "open"},
                        now(), "u-lead", scenario_id=ctx.id)


def test_scenario_modified_region_changes_visibility(c, world):
    ctx = mk(c, ADMIN, shared=("regional_ops_lead",))
    for key, region in (("p_apac", "EMEA"), ("p_emea2", "APAC")):
        p = props_of(c, "Port", world[key]); p["region"] = region
        put(c, ADMIN, ctx, "Port", world[key], p)
    w = props_of(c, "Warehouse", world["w_apac"]); w["region"] = "EMEA"
    put(c, ADMIN, ctx, "Warehouse", world["w_apac"], w)
    w2 = props_of(c, "Warehouse", world["w_emea2"]); w2["region"] = "APAC"
    put(c, ADMIN, ctx, "Warehouse", world["w_emea2"], w2)
    ports = names(c, LEAD, ctx, "rd_eff_obj_port")
    assert "Singapore" in ports and "Hamburg" not in ports and "Rotterdam" in ports
    main_ports = names(c, LEAD, None, "rd_cur_obj_port")
    assert "Singapore" not in main_ports and "Hamburg" in main_ports
    # shipments follow exists(ships_to, Warehouse: region = $region) evaluated on the effective warehouses
    ships = names(c, LEAD, ctx, "rd_eff_obj_shipment", "ref")
    assert "S2" in ships and "S4" not in ships and "S1" in ships
    main_ships = names(c, LEAD, None, "rd_cur_obj_shipment", "ref")
    assert "S2" not in main_ships and "S4" in main_ships
    # by-id access agrees (hidden = missing)
    with scoped(c, LEAD, ctx):
        assert bt.get_object(c, "Port", world["p_apac"], scenario_id=ctx.id) is not None
        assert bt.get_object(c, "Port", world["p_emea2"], scenario_id=ctx.id) is None
        assert bt.get_object(c, "Shipment", world["s4"], scenario_id=ctx.id) is None
    # the lead's own (not shared) view of the scenario does not exist
    assert names(c, PLAN2, ctx, "rd_eff_obj_port") == set()


def test_link_policy_with_scenario_only_endpoints(c, world):
    ctx = mk(c, ADMIN, shared=("regional_ops_lead",))
    n_emea, n_apac = uuid.uuid4(), uuid.uuid4()
    put(c, ADMIN, ctx, "Shipment", n_emea, {"ref": "N-EMEA", "status": "planned"})
    put(c, ADMIN, ctx, "Shipment", n_apac, {"ref": "N-APAC", "status": "planned"})
    with scoped(c, ADMIN, ctx):
        bt.write_link(c, "ships_to", n_emea, world["w_emea"], now(), "u-admin", scenario_id=ctx.id,
                      src_type="Shipment", dst_type="Warehouse")
        bt.write_link(c, "ships_to", n_apac, world["w_apac"], now(), "u-admin", scenario_id=ctx.id,
                      src_type="Shipment", dst_type="Warehouse")
    with scoped(c, LEAD, ctx):
        refs = {r[0] for r in c.execute("SELECT ref FROM rd_eff_obj_shipment").fetchall()}
        assert "N-EMEA" in refs and "N-APAC" not in refs
        assert bt.list_links(c, "ships_to", scenario_id=ctx.id) and \
            {r["src"] for r in bt.list_links(c, "ships_to", scenario_id=ctx.id)} == {world["s1"], world["s4"], n_emea}
        assert bt.neighbours(c, "ships_to", n_apac, scenario_id=ctx.id) == []     # endpoint hidden -> edge hidden
        assert bt.neighbours(c, "ships_to", n_emea, scenario_id=ctx.id) == [world["w_emea"]]
    with scoped(c, LEAD):                                                          # main: nothing of it
        assert c.execute("SELECT count(*) FROM link_ships_to WHERE src IN (%s,%s)", [n_emea, n_apac]).fetchone()[0] == 0


def test_forged_scenario_guc_by_raw_sql_is_fail_closed(c, world):
    ctx = mk(c, PLAN)
    put(c, PLAN, ctx, "Shipment", uuid.uuid4(), {"ref": "SECRET-SCN", "status": "planned"})
    with scoped(c, PLAN2):                                                         # main context, then forge the GUC
        c.execute("SELECT set_config('isnad.scenario', %s, true), set_config('isnad.scenario_base', now()::text, true)",
                  [str(ctx.id)])
        assert c.execute("SELECT count(*) FROM rd_eff_obj_shipment").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM obj_shipment WHERE ref = 'SECRET-SCN'").fetchone()[0] == 0
        # main rows the user may read anyway stay readable through the base tables (no scenario data leaks)
        assert c.execute("SELECT count(*) FROM obj_shipment WHERE scenario_id <> 0").fetchone()[0] == 0


def test_scenario_writes_leave_graph_outbox_and_main_untouched(c, world):
    before_outbox = c.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]
    before_main = c.execute("SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM obj_port x WHERE scenario_id = 0").fetchone()[0]
    ctx = mk(c, ADMIN)
    p = props_of(c, "Port", world["p_emea"]); p["status"] = "closed"
    put(c, ADMIN, ctx, "Port", world["p_emea"], p)
    with scoped(c, ADMIN, ctx):
        bt.delete_object(c, "Port", world["p_amer"], now(), "u-admin", scenario_id=ctx.id)
    assert c.execute("SELECT count(*) FROM graph_outbox").fetchone()[0] == before_outbox
    assert c.execute("SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM obj_port x WHERE scenario_id = 0").fetchone()[0] == before_main
    assert "Houston" not in names(c, ADMIN, ctx, "rd_eff_obj_port") and "Houston" in names(c, ADMIN, None, "rd_cur_obj_port")


def test_isnad_app_cannot_tamper_with_scenario_tables(c, world):
    ctx = mk(c, PLAN)
    with scoped(c, PLAN, ctx):
        for stmt in ("DELETE FROM scenario_override", "TRUNCATE scenario_override", "UPDATE scenario SET name = 'x'",
                     "DELETE FROM scenario_event", "DELETE FROM scenario"):
            with pytest.raises(psycopg.Error), c.transaction():
                c.execute(stmt)
        # cannot create a scenario owned by someone else
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute("INSERT INTO scenario (name, owner_user, owner_role, base_recorded) VALUES ('x','u-admin','admin',now())")
