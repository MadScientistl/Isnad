"""DB-level row-level-security tests for the generated policy layer (scratch schema, Postgres required)."""
import os
import threading
import uuid

import psycopg
import pytest

from engine.compiler import compile_ontology
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("AMARH_DSN", "postgresql://amarh:amarh@localhost:55441/amarh")
IDS = {k: uuid.uuid4() for k in ("p_emea", "p_apac", "p_hist", "p_moved", "w_emea", "w_apac", "s_emea", "s_apac",
                                 "sup", "inv", "car", "ev")}


@pytest.fixture(scope="module")
def schema():
    comp = compile_ontology(ROOT / "ontology" / "supply_chain.yaml")
    try:
        admin = psycopg.connect(DSN, connect_timeout=2, autocommit=True)
    except Exception as e:
        pytest.skip(f"postgres unreachable: {e}")
    name = "rls_" + uuid.uuid4().hex[:8]
    try:
        admin.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        admin.execute(f"CREATE SCHEMA {name}")
        admin.execute(f"SET search_path = {name}, public")
        try:
            admin.execute(comp.ddl())
        except psycopg.errors.UndefinedFile as e:
            pytest.skip(f"postgis not installed: {e}")
        admin.execute(comp.ddl())  # idempotent
        _seed(admin)
        yield name
    finally:
        admin.execute(f"DROP SCHEMA {name} CASCADE")
        admin.close()


CUR = "tstzrange(now() - interval '2 days', 'infinity')"
OLD = "tstzrange(now() - interval '20 days', now() - interval '10 days')"
MID = "tstzrange(now() - interval '10 days', 'infinity')"


def _obj(c, table, id_, cols, rng=CUR, rec=None):
    names = ["id", *cols, "valid_during", "recorded_during", "changed_by"]
    vals = [id_, *cols.values()]
    ph = ", ".join(["%s"] * len(vals))
    c.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({ph}, {rng}, {rec or rng}, 't')", vals)


def _link(c, table, src, dst):
    c.execute(f"INSERT INTO {table} (src, dst, valid_during, recorded_during, changed_by) "
              f"VALUES (%s,%s,{CUR},{CUR},'t')", (src, dst))


def _seed(c):
    I = IDS
    _obj(c, "obj_port", I["p_emea"], {"name": "Rotterdam", "region": "EMEA", "status": "open"})
    _obj(c, "obj_port", I["p_apac"], {"name": "Singapore", "region": "APAC", "status": "open"})
    # history: P_hist always EMEA (two versions); P_moved was EMEA, is now APAC
    _obj(c, "obj_port", I["p_hist"], {"name": "Hamburg", "region": "EMEA", "status": "open"}, OLD)
    _obj(c, "obj_port", I["p_hist"], {"name": "Hamburg", "region": "EMEA", "status": "closed"}, MID)
    _obj(c, "obj_port", I["p_moved"], {"name": "Moved", "region": "EMEA", "status": "open"}, OLD)
    _obj(c, "obj_port", I["p_moved"], {"name": "Moved", "region": "APAC", "status": "open"}, MID)
    _obj(c, "obj_warehouse", I["w_emea"], {"name": "W-EMEA", "region": "EMEA"})
    _obj(c, "obj_warehouse", I["w_apac"], {"name": "W-APAC", "region": "APAC"})
    _obj(c, "obj_shipment", I["s_emea"], {"ref": "S-EMEA", "status": "in_transit"})
    _obj(c, "obj_shipment", I["s_apac"], {"ref": "S-APAC", "status": "in_transit"})
    _obj(c, "obj_supplier", I["sup"], {"name": "Acme", "country": "DE"})
    _obj(c, "obj_carrier", I["car"], {"name": "Maersk"})
    _obj(c, "obj_invoice", I["inv"], {"ref": "INV-1", "amount": 10.0, "status": "open"})
    _obj(c, "obj_event", I["ev"], {"kind": "x"})
    _link(c, "link_ships_to", I["s_emea"], I["w_emea"])
    _link(c, "link_ships_to", I["s_apac"], I["w_apac"])
    _link(c, "link_ships_via", I["s_emea"], I["p_emea"])
    _link(c, "link_ships_via", I["s_emea"], I["p_apac"])  # endpoint hidden from EMEA lead
    _link(c, "link_ships_via", I["s_apac"], I["p_apac"])
    _link(c, "link_billed_by", I["inv"], I["sup"])
    _link(c, "link_ships_from", I["s_emea"], I["p_emea"])  # dst is a class (Facility)
    _link(c, "link_ships_from", I["s_emea"], I["w_apac"])


@pytest.fixture
def db(schema):
    c = psycopg.connect(DSN, options=f"-c search_path={schema},public")
    yield c
    c.rollback()
    c.close()


def act(c, role, region="EMEA"):
    c.execute("SET LOCAL ROLE amarh_app")
    if role is not None:
        c.execute("SELECT set_config('amarh.role', %s, true), set_config('amarh.region', %s, true)", (role, region))


