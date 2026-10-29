"""python -m engine.api.seed: apply the schema (governance + compiler DDL) and load the demo base data.

Runs as the OWNER/loader (writes with `bt.*` directly, no role) and is audited as ONE `seed`
execution (`audit_event` name='seed', source='seed', params = counts), inserted once per data epoch
(a `reset` starts a new one). Idempotent and non-destructive: every entity has a deterministic uuid5 id;
missing entities are written at the sim epoch, existing entities without a `location` / `region` are
rewritten with one (a bitemporal correction at the epoch), links are only written when absent. Nothing is
deleted. `region` (APAC|EMEA|AMER) is assigned to ports and warehouses by longitude.
No shipments are created: the simulation does that through engine.actions.execute.
"""
from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql

from psycopg.types.json import Jsonb

from engine.db import bitemporal as bt
from engine.db import connect
from engine.db.setup import apply_schema as _apply_governed_schema

NS = uuid.UUID("6f1d1c3e-0000-4000-8000-00000000a11a")
ROOT = Path(__file__).resolve().parents[2]
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

PORTS = [("Rotterdam", 51.95, 4.14), ("Shanghai", 31.23, 121.47), ("Singapore", 1.26, 103.84),
         ("Los Angeles", 33.74, -118.27), ("Hamburg", 53.54, 9.97), ("Dubai", 25.01, 55.06),
         ("Antwerp", 51.27, 4.40), ("Busan", 35.10, 129.04), ("Santos", -23.96, -46.30),
         ("Durban", -29.87, 31.03), ("Mumbai", 18.95, 72.95), ("Savannah", 32.08, -81.09)]
WAREHOUSES = [("Venlo DC", 51.37, 6.17), ("Chicago DC", 41.88, -87.63), ("Shenzhen Hub", 22.54, 114.06),
              ("Atlanta DC", 33.75, -84.39), ("Pune Hub", 18.52, 73.86), ("Sao Paulo DC", -23.55, -46.63)]
CARRIERS = ["Maersk", "MSC", "COSCO", "Hapag-Lloyd", "CMA CGM", "Evergreen"]
COUNTRIES = ["CN", "DE", "NL", "US", "VN", "IN", "MX", "PL"]
N_SUPPLIERS = 30
CHAINS = [(0, 1, 2, 3, 4), (5, 6, 7, 8)]


def _id(kind: str, i) -> uuid.UUID:
    return uuid.uuid5(NS, f"{kind}:{i}")


def _point(lat: float, lon: float) -> dict:
    return {"type": "Point", "coordinates": [lon, lat]}


def _migrate_columns(conn, schema_sql: str) -> int:
    """Add columns that build/schema.sql defines but older tables lack (e.g. Phase 1 demo schema).

    The target schema is applied into a throwaway schema (rolled back) and compared column by column.
    """
    tmp = f"_seedcmp_{uuid.uuid4().hex[:8]}"
    try:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(tmp)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(tmp)))
        conn.execute(schema_sql)
        want = conn.execute(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY c.relname, a.attnum", [tmp]).fetchall()
    finally:
        conn.rollback()
    return _apply_missing(conn, want)


def _apply_missing(conn, want) -> int:
    added = 0
    have = {(r[0], r[1]) for r in conn.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = to_regnamespace(current_schema()) AND c.relkind = 'r' "
        "AND a.attnum > 0 AND NOT a.attisdropped").fetchall()}
    tables = {t for t, _ in have}
    for t, col, typ in want:
        if t in tables and (t, col) not in have:
            conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + typ).format(
                sql.Identifier(t), sql.Identifier(col)))
            added += 1
    conn.commit()
    return added


def region_for(lon: float) -> str:
    """APAC / EMEA / AMER by longitude (D8)."""
    return "AMER" if lon < -30 else "EMEA" if lon < 60 else "APAC"


