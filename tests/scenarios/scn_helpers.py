import uuid
from datetime import UTC, datetime, timedelta

from psycopg import sql

from engine.actions import make_principal
from engine.db import bitemporal as bt

T0 = datetime(2026, 1, 1, tzinfo=UTC)

PLANNER = make_principal("planner", "pat")
PLANNER2 = make_principal("planner", "paul")
FIN = make_principal("finance_analyst", "fiona")
LEAD = make_principal("regional_ops_lead", "lena")
ADMIN = make_principal("admin", "adam")


def now():
    return datetime.now(UTC)


def owner_obj(conn, type_, props, t=T0, id_=None):
    i = id_ or uuid.uuid4()
    bt.write_object(conn, type_, i, props, t, "setup")
    conn.commit()
    return i


def owner_link(conn, link, src, dst, t=T0):
    bt.write_link(conn, link, src, dst, t, "setup")
    conn.commit()


def checksum(conn, tables=("obj_port", "obj_warehouse", "obj_shipment", "obj_invoice", "obj_supplier", "obj_event",
                           "link_ships_via", "link_ships_to", "link_affects", "link_invoices_for",
                           "link_billed_by")) -> str:
    """md5 of every MAIN row (scenario_id = 0) of the data tables + the main audit/proposal/outbox rows."""
    conn.rollback()
    parts = []
    for t in tables:
        parts.append(conn.execute(sql.SQL("SELECT coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '') "
                                          "FROM {} x WHERE scenario_id = 0").format(sql.Identifier(t))).fetchone()[0])
    parts.append(str(conn.execute("SELECT count(*) FROM audit_event WHERE scenario_id = 0").fetchone()[0]))
    parts.append(str(conn.execute("SELECT count(*) FROM proposal").fetchone()[0]))
    parts.append(str(conn.execute("SELECT count(*) FROM webhook_outbox").fetchone()[0]))
    parts.append(str(conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0]))
    conn.rollback()
    return "|".join(parts)