def q(c, role, sql, region="EMEA", params=()):
    act(c, role, region)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.rollback()


def count(c, role, table, region="EMEA", where=""):
    return q(c, role, f"SELECT count(*) FROM {table} {where}", region)[0][0]


def test_unset_and_unknown_role_see_nothing(db):
    for role in (None, "", "nobody", "sim2"):
        for t in ("obj_port", "obj_invoice", "obj_shipment", "link_ships_to", "cur_obj_port", "rd_obj_supplier",
                  "rd_cur_obj_port", "cur_link_ships_via"):
            assert count(db, role, t) == 0, (role, t)


def test_unset_role_cannot_write(db):
    act(db, None)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("INSERT INTO obj_carrier (id,name,valid_during,recorded_during,changed_by) "
                   f"VALUES (gen_random_uuid(),'x',{CUR},{CUR},'t')")


def test_planner_cannot_see_invoice(db):
    assert count(db, "planner", "obj_invoice") == 0
    assert count(db, "planner", "cur_obj_invoice") == 0
    assert count(db, "planner", "rd_obj_invoice") == 0
    assert count(db, "planner", "link_billed_by") == 0  # endpoint invoice hidden
    assert count(db, "planner", "obj_port", where="WHERE id IN (SELECT id FROM obj_port)") >= 2
    assert count(db, "planner", "obj_shipment") == 2


def test_finance_sees_invoice_and_billing_link(db):
    assert count(db, "finance_analyst", "obj_invoice") == 1
    assert count(db, "finance_analyst", "link_billed_by") == 1
    assert count(db, "admin", "obj_invoice") == 1
    assert count(db, "sim", "obj_invoice") == 1


def test_regional_lead_sees_only_region(db):
    ports = {r[0] for r in q(db, "regional_ops_lead", "SELECT id FROM cur_obj_port")}
    assert ports == {IDS["p_emea"], IDS["p_hist"]}
    assert {r[0] for r in q(db, "regional_ops_lead", "SELECT id FROM obj_warehouse")} == {IDS["w_emea"]}
    assert {r[0] for r in q(db, "regional_ops_lead", "SELECT id FROM obj_shipment")} == {IDS["s_emea"]}
    assert count(db, "regional_ops_lead", "obj_invoice") == 0


def test_region_param_fails_closed(db):
    assert count(db, "regional_ops_lead", "obj_port", region="") == 0
    assert count(db, "regional_ops_lead", "obj_warehouse", region="") == 0
    assert count(db, "regional_ops_lead", "obj_shipment", region="") == 0
    assert {r[0] for r in q(db, "regional_ops_lead", "SELECT id FROM obj_port", region="APAC")} == {IDS["p_apac"], IDS["p_moved"]}


def test_links_hidden_when_endpoint_hidden(db):
    via = {(r[0], r[1]) for r in q(db, "regional_ops_lead", "SELECT src, dst FROM link_ships_via")}
    assert via == {(IDS["s_emea"], IDS["p_emea"])}  # s_emea->p_apac hidden, s_apac->* hidden
    ships_to = {(r[0], r[1]) for r in q(db, "regional_ops_lead", "SELECT src, dst FROM link_ships_to")}
    assert ships_to == {(IDS["s_emea"], IDS["w_emea"])}
    # class-typed destination (Facility = Port | Warehouse): one visible, one hidden
    frm = {(r[0], r[1]) for r in q(db, "regional_ops_lead", "SELECT src, dst FROM link_ships_from")}
    assert frm == {(IDS["s_emea"], IDS["p_emea"])}
    assert count(db, "admin", "link_ships_via") == 3
    assert count(db, "planner", "link_ships_via") == 3


def test_link_type_not_granted_to_role(db):
    assert count(db, "planner", "link_invoices_for") == 0
    _link_row = q(db, "finance_analyst", "SELECT count(*) FROM link_ships_via")[0][0]
    assert _link_row == 3


def test_history_follows_current_visibility(db):
    # P_hist: both versions visible to EMEA lead
    n = q(db, "regional_ops_lead", "SELECT count(*) FROM obj_port WHERE id = %s", params=(IDS["p_hist"],))[0][0]
    assert n == 2
    # P_moved: current version is APAC, so its old EMEA version is hidden too (D4)
    n = q(db, "regional_ops_lead", "SELECT count(*) FROM obj_port WHERE id = %s", params=(IDS["p_moved"],))[0][0]
    assert n == 0
    n = q(db, "planner", "SELECT count(*) FROM obj_port WHERE id = %s", params=(IDS["p_moved"],))[0][0]
    assert n == 2


