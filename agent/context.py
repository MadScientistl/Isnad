"""Run context: the acting human principal, ontology, connection factory, caps and the run's tool trace.

The human `Principal` is built by the caller (server side, from the same seam as the UI role switcher)
and is never derived from model output. Reads run as the human; proposals are written as `agent:<role>`.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from engine.db.principal import Principal, ScenarioCtx

DEFAULT_CAPS = dict(max_turns=12, max_tool_calls=25, max_output_tokens=60_000, max_cost_usd=0.50, max_wall_s=120.0)


@dataclass(frozen=True)
class Caps:
    max_turns: int = 12
    max_tool_calls: int = 25
    max_output_tokens: int = 60_000
    max_cost_usd: float = 0.50
    max_wall_s: float = 120.0

    @classmethod
    def for_role(cls, role_def, **override) -> "Caps":
        ag = getattr(role_def, "agent", None)
        kw = dict(DEFAULT_CAPS)
        if ag is not None:
            if ag.max_tool_calls is not None:
                kw["max_tool_calls"] = int(ag.max_tool_calls)
            if ag.max_cost_usd is not None:
                kw["max_cost_usd"] = float(ag.max_cost_usd)
        kw.update({k: v for k, v in override.items() if v is not None})
        return cls(**kw)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class AgentContext:
    principal: Principal                 # the acting HUMAN (role, user, region, approves)
    comp: object                         # compiled ontology (has .model) or the Ontology itself
    conn_factory: object                 # () -> psycopg connection (not in autocommit)
    pipeline: object = None              # optional SyncPipeline (inferred edges)
    caps: Caps | None = None
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancel: threading.Event = field(default_factory=threading.Event)
    clock: object = time.monotonic       # injectable for wall-clock tests
    started: float = 0.0
    trace: list = field(default_factory=list)       # compact per-call trace (passed to propose)
    seen: dict = field(default_factory=dict)         # ref -> set(tool_call_id)
    inferred_refs: set = field(default_factory=set)
    seen_ids: dict = field(default_factory=dict)     # uuid str -> type name or None
    tool_calls: int = 0
    scn: ScenarioCtx | None = None                   # the run's ACTIVE scenario (never set from model input)
    scn_name: str = ""
    scenario_ids: list = field(default_factory=list)  # every scenario this run opened, in order
    on_finish: object = None                         # optional callable run before the run's scenarios are expired
    _conn: object = None

    def __post_init__(self):
        role = self.model.roles.get(self.principal.role)
        if role is None or role.agent is None:
            raise ValueError(f"role {self.principal.role!r} has no agent")
        if not self.principal.user or self.principal.user.startswith("agent:"):
            raise ValueError("the acting principal must be a human user")
        if self.caps is None:
            self.caps = Caps.for_role(role)
        self.started = self.clock()

    @property
    def model(self):
        return getattr(self.comp, "model", self.comp)

    @property
    def role_def(self):
        return self.model.roles[self.principal.role]

    @property
    def agent_principal(self) -> Principal:
        from engine.actions import make_agent_principal
        return make_agent_principal(self.principal)

    @property
    def sid(self) -> int:
        return self.scn.id if self.scn is not None else 0

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining_s(self) -> float:
        return self.caps.max_wall_s - self.elapsed()

    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = self.conn_factory()
        else:
            try:
                self._conn.rollback()  # never inherit an open or failed transaction
            except Exception:  # noqa: BLE001
                self._conn = self.conn_factory()
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.rollback()
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    # ---- facts the run has actually seen (evidence / id validation)
    def see(self, ref: str, call_id: str, *, inferred: bool = False, type_: str | None = None) -> None:
        self.seen.setdefault(ref, set()).add(call_id)
        if inferred:
            self.inferred_refs.add(ref)
        if type_ is not None and ":" in ref:
            tail = ref.split(":", 1)[1]
            if len(tail) == 36:
                self.seen_ids[tail.lower()] = type_

    def see_id(self, id_: str, type_: str | None = None) -> None:
        k = str(id_).lower()
        if self.seen_ids.get(k) is None:
            self.seen_ids[k] = type_
