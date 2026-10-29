import uuid
from datetime import UTC, datetime

from engine.actions import SIM, make_principal  # noqa: F401
from engine.db import bitemporal as bt

T0 = datetime(2026, 1, 1, tzinfo=UTC)

PLANNER = make_principal("planner", "pat")
FIN = make_principal("finance_analyst", "fiona")
FIN2 = make_principal("finance_analyst", "finn")
LEAD = make_principal("regional_ops_lead", "lena")
ADMIN = make_principal("admin", "adam")
ADMIN2 = make_principal("admin", "ada")


def owner_obj(conn, type_, props, t=T0, id_=None):
    """Setup helper: write an object as the owner/loader (no audit), like the seed."""
    i = id_ or uuid.uuid4()
    bt.write_object(conn, type_, i, props, t, "setup")
    conn.commit()
    return i


def owner_link(conn, link, src, dst, t=T0):
    bt.write_link(conn, link, src, dst, t, "setup")
    conn.commit()