def test_masking_via_rd_views(db):
    assert q(db, "planner", "SELECT country FROM rd_obj_supplier")[0][0] is None
    assert q(db, "planner", "SELECT country FROM rd_cur_obj_supplier")[0][0] is None
    assert q(db, "regional_ops_lead", "SELECT country FROM rd_obj_supplier")[0][0] is None
    assert q(db, "finance_analyst", "SELECT country FROM rd_obj_supplier")[0][0] == "DE"
    assert q(db, "admin", "SELECT country FROM rd_cur_obj_supplier")[0][0] == "DE"
    # non-masked columns still come through
    assert q(db, "planner", "SELECT name FROM rd_obj_supplier")[0][0] == "Acme"


def test_writes_denied_for_read_only_roles(db):
    ins = ("INSERT INTO obj_warehouse (id,name,region,valid_during,recorded_during,changed_by) "
           f"VALUES (gen_random_uuid(),'w','EMEA',{CUR},{CUR},'t')")
    for role in ("planner", "finance_analyst", "regional_ops_lead"):  # nobody but admin/sim writes Warehouse
        act(db, role)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(ins)
        db.rollback()
    act(db, "admin")
    db.execute(ins)
    db.rollback()


def test_write_denied_for_invisible_type(db):
    act(db, "planner")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("INSERT INTO obj_invoice (id,ref,amount,valid_during,recorded_during,changed_by) "
                   f"VALUES (gen_random_uuid(),'x',1,{CUR},{CUR},'t')")


def test_regional_write_limited_to_region(db):
    ins = ("INSERT INTO obj_port (id,name,region,valid_during,recorded_during,changed_by) "
           f"VALUES (gen_random_uuid(),'p',%s,{CUR},{CUR},'t')")
    act(db, "regional_ops_lead")
    db.execute(ins, ("EMEA",))
    db.rollback()
    act(db, "regional_ops_lead")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(ins, ("APAC",))
    db.rollback()


def test_update_closes_range_only_where_visible_and_writable(db):
    close = "UPDATE obj_port SET recorded_during = tstzrange(lower(recorded_during), now()) WHERE id = %s"
    # regional lead may close their own port version, cannot touch APAC (invisible: 0 rows)
    act(db, "regional_ops_lead")
    assert db.execute(close, (IDS["p_emea"],)).rowcount == 1
    db.rollback()
    act(db, "regional_ops_lead")
    assert db.execute(close, (IDS["p_apac"],)).rowcount == 0
    db.rollback()
    # finance cannot update ports it can read
    act(db, "finance_analyst")
    assert db.execute(close, (IDS["p_emea"],)).rowcount == 0
    db.rollback()


def test_link_writes(db):
    ins = f"INSERT INTO link_affects (src,dst,valid_during,recorded_during,changed_by) VALUES (%s,%s,{CUR},{CUR},'t')"
    act(db, "planner")
    db.execute(ins, (IDS["ev"], IDS["p_apac"]))
    db.rollback()
    act(db, "regional_ops_lead")
    db.execute(ins, (IDS["ev"], IDS["p_emea"]))
    db.rollback()
    act(db, "regional_ops_lead")  # APAC port invisible -> cannot link to it
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(ins, (IDS["ev"], IDS["p_apac"]))
    db.rollback()
    act(db, "finance_analyst")  # may read `affects` but writes no Event
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(ins, (IDS["ev"], IDS["p_emea"]))


def test_no_delete_or_truncate(db):
    for stmt in ("DELETE FROM obj_port", "TRUNCATE obj_port", "DELETE FROM link_ships_to"):
        act(db, "admin")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(stmt)
        db.rollback()


def test_owner_connection_unaffected(db):
    # No SET ROLE: the connection user (superuser in dev) sees everything, even with FORCE RLS.
    assert db.execute("SELECT count(*) FROM obj_invoice").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM obj_port").fetchone()[0] == 6


def test_catalog_flags(db):
    rows = db.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = current_schema()::regnamespace AND relkind = 'r' AND (relname LIKE 'obj\\_%' OR relname LIKE 'link\\_%')"
    ).fetchall()
    assert len(rows) >= 15 and all(r[1] and r[2] for r in rows), rows
    views = db.execute("SELECT c.relname, c.reloptions FROM pg_class c WHERE c.relnamespace = current_schema()::regnamespace "
                       "AND c.relkind = 'v' AND (c.relname LIKE 'cur\\_%' OR c.relname LIKE 'rd\\_%')").fetchall()
    assert views and all("security_invoker=true" in (v[1] or []) for v in views), views
    role = db.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = 'amarh_app'").fetchone()
    assert role == (False, False, False)


def test_concurrent_role_creation_is_safe(schema):
    block = compile_ontology(ROOT / "ontology" / "supply_chain.yaml").policy_ddl().split("DO $$ BEGIN IF NOT has_schema")[0]
    name = "amarh_conc_" + uuid.uuid4().hex[:6]
    block = block.replace("amarh_app", name)  # same block, throwaway role name
    errs = []

    def run():
        try:
            c = psycopg.connect(DSN, autocommit=True)
            c.execute(block)
            c.close()
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = psycopg.connect(DSN, autocommit=True)
    c.execute(f"DROP ROLE IF EXISTS {name}")
    c.close()
    assert not errs
