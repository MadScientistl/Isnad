"""Event sinks for agent runs: MemorySink (tests) and DbSink (agent_run / agent_run_event, append-only).

DbSink writes short committed transactions under `scoped(conn, human_principal)`, so the RLS insert policies
(user = amarh.user AND role = amarh.role) bind every row to the human who started the run. Payloads are the
already-masked versions produced by the runner; nothing here ever sees the API key or raw HTTP headers.
"""
from __future__ import annotations

import json
import threading
from typing import Protocol

from engine.db.principal import scoped

PERSISTED = ("message", "tool_call", "tool_result", "denied", "proposal", "finished")


class EventSink(Protocol):
    def run_started(self, run_id: str, principal, prompt: str, model: str, caps: dict) -> None: ...
    def emit(self, event: dict) -> None: ...


class MemorySink:
    def __init__(self):
        self.runs: list[dict] = []
        self.events: list[dict] = []

    def run_started(self, run_id, principal, prompt, model, caps):
        self.runs.append({"id": run_id, "role": principal.role, "user": principal.user, "prompt": prompt,
                          "model": model, "caps": caps})

    def emit(self, event):
        self.events.append(json.loads(json.dumps(event, default=str)))

    def kinds(self):
        return [e["kind"] for e in self.events]


class DbSink:
    """`conn_factory` -> a connection of its own (not shared with the tools). Call `close()` when done."""

    def __init__(self, conn_factory, principal):
        self._factory, self._p = conn_factory, principal
        self._conn = None
        self._lock = threading.Lock()
        self._run_id = None

    def _c(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._factory()
        return self._conn

    def run_started(self, run_id, principal, prompt, model, caps):
        if (principal.role, principal.user) != (self._p.role, self._p.user):
            raise ValueError("sink principal differs from the run principal")
        with self._lock:
            c = self._c()
            with c.transaction():
                with scoped(c, self._p):
                    c.execute('INSERT INTO agent_run (id, role, "user", prompt, model, caps) '
                              "VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
                              [run_id, principal.role, principal.user, prompt, model, json.dumps(caps)])
        self._run_id = run_id

    def emit(self, event):
        if event["kind"] not in PERSISTED:
            return  # streaming deltas are not stored
        with self._lock:
            c = self._c()
            with c.transaction():
                with scoped(c, self._p):
                    c.execute("INSERT INTO agent_run_event (run_id, kind, payload, tokens_in, tokens_out, cost_usd) "
                              "VALUES (%s,%s,%s::jsonb,%s,%s,%s)",
                              [self._run_id, event["kind"],
                               json.dumps({"seq": event["seq"], **(event.get("payload") or {})}, default=str),
                               int(event.get("tokens_in", 0)), int(event.get("tokens_out", 0)),
                               float(event.get("cost_usd", 0.0))])
            c.commit()

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
