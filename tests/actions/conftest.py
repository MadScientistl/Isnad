import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.actions import ensure_audit_schema
from engine.db import connect, get_dsn


@pytest.fixture(scope="session")
def aschema():
    name = f"act_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
    admin.execute((ROOT / "build" / "schema.sql").read_text())
    c = connect(schema=name)
    ensure_audit_schema(c)
    ensure_audit_schema(c)  # idempotent
    c.close()
    yield name
    admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
    admin.close()


@pytest.fixture
def conn(aschema):
    c = connect(schema=aschema)
    yield c
    c.rollback()
    c.execute("TRUNCATE obj_shipment, obj_port, obj_warehouse, obj_supplier, obj_invoice, obj_event, "
              "link_affects, link_billed_by, link_invoices_for, link_ships_from, link_ships_to, "
              "link_ships_via, link_operated_by, obj_carrier, graph_outbox, audit_event")
    c.commit()
    c.close()
