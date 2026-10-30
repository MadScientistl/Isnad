"""One entry point to create/upgrade a schema: governance DDL + the compiler's build/schema.sql."""
from __future__ import annotations

from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_SQL = Path(__file__).with_name("governance.sql")
BUILD_SCHEMA_SQL = ROOT / "build" / "schema.sql"


def apply_schema(conn: psycopg.Connection, schema_sql_text: str | None = None) -> None:
    """Apply governance.sql then the compiler DDL to the connection's current search_path schema.

    Both are idempotent. Governance goes first so the `isnad_app` role and the append-only
    function exist before the compiler's grants/policies reference them. Commits nothing when
    the caller is inside a transaction block; in autocommit mode each statement script commits.
    """
    conn.execute(GOVERNANCE_SQL.read_text())
    text = schema_sql_text if schema_sql_text is not None else BUILD_SCHEMA_SQL.read_text()
    conn.execute(text)
    from engine import versioning
    versioning.apply(conn)
    from . import bitemporal
    bitemporal.clear_caches()
