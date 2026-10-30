import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from scn_helpers import owner_link, owner_obj  # noqa: E402
from engine.db import connect, get_dsn  # noqa: E402
from engine.db.setup import apply_schema  # noqa: E402


@pytest.fixture
def aschema():
    """Fresh schema per test (governance tables are append-only: no TRUNCATE between tests)."""
    name = f"scn_{uuid.uuid4().hex[:10]}"
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
def world(conn):
    """EMEA/APAC ports + warehouses, two shipments (cargo 1000 / 500) via the EMEA / APAC port, supplier,
    an open invoice for the EMEA shipment."""
    w = {}
    w["emea_port"] = owner_obj(conn, "Port", {"name": "Rotterdam", "status": "open", "region": "EMEA"})
    w["apac_port"] = owner_obj(conn, "Port", {"name": "Singapore", "status": "open", "region": "APAC"})
    w["emea_wh"] = owner_obj(conn, "Warehouse", {"name": "WH Hamburg", "region": "EMEA"})
    w["apac_wh"] = owner_obj(conn, "Warehouse", {"name": "WH Sydney", "region": "APAC"})
    w["supplier"] = owner_obj(conn, "Supplier", {"name": "Acme", "country": "NL"})
    w["ship"] = owner_obj(conn, "Shipment", {"ref": "SH1", "status": "in_transit", "cargo_value": 1000.0})
    w["ship2"] = owner_obj(conn, "Shipment", {"ref": "SH2", "status": "in_transit", "cargo_value": 500.0})
    owner_link(conn, "ships_to", w["ship"], w["emea_wh"])
    owner_link(conn, "ships_to", w["ship2"], w["apac_wh"])
    owner_link(conn, "ships_via", w["ship"], w["emea_port"])
    owner_link(conn, "ships_via", w["ship2"], w["apac_port"])
    w["inv"] = owner_obj(conn, "Invoice", {"ref": "INV-1", "amount": 100.0, "status": "open"})
    owner_link(conn, "invoices_for", w["inv"], w["ship"])
    owner_link(conn, "billed_by", w["inv"], w["supplier"])
    return w
