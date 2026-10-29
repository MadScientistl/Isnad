from __future__ import annotations

import time
from datetime import datetime, UTC
from pathlib import Path

import psycopg
from psycopg import sql

from engine.db import connect, get_dsn
from engine.db import bitemporal as bt
from engine.db.setup import apply_schema

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "build" / "schema.sql"
VALID_FROM = datetime(2020, 1, 1, tzinfo=UTC)


def reset_schema(name: str = "bench") -> None:
    with psycopg.connect(get_dsn(), autocommit=True) as a:
        a.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
        a.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        a.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        a.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        apply_schema(a, SCHEMA_SQL.read_text())


def load(ds, schema: str = "bench") -> dict:
    conn = connect(schema=schema)
    t0 = time.perf_counter()
    for t, rows in ds.objects.items():
        bt.bulk_load_objects(conn, t, rows, VALID_FROM, "bench")
    t1 = time.perf_counter()
    for l, rows in ds.links.items():
        bt.bulk_load_links(conn, l, rows, VALID_FROM, "bench")
    conn.commit()
    t2 = time.perf_counter()
    conn.execute("ANALYZE"); conn.commit()
    conn.close()
    return {"objects_s": t1 - t0, "links_s": t2 - t1, "total_s": t2 - t0,
            "objects": ds.n_objects, "edges": ds.n_edges}