def apply_schema(conn, schema_sql: str | None = None) -> dict:
    if schema_sql is None:
        schema_sql = (ROOT / "build" / "schema.sql").read_text()
    conn.commit()
    added = _migrate_columns(conn, schema_sql)
    _apply_governed_schema(conn, schema_sql)
    conn.commit()
    return {"columns_added": added}


def seed(conn, schema_sql: str | None = None) -> dict:
    info = apply_schema(conn, schema_sql)
    by = "seed"
    stats = {"written": 0, "backfilled": 0, "links": 0}

    def put(type_, key, props, location=None):
        id_ = _id(type_, key)
        cur = bt.get_object(conn, type_, id_, as_of_valid=EPOCH, masked=False)
        if cur is None:
            cur = bt.get_object(conn, type_, id_, masked=False)
        if cur is None:
            bt.write_object(conn, type_, id_, {**props, **({"location": location} if location else {})},
                            EPOCH, by)
            stats["written"] += 1
        elif location is not None and (cur.get("location") is None or
                                       ("region" in props and cur.get("region") is None)):
            keep = {k: v for k, v in cur.items() if k in props or k == "status" or k == "location"}
            keep.update({k: v for k, v in props.items() if keep.get(k) is None})
            keep["location"] = location
            bt.write_object(conn, type_, id_, keep, EPOCH, by)
            stats["backfilled"] += 1
        return id_

    for name, lat, lon in PORTS:
        put("Port", name, {"name": name, "lat": lat, "lon": lon, "status": "open",
                                  "region": region_for(lon)}, _point(lat, lon))
    for name, lat, lon in WAREHOUSES:
        put("Warehouse", name, {"name": name, "lat": lat, "lon": lon, "region": region_for(lon)},
            _point(lat, lon))
    for c in CARRIERS:
        put("Carrier", c, {"name": c})
    S = [put("Supplier", i, {"name": f"Supplier {i:02d}", "country": COUNTRIES[i % len(COUNTRIES)]})
         for i in range(N_SUPPLIERS)]

    def link_if_absent_in(child: uuid.UUID, parent: uuid.UUID):
        """supplies edge parent -> child, only when child has no supplier yet (keeps reruns stable)."""
        if bt.neighbours(conn, "supplies", child, "in", as_of_valid=EPOCH) or \
                bt.neighbours(conn, "supplies", child, "in"):
            return
        if bt.write_link(conn, "supplies", parent, child, EPOCH, by, src_type="Supplier", dst_type="Supplier"):
            stats["links"] += 1

    in_chain = set()
    for chain in CHAINS:
        for a, b in zip(chain, chain[1:]):
            link_if_absent_in(S[b], S[a])
            in_chain.add(b)
    rnd = random.Random(42)
    for i in range(1, N_SUPPLIERS):
        parent = rnd.randrange(0, i)
        if i not in in_chain and not any(i == c[0] for c in CHAINS):
            link_if_absent_in(S[i], S[parent])
    conn.commit()
    ports = len(bt.list_objects(conn, "Port", masked=False))
    out = {**info, **stats, "ports": ports,
           "warehouses": len(bt.list_objects(conn, "Warehouse", masked=False)),
           "carriers": len(bt.list_objects(conn, "Carrier", masked=False)),
           "suppliers": len(bt.list_objects(conn, "Supplier", masked=False))}
    # ONE audited `seed` execution per data epoch (a `reset` event starts a new epoch)
    if not conn.execute(
            "SELECT 1 FROM audit_event WHERE name='seed' AND "
            "seq > coalesce((SELECT max(seq) FROM audit_event WHERE name='reset'), 0)").fetchone():
        conn.execute(
            "INSERT INTO audit_event (action_id, name, actor, source, params, sim_time, role, status) "
            "VALUES (%s,'seed','seed','seed',%s,%s,'owner','executed')",
            [uuid.uuid4(), Jsonb(out), EPOCH])
    conn.commit()
    return out


def main() -> None:
    schema = os.environ.get("AMARH_SCHEMA")
    conn = connect(schema=schema)
    if schema:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.commit()
    print(seed(conn))


if __name__ == "__main__":
    main()
