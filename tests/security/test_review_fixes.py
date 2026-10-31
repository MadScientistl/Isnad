"""Regression tests for the Phase 3 security review (F2, F3, F4)."""
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).parent))
import conftest as C  # noqa: E402
from oracle import Oracle  # noqa: E402
from engine.db import bitemporal as bt, connect  # noqa: E402
from engine.db.principal import scoped  # noqa: E402

LEAD = C.PRINCIPALS["regional_ops_lead"]


@pytest.fixture(scope="module")
def hist():
    """A port that is EMEA now but was APAC earlier (old name is the secret), plus a Warehouse likewise."""
    admin, name = C.make_schema("rf")
    c = connect(schema=name)
    types, props = {}, {}
    ids = {}
    for key, typ, old, new in (("port", "Port", {"name": "OLD-APAC-SECRET", "region": "APAC", "status": "open"},
                                {"name": "NowEMEA", "region": "EMEA", "status": "open"}),
                               ("wh", "Warehouse", {"name": "OLD-WH-SECRET", "region": "APAC"},
                                {"name": "NowWh", "region": "EMEA"})):
        i = uuid.uuid4()
        bt.write_object(c, typ, i, old, C.T0, "seed")
        bt.write_object(c, typ, i, new, C.NOW - timedelta(days=1), "seed")
        ids[key], types[i], props[i] = i, typ, new
    c.commit()
    c.close()
    yield name, ids, types
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


def test_d4_version_rows_must_satisfy_the_filter_themselves(hist):
    name, ids, types = hist
    past = C.NOW - timedelta(days=3)
    orc = Oracle(types, {i: {"region": "EMEA"} for i in types}, [])
    with C.as_role(name, "regional_ops_lead") as c:
        for key, typ in (("port", "Port"), ("wh", "Warehouse")):
            i = ids[key]
            h = bt.history(c, typ, i)
            assert [r["region"] for r in h] == ["EMEA"]            # APAC version row invisible
            assert all("SECRET" not in str(r) for r in h)
            assert bt.get_object(c, typ, i, as_of_valid=past) is None
            assert i not in [r["id"] for r in bt.list_objects(c, typ, past)]
            for tbl in (f"obj_{typ.lower()}", f"rd_obj_{typ.lower()}"):
                regs = [r[0] for r in c.execute(f"SELECT region FROM {tbl} WHERE id=%s", [i]).fetchall()]
                assert regs == ["EMEA"]
            # the oracle agrees: current version visible, old one not
            assert orc.version_visible("regional_ops_lead", i, {"region": "EMEA"})
            assert not orc.version_visible("regional_ops_lead", i, {"region": "APAC"})
    with C.as_role(name, "planner") as c:  # roles without `where` are unchanged: full history
        assert "APAC" in [r["region"] for r in bt.history(c, "Port", ids["port"])]
        assert bt.get_object(c, "Port", ids["port"], as_of_valid=past)["name"] == "OLD-APAC-SECRET"
    with C.as_role(name, "admin") as c:
        assert len(bt.history(c, "Port", ids["port"])) >= 2


def test_d4_geo_and_stats_paths_use_filtered_rows(hist):
    name, ids, _ = hist
    past = C.NOW - timedelta(days=3)
    with C.as_role(name, "regional_ops_lead") as c:
        rows = bt.list_objects(c, "Warehouse", past)
        assert [r for r in rows if "SECRET" in str(r)] == []


# ---------------------------------------------------------------- F4 (partial)
def test_definer_helpers_have_pg_temp_last(hist):
    name, _, _ = hist
    c = connect(schema=name)
    cfgs = [r[0] for r in c.execute(
        "SELECT proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND proname LIKE 'isnad\\_visible\\_%'").fetchall()]
    c.close()
    assert cfgs
    for cfg in cfgs:
        sp = [x for x in cfg if x.startswith("search_path=")][0].split("=", 1)[1].split(", ")
        assert sp[0] == "pg_catalog" and sp[-1] == "pg_temp", sp


def test_isnad_app_cannot_create_temp_tables(hist):
    name, _, _ = hist
    with C.as_role(name, "regional_ops_lead") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("CREATE TEMP TABLE obj_port (id uuid)")


# ------------------------------------------------- known limitations (tracked, see docs/contracts.md)
@pytest.mark.xfail(strict=True, reason="KNOWN LIMITATION (F4): isnad.* identity GUCs are trusted inside a "
                                       "scoped session; arbitrary SQL could set_config them. Signed identity "
                                       "context is deferred.")
def test_limitation_set_config_forges_identity(hist):
    name, _, _ = hist
    with C.as_role(name, "regional_ops_lead") as c:
        c.execute("SELECT set_config('isnad.role', 'admin', true)")
        # desired: forging is impossible / sees nothing extra
        assert c.execute("SELECT count(*) FROM obj_port WHERE region <> 'EMEA'").fetchone()[0] == 0 \
            and c.execute("SELECT current_setting('isnad.role')").fetchone()[0] == "regional_ops_lead"


@pytest.mark.xfail(strict=True, reason="KNOWN LIMITATION (F4): the connection user can RESET ROLE out of "
                                       "isnad_app inside a scoped session if SQL execution is exposed.")
def test_limitation_reset_role_escapes_rls(hist):
    name, _, _ = hist
    with C.as_role(name, "regional_ops_lead") as c:
        with pytest.raises(psycopg.errors.Error):
            c.execute("RESET ROLE")
            c.execute("SELECT 1 FROM obj_warehouse").fetchall()
            raise AssertionError("RESET ROLE succeeded and read the owner's rows")


def test_read_source_refuses_base_table_when_scoped_and_view_missing(hist):
    name, _, _ = hist
    with C.as_role(name, "regional_ops_lead") as c:
        key = (bt._sp(c), "rd_obj_port")
        old = bt._rd_cache.get(key)
        bt._rd_cache[key] = False  # simulate a missing masking view
        try:
            with pytest.raises(LookupError):
                bt.read_source(c, "obj_port")
            assert bt.read_source(c, "obj_port", masked=False) == "obj_port"
        finally:
            if old is None:
                bt._rd_cache.pop(key, None)
            else:
                bt._rd_cache[key] = old
