"""Per-request identity for Postgres row-level security (see docs/contracts.md, Phase 3)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(frozen=True)
class Principal:
    role: str
    user: str
    region: str = ""
    # actions this role may approve (feeds proposal visibility policy); optional extension of the contract
    approves: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioCtx:
    """A resolved scenario (Phase 5): ONLY built server-side from the `scenario` table after a visibility
    check (`engine.scenarios.resolve`); never from a client value."""
    id: int
    base_recorded: datetime
    clock: datetime | None = None

    def __post_init__(self):
        if type(self.id) is not int or self.id <= 0:
            raise ValueError("ScenarioCtx.id must be a positive int")
        if not isinstance(self.base_recorded, datetime):
            raise TypeError("ScenarioCtx.base_recorded must be a datetime")
        if self.clock is not None and not isinstance(self.clock, datetime):
            raise TypeError("ScenarioCtx.clock must be a datetime or None")


class _Owner:
    def __repr__(self) -> str:
        return "OWNER"


OWNER = _Owner()  # sentinel: loader/migration code running as the connection user


_SCN_RESET = ("SELECT set_config('amarh.scenario', '', true), set_config('amarh.scenario_base', '', true), "
              "set_config('amarh.now', '', true)")


def _scn_values(scenario) -> tuple[str, str, str]:
    if scenario is None:
        return "", "", ""
    if not isinstance(scenario, ScenarioCtx):
        raise TypeError(f"scenario must be a ScenarioCtx or None, got {type(scenario).__name__}")
    return (str(scenario.id), scenario.base_recorded.isoformat(),
            scenario.clock.isoformat() if scenario.clock is not None else "")


@contextmanager
def scoped(conn: psycopg.Connection, principal, scenario: ScenarioCtx | None = None, *,
           statement_timeout_ms: int = 5000, lock_timeout_ms: int = 2000):
    """Run the block as `principal` inside the current transaction (SET LOCAL ROLE amarh_app).

    The caller must be in a transaction (`with conn.transaction():` or an implicit one); the
    identity is transaction-local so it can never leak to the next transaction on a reused
    connection. On exit the role is reset (so code after the block, in the same transaction,
    runs as the owner again). OWNER does nothing, except that with a `scenario` it sets (and on
    exit clears) the scenario GUCs without switching role.

    `scenario` (Phase 5) selects the what-if overlay: `amarh.scenario`, `amarh.scenario_base` and
    `amarh.now` (the scenario clock, '' = wall clock) are set next to the identity and cleared on exit;
    with `scenario=None` they are explicitly set to '' (= main). Only a `ScenarioCtx` is accepted.
    """
    scn_vals = _scn_values(scenario)
    if principal is OWNER:
        if scenario is None:
            yield conn
            return
        conn.execute("SELECT set_config('amarh.scenario', %s, true), set_config('amarh.scenario_base', %s, true), "
                     "set_config('amarh.now', %s, true)", list(scn_vals))
        try:
            yield conn
        finally:
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.INERROR:
                conn.execute(_SCN_RESET)
        return
    if conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("scoped() requires an open transaction (conn.transaction())")
    conn.execute("SET LOCAL ROLE amarh_app")
    conn.execute(
        "SELECT set_config('amarh.role', %s, true), set_config('amarh.user', %s, true), "
        "set_config('amarh.region', %s, true), set_config('amarh.approves', %s, true), "
        "set_config('amarh.scenario', %s, true), set_config('amarh.scenario_base', %s, true), "
        "set_config('amarh.now', %s, true), "
        "set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)",
        [principal.role, principal.user, principal.region or "", ",".join(principal.approves), *scn_vals,
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
                "set_config('amarh.scenario', '', true), set_config('amarh.scenario_base', '', true), "
                "set_config('amarh.now', '', true), "
                "set_config('statement_timeout', '0', true), set_config('lock_timeout', '0', true)")


def current_scenario(conn: psycopg.Connection) -> int:
    """The scenario id currently set on the connection (0 = main). For tests / consistency checks."""
    v = conn.execute("SELECT current_setting('amarh.scenario', true)").fetchone()[0]
    return int(v) if v else 0


def current_principal(conn: psycopg.Connection) -> Principal | None:
    """The identity currently set on the connection (None when no role is set). For tests."""
    role, user, region, appr = conn.execute(
        "SELECT current_setting('amarh.role', true), current_setting('amarh.user', true), "
        "current_setting('amarh.region', true), current_setting('amarh.approves', true)").fetchone()
    if not role:
        return None
    return Principal(role, user or "", region or "", tuple(a for a in (appr or "").split(",") if a))
