import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_helpers import link, obj  # noqa: E402
from engine.db import connect, get_dsn  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402



@pytest.fixture
def aschema():
    name = f"ag_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    apply_schema(admin)
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(aschema):
    c = connect(schema=aschema)
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def factory(aschema):
    return lambda: connect(schema=aschema)


@pytest.fixture
def world(conn):
    w = {}
    w["emea_port"] = obj(conn, "Port", {"name": "Rotterdam", "status": "open", "region": "EMEA"})
    w["apac_port"] = obj(conn, "Port", {"name": "Singapore", "status": "open", "region": "APAC"})
    w["emea_wh"] = obj(conn, "Warehouse", {"name": "WH Hamburg", "region": "EMEA"})
    w["apac_wh"] = obj(conn, "Warehouse", {"name": "WH Sydney", "region": "APAC"})
    w["carrier"] = obj(conn, "Carrier", {"name": "MaerskLike"})
    w["s_emea"] = obj(conn, "Shipment", {"ref": "SH-EMEA", "status": "in_transit", "cargo_value": 500.0})
    w["s_apac"] = obj(conn, "Shipment", {"ref": "SH-APAC", "status": "in_transit"})
    link(conn, "ships_to", w["s_emea"], w["emea_wh"])
    link(conn, "ships_to", w["s_apac"], w["apac_wh"])
    link(conn, "ships_via", w["s_emea"], w["emea_port"])
    link(conn, "ships_via", w["s_apac"], w["apac_port"])
    w["ev"] = obj(conn, "Event", {"kind": "port_closure", "severity": "high", "note": "storm"})
    link(conn, "affects", w["ev"], w["emea_port"])
    w["sup_a"] = obj(conn, "Supplier", {"name": "Acme", "country": "NL"})
    w["sup_b"] = obj(conn, "Supplier", {"name": "Bolt", "country": "DE"})
    w["sup_c"] = obj(conn, "Supplier", {"name": "Cog", "country": "FR"})
    link(conn, "supplies", w["sup_a"], w["sup_b"])
    link(conn, "supplies", w["sup_b"], w["sup_c"])
    w["inv"] = obj(conn, "Invoice", {"ref": "INV-1", "amount": 100.0, "status": "open"})
    w["inv_other"] = obj(conn, "Invoice", {"ref": "INV-2", "amount": 250.0, "status": "open"})
    link(conn, "billed_by", w["inv"], w["sup_a"])
    link(conn, "billed_by", w["inv_other"], w["sup_a"])
    return w
