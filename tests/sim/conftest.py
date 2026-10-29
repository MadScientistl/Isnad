import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.actions import apply_action, ensure_audit_schema
from engine.db import connect, get_dsn

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
PORTS = [("Rotterdam", 4.48, 51.95), ("Singapore", 103.8, 1.26), ("Shanghai", 121.8, 31.2),
         ("Los Angeles", -118.25, 33.73), ("Dubai", 55.06, 25.01), ("Santos", -46.3, -23.98)]
WAREHOUSES = [("WH Hamburg", 10.0, 53.55), ("WH Chicago", -87.63, 41.88),
              ("WH Mumbai", 72.88, 19.07), ("WH Sydney", 151.2, -33.87)]


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def seed_entities(conn):
    ids = {"ports": [], "warehouses": [], "carriers": [], "suppliers": []}

    def mk(t, props):
        # deterministic ids: the digest depends on entity ids (sim sorts candidates by id)
        i = uuid.uuid5(uuid.NAMESPACE_URL, f"sim-test:{t}:{props['name']}")
        r = apply_action(conn, "create_object", {"type": t, "id": i, "props": props}, "seed", valid_at=EPOCH,
                         source="seed")
        return r.created[t]

    for n, lo, la in PORTS:
        ids["ports"].append(mk("Port", {"name": n, "lon": lo, "lat": la, "location": _pt(lo, la),
                                        "status": "open"}))
    for n, lo, la in WAREHOUSES:
        ids["warehouses"].append(mk("Warehouse", {"name": n, "lon": lo, "lat": la, "location": _pt(lo, la)}))
    for i in range(3):
        ids["carriers"].append(mk("Carrier", {"name": f"Carrier {i}"}))
    for i in range(5):
        ids["suppliers"].append(mk("Supplier", {"name": f"Supplier {i}", "country": "XX"}))
    return ids


class Db:
    """Factory for fresh scratch schemas, all dropped at session end."""

    def __init__(self):
        try:
            self.admin = psycopg.connect(get_dsn(), autocommit=True)
        except psycopg.OperationalError as e:
            pytest.skip(f"postgres unreachable: {e}")
        self.names = []

    def new(self, seeded=True):
        name = f"sim_{uuid.uuid4().hex[:10]}"
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        self.admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        self.admin.execute((ROOT / "build" / "schema.sql").read_text())
        self.names.append(name)
        c = connect(schema=name)
        ensure_audit_schema(c)
        ids = seed_entities(c) if seeded else None
        c.close()
        return name, ids

    def close(self):
        for n in self.names:
            self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(n)))
        self.admin.close()


@pytest.fixture(scope="session")
def db():
    d = Db()
    yield d
    d.close()
