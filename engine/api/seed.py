"""python -m engine.api.seed: apply build/schema.sql and load a demo dataset (idempotent-ish: skips if data exists)."""
from __future__ import annotations

import os
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path

from engine.db import bitemporal as bt
from engine.db import connect

NS = uuid.UUID("6f1d1c3e-0000-4000-8000-00000000a11a")
ROOT = Path(__file__).resolve().parents[2]


def _id(kind: str, i) -> uuid.UUID:
    return uuid.uuid5(NS, f"{kind}:{i}")


def seed(conn, schema_sql: str | None = None) -> dict:
    if schema_sql is None:
        schema_sql = (ROOT / "build" / "schema.sql").read_text()
    conn.execute(schema_sql)
    conn.commit()
    if bt.list_objects(conn, "Supplier"):
        return {"skipped": True}
    rnd = random.Random(42)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    by = "seed"
    ports = [("Rotterdam", 51.95, 4.14), ("Shanghai", 31.23, 121.47), ("Singapore", 1.26, 103.84),
             ("Los Angeles", 33.74, -118.27), ("Hamburg", 53.54, 9.97), ("Dubai", 25.01, 55.06)]
    whs = [("Venlo DC", 51.37, 6.17), ("Chicago DC", 41.88, -87.63), ("Shenzhen Hub", 22.54, 114.06)]
    carriers = ["Maersk", "MSC", "COSCO", "Hapag-Lloyd"]
    countries = ["CN", "DE", "NL", "US", "VN", "IN", "MX", "PL"]
    n = {k: 0 for k in ("Supplier", "Port", "Warehouse", "Carrier", "Shipment")}

    def put(type_, key, props):
        bt.write_object(conn, type_, _id(type_, key), props, t0, by)
        n[type_] += 1
        return _id(type_, key)

    P = [put("Port", p[0], {"name": p[0], "lat": p[1], "lon": p[2],
                            "status": "closed" if p[0] == "Dubai" else "open"}) for p in ports]
    for w in whs:
        put("Warehouse", w[0], {"name": w[0], "lat": w[1], "lon": w[2]})
    C = [put("Carrier", c, {"name": c}) for c in carriers]
    S = [put("Supplier", i, {"name": f"Supplier {i:02d}", "country": countries[i % len(countries)]})
         for i in range(20)]
    SH = [put("Shipment", i, {"ref": f"SHP-{1000+i}", "status": rnd.choice(["planned", "in_transit", "delayed"]),
                              "eta": datetime(2026, rnd.randint(2, 9), rnd.randint(1, 28), tzinfo=UTC)})
          for i in range(15)]
    links = 0

    def link(l, a, b, st, dt):
        nonlocal links
        bt.write_link(conn, l, a, b, t0, by, src_type=st, dst_type=dt)
        links += 1

    # supply chain: S0 -> S1 -> S2 -> S3 -> S4 plus a tree below it
    for i in range(4):
        link("supplies", S[i], S[i + 1], "Supplier", "Supplier")
    for i in range(5, 20):
        link("supplies", S[rnd.randrange(0, i)], S[i], "Supplier", "Supplier")
    for s in SH:
        link("operated_by", s, rnd.choice(C), "Shipment", "Carrier")
        for p in rnd.sample(P, 2):
            link("ships_via", s, p, "Shipment", "Port")
    conn.commit()
    return {"objects": sum(n.values()), "links": links, **n}


def main() -> None:
    schema = os.environ.get("AMARH_SCHEMA")
    conn = connect(schema=schema)
    if schema:
        from psycopg import sql
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.commit()
    print(seed(conn))


if __name__ == "__main__":
    main()
