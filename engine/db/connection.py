"""Connection helper. DSN comes from AMARH_DSN, defaulting to the contract DSN."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
import psycopg.types.datetime
from psycopg import sql

DEFAULT_DSN = "postgresql://amarh:amarh@localhost:55441/amarh"


class _InfTimestamptz(psycopg.types.datetime.TimestamptzLoader):
    """Load 'infinity' as datetime.max (UTC) instead of raising."""

    def load(self, data):
        if bytes(data) == b"infinity":
            return datetime.max.replace(tzinfo=timezone.utc)
        return super().load(data)


def get_dsn() -> str:
    return os.environ.get("AMARH_DSN", DEFAULT_DSN)


def connect(dsn: str | None = None, schema: str | None = None, **kw) -> psycopg.Connection:
    """Open a psycopg3 connection (transactional, not autocommit).

    If `schema` is given it is put first on the search_path (session level).
    """
    conn = psycopg.connect(dsn or get_dsn(), **kw)
    conn.adapters.register_loader("timestamptz", _InfTimestamptz)
    if schema:
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.commit()
    return conn
