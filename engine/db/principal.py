"""Per-request identity for Postgres row-level security (see docs/contracts.md, Phase 3)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Principal:
    role: str
    user: str
    region: str = ""
    # actions this role may approve (feeds proposal visibility policy); optional extension of the contract
    approves: tuple[str, ...] = ()


class _Owner:
    def __repr__(self) -> str:
        return "OWNER"


OWNER = _Owner()  # sentinel: loader/migration code running as the connection user


@contextmanager
def scoped(conn: psycopg.Connection, principal, *, statement_timeout_ms: int = 5000,
           lock_timeout_ms: int = 2000):
    """Run the block as `principal` inside the current transaction (SET LOCAL ROLE amarh_app).

    The caller must be in a transaction (`with conn.transaction():` or an implicit one); the
    identity is transaction-local so it can never leak to the next transaction on a reused
    connection. On exit the role is reset (so code after the block, in the same transaction,
    runs as the owner again). OWNER does nothing.
    """
    if principal is OWNER:
        yield conn
        return
    if conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("scoped() requires an open transaction (conn.transaction())")
    conn.execute("SET LOCAL ROLE amarh_app")
    conn.execute(
        "SELECT set_config('amarh.role', %s, true), set_config('amarh.user', %s, true), "
        "set_config('amarh.region', %s, true), set_config('amarh.approves', %s, true), "
        "set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)",
        [principal.role, principal.user, principal.region or "", ",".join(principal.approves),
         f"{int(statement_timeout_ms)}ms", f"{int(lock_timeout_ms)}ms"])
    try:
        yield conn
    finally:
        # in an aborted transaction nothing can run; the identity dies with the rollback
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.INERROR:
            conn.execute("RESET ROLE")
            conn.execute(
                "SELECT set_config('amarh.role', '', true), set_config('amarh.user', '', true), "
                "set_config('amarh.region', '', true), set_config('amarh.approves', '', true), "
                "set_config('statement_timeout', '0', true), set_config('lock_timeout', '0', true)")


def current_principal(conn: psycopg.Connection) -> Principal | None:
    """The identity currently set on the connection (None when no role is set). For tests."""
    role, user, region, appr = conn.execute(
        "SELECT current_setting('amarh.role', true), current_setting('amarh.user', true), "
        "current_setting('amarh.region', true), current_setting('amarh.approves', true)").fetchone()
    if not role:
        return None
    return Principal(role, user or "", region or "", tuple(a for a in (appr or "").split(",") if a))
