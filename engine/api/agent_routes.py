"""Agent HTTP API (Phase 4): start a run, stream it (SSE), list / read / cancel runs, role config.

Identity is the app's single seam (`get_principal`, from `x-role`); request bodies never carry a role or a user
(`extra="forbid"`). A run executes in a worker thread with its own connections; its events go to agent_run /
agent_run_event through `DbSink` (committed per event) and to an in-process buffer that live SSE clients follow.
Reads of agent_run / agent_run_event always happen under `scoped()` as the caller, so RLS decides visibility and
an invisible run is a 404 (never 403). Status is derived at read time: the last `finished` event, else `running`
if the run is in the in-process registry, else `interrupted` (rows are never updated).

Limits (env, read per request): AMARH_AGENT_MAX_CONCURRENT=1 per user, AMARH_AGENT_RUNS_PER_MIN=6 per user,
AMARH_AGENT_DAILY_USD=5 per user, AMARH_AGENT_MAX_GLOBAL=4 runs at once. They are checked under one lock that also
registers the new run, so concurrent POSTs cannot both pass.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, field_validator

from agent import AgentContext, AgentRun, DbSink, allowed_actions, allowed_tools
from agent.context import Caps
from engine.db.principal import Principal, scoped

log = logging.getLogger("amarh.agent_api")

PROMPT_MAX = 4000
START_WAIT_S = 5.0
KEEPALIVE_S = 10.0


class ModelFailure(Exception):
    """Generic model-side failure. Upstream text (which could echo request data) is never kept."""


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")   # no role / user / anything else
    prompt: str

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("prompt must not be empty")
        if len(v) > PROMPT_MAX:
            raise ValueError(f"prompt must be at most {PROMPT_MAX} characters")
        return v


class _SafeModel:
    """Wraps a model client: any failure becomes ModelFailure('model_error'); details go to the server log only."""

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name

    def complete(self, **kw):
        try:
            return self.inner.complete(**kw)
        except Exception as e:  # noqa: BLE001
            log.warning("model call failed: %s", type(e).__name__)   # class only: no upstream text, no key
            raise ModelFailure("model_error") from None


class _Sink(DbSink):
    def __init__(self, factory, principal, started: threading.Event):
        super().__init__(factory, principal)
        self._started = started

    def run_started(self, *a, **kw):
        try:
            super().run_started(*a, **kw)
        finally:
            self._started.set()


@dataclass
class Entry:
    run_id: str
    user: str
    role: str
    ctx: AgentContext
    started: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)
    buf: list = field(default_factory=list)
    done: bool = False
    thread: threading.Thread | None = None
    streams: int = 0          # open SSE connections (each holds a worker thread)

    def push(self, ev: dict) -> None:
        with self.cond:
            self.buf.append(ev)
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.runs: dict[str, Entry] = {}
        self.starts: dict[str, deque] = {}

    def running_for(self, user: str) -> int:
        return sum(1 for e in self.runs.values() if e.user == user and not e.done)


def _env_num(name: str, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_STREAMS_PER_RUN = 4


def limits() -> dict:
    return {"max_concurrent": _env_num("AMARH_AGENT_MAX_CONCURRENT", 1, int),
            "runs_per_minute": _env_num("AMARH_AGENT_RUNS_PER_MIN", 6, int),
            "daily_usd": _env_num("AMARH_AGENT_DAILY_USD", 5.0, float),
            "max_global": _env_num("AMARH_AGENT_MAX_GLOBAL", 4, int),
            "prompt_max_chars": PROMPT_MAX}


def default_model_factory(_principal=None):
    """Real client. Raises when ANTHROPIC_API_KEY is missing (the route turns that into 503)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("no key")
    from agent.model_anthropic import AnthropicModel
    return AnthropicModel()


def _err(status: int, code: str, detail: str, **extra) -> JSONResponse:
    headers = {"Retry-After": str(extra["retry_after"])} if "retry_after" in extra else None
    return JSONResponse({"code": code, "detail": detail, **extra}, status_code=status, headers=headers)


def _jsonable(x):
    return json.loads(json.dumps(x, default=str))


def mount(app, *, comp, get_principal, connect_fn, get_pipeline, model_factory=None) -> Registry:
    """Register the /agent routes on `app`. `connect_fn()` opens a new (non-autocommit) connection."""
    om = comp.model
    reg = Registry()
    app.state.agent_registry = reg
    if model_factory is not None:
        app.state.agent_model_factory = model_factory
    elif not hasattr(app.state, "agent_model_factory"):
        app.state.agent_model_factory = default_model_factory

    def role_agent(p: Principal):
        r = om.roles.get(p.role)
        return r.agent if r is not None else None

    # ------------------------------------------------------------------ DB reads (RLS decides)
    def read(p: Principal, fn):
        conn = connect_fn()
        try:
            with conn.transaction():
                with scoped(conn, p):
                    with conn.cursor(row_factory=dict_row) as cur:
                        return fn(cur)
        finally:
            conn.close()

    RUNS_SQL = (
        'SELECT r.id, r.role, r."user", r.prompt, r.model, r.caps, r.started_at, f.payload AS fin, '
        "coalesce(s.ti,0) AS tokens_in, coalesce(s.tout,0) AS tokens_out, coalesce(s.cost,0) AS cost_usd, "
        "coalesce(s.turns,0) AS turns, coalesce(s.tcs,0) AS tool_calls, coalesce(s.props,0) AS proposals, r.scenario_id AS run_scenario_id, "
        "coalesce((SELECT array_agg(DISTINCT (x.payload->'result'->'scenario'->>'id')::int) FROM agent_run_event x "
        "WHERE x.run_id = r.id AND x.kind = 'tool_result' AND x.payload->'result'->'scenario'->>'id' IS NOT NULL), "
        "'{}'::int[]) AS scenario_ids "
        "FROM agent_run r "
        "LEFT JOIN LATERAL (SELECT payload FROM agent_run_event e WHERE e.run_id = r.id AND e.kind = 'finished' "
        "ORDER BY e.seq DESC LIMIT 1) f ON true "
        "LEFT JOIN LATERAL (SELECT sum(tokens_in) AS ti, sum(tokens_out) AS tout, sum(cost_usd) AS cost, "
        "count(*) FILTER (WHERE kind = 'message') AS turns, count(*) FILTER (WHERE kind = 'tool_call') AS tcs, "
        "count(*) FILTER (WHERE kind = 'proposal') AS props FROM agent_run_event e WHERE e.run_id = r.id) s ON true ")

    def run_json(r: dict) -> dict:
        fin = r.get("fin")
        if fin:
            status = fin.get("status") or "error"
        elif str(r["id"]) in reg.runs and not reg.runs[str(r["id"])].done:
            status = "running"
        else:
            status = "interrupted"
        return _jsonable({
            "id": str(r["id"]), "role": r["role"], "user": r["user"], "prompt": r["prompt"], "model": r["model"],
            "caps": r["caps"], "started_at": r["started_at"], "status": status,
            "summary": (fin or {}).get("summary"), "cap": (fin or {}).get("cap"), "error": (fin or {}).get("error"),
            "tokens_in": int(r["tokens_in"]), "tokens_out": int(r["tokens_out"]),
            "cost_usd": round(float(r["cost_usd"]), 6), "turns": int(r["turns"]),
            "tool_calls": int(r["tool_calls"]), "proposals": int(r["proposals"]),
            "proposal_ids": (fin or {}).get("proposals") or [],
            # scenarios the run used, derived from its tool results (agent_run rows are append-only, so the
            # column written at run start stays 0)
            "scenario_ids": sorted(r.get("scenario_ids") or []),
            "scenario_id": (sorted(r.get("scenario_ids") or []) or [int(r.get("run_scenario_id") or 0)])[-1] or None})

    def fetch_run(p: Principal, run_id: str):
        def fn(cur):
            cur.execute(RUNS_SQL + "WHERE r.id = %s", [run_id])
            return cur.fetchone()
        return read(p, fn)

    def fetch_events(p: Principal, run_id: str, after: int = 0) -> list[dict]:
        def fn(cur):
            cur.execute("SELECT seq AS dbseq, kind, payload, tokens_in, tokens_out, cost_usd, at "
                        "FROM agent_run_event WHERE run_id = %s ORDER BY seq", [run_id])
            return cur.fetchall()
        out = []
        for r in read(p, fn):
            pl = dict(r["payload"] or {})
            seq = int(pl.pop("seq", r["dbseq"]))
            if seq > after:
                out.append(_jsonable({"seq": seq, "kind": r["kind"], "payload": pl, "tokens_in": r["tokens_in"],
                                      "tokens_out": r["tokens_out"], "cost_usd": float(r["cost_usd"]),
                                      "at": r["at"]}))
        return out

    def _uuid(run_id: str) -> str | None:
        try:
            import uuid as _u
            return str(_u.UUID(run_id))
        except ValueError:
            return None

    def visible_run(p: Principal, run_id: str):
        rid = _uuid(run_id)
        row = fetch_run(p, rid) if rid else None
        if row is None:
            return None, None
        return rid, row

    NOT_FOUND = lambda: _err(404, "not_found", "run not found")  # noqa: E731

    # ------------------------------------------------------------------ limits
    def check_limits(p: Principal, lim: dict):
        """Called under reg.lock. Returns a 429 response or None."""
        now = time.monotonic()
        if reg.running_for(p.user) >= lim["max_concurrent"]:
            return _err(429, "rate_limited", "a run is already in progress for this user", retry_after=5)
        if sum(1 for e in reg.runs.values() if not e.done) >= lim["max_global"]:
            return _err(429, "rate_limited", "the agent is busy; try again shortly", retry_after=5)
        dq = reg.starts.setdefault(p.user, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()

        def fn(cur):
            cur.execute('SELECT count(*) AS n FROM agent_run WHERE "user" = %s AND started_at > now() - interval '
                        "'60 seconds'", [p.user])
            n = cur.fetchone()["n"]
            cur.execute('SELECT coalesce(sum(e.cost_usd),0) AS usd FROM agent_run_event e JOIN agent_run r '
                        'ON r.id = e.run_id WHERE r."user" = %s AND e.at > now() - interval \'24 hours\'', [p.user])
            return n, float(cur.fetchone()["usd"])
        n_db, usd = read(p, fn)
        if max(len(dq), n_db) >= lim["runs_per_minute"]:
            wait = max(1, int(60 - (now - dq[0]))) if dq else 30
            return _err(429, "rate_limited", "too many runs started this minute", retry_after=wait)
        if usd >= lim["daily_usd"]:
            return _err(429, "rate_limited", "daily agent budget used up", retry_after=3600)
        dq.append(now)
        return None

    # ------------------------------------------------------------------ routes
    @app.post("/agent/runs", status_code=201)
    def start_run(body: RunBody, request: Request, p: Principal = Depends(get_principal)):
        if role_agent(p) is None:
            return _err(403, "no_agent", "this role has no agent")
        try:
            model = _SafeModel(request.app.state.agent_model_factory(p))
        except Exception as e:  # noqa: BLE001
            log.warning("agent model unavailable: %s", type(e).__name__)
            return _err(503, "agent_unavailable", "the agent is not configured")
        lim = limits()
        started = threading.Event()
        with reg.lock:
            blocked = check_limits(p, lim)
            if blocked is not None:
                return blocked
            ctx = AgentContext(p, comp, connect_fn)
            entry = Entry(str(ctx.run_id), p.user, p.role, ctx, started)
            reg.runs[entry.run_id] = entry
        sink = _Sink(connect_fn, p, started)

        def work():
            try:
                ctx.pipeline = get_pipeline()
                AgentRun(ctx, model, sink).run(body.prompt, on_event=entry.push)
            except Exception as e:  # noqa: BLE001
                log.warning("agent run crashed: %s", type(e).__name__)
            finally:
                try:
                    sink.close()
                except Exception:  # noqa: BLE001
                    pass
                started.set()
                entry.finish()
                with reg.lock:
                    reg.runs.pop(entry.run_id, None)

        entry.thread = threading.Thread(target=work, name=f"agent-run-{entry.run_id[:8]}", daemon=True)
        entry.thread.start()
        started.wait(START_WAIT_S)   # the agent_run row exists when we answer
        return JSONResponse({"run_id": entry.run_id}, status_code=201)

    @app.get("/agent/config")
    def agent_config(request: Request, p: Principal = Depends(get_principal)):
        ag = role_agent(p)
        if ag is None:
            return _err(403, "no_agent", "this role has no agent")
        actions = []
        for a in allowed_actions(om, p.role):
            d = om.actions[a]
            actions.append({"name": a, "description": d.description, "params": dict(d.params),
                            "requires_approval": d.requires_approval, "approvers": list(d.approvers)})
        try:
            available = request.app.state.agent_model_factory is not default_model_factory \
                or bool(os.environ.get("ANTHROPIC_API_KEY"))
        except Exception:  # noqa: BLE001
            available = False
        return {"role": p.role, "user": p.user, "available": available,
                "tools": list(allowed_tools(om, p.role)), "actions": actions,
                "caps": Caps.for_role(om.roles[p.role]).as_dict(), "limits": limits()}

    @app.get("/agent/runs")
    def list_runs(limit: int = Query(30, ge=1, le=200), p: Principal = Depends(get_principal)):
        if role_agent(p) is None:
            return _err(403, "no_agent", "this role has no agent")

        def fn(cur):
            cur.execute(RUNS_SQL + "ORDER BY r.seq DESC LIMIT %s", [limit])
            return cur.fetchall()
        return [run_json(r) for r in read(p, fn)]

    @app.get("/agent/runs/{run_id}")
    def get_run(run_id: str, p: Principal = Depends(get_principal)):
        rid, row = visible_run(p, run_id)
        if row is None:
            return NOT_FOUND()
        return {"run": run_json(row), "events": fetch_events(p, rid)}

    @app.post("/agent/runs/{run_id}/cancel")
    def cancel_run(run_id: str, p: Principal = Depends(get_principal)):
        rid, row = visible_run(p, run_id)
        if row is None:
            return NOT_FOUND()
        entry = reg.runs.get(rid)
        if entry is not None and not entry.done and not row.get("fin"):
            entry.ctx.cancel.set()
            return {"run_id": rid, "status": "cancelling"}
        return {"run_id": rid, "status": run_json(row)["status"]}

    def frame(ev: dict) -> str:
        return f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {json.dumps(ev, default=str)}\n\n"

    @app.get("/agent/runs/{run_id}/stream")
    def stream_run(run_id: str, after: int | None = Query(None, ge=0),
                   last_event_id: str | None = Header(default=None), p: Principal = Depends(get_principal)):
        rid, row = visible_run(p, run_id)
        if row is None:
            return NOT_FOUND()
        a = after
        if a is None:
            try:
                a = max(0, int(last_event_id)) if last_event_id else 0
            except ValueError:
                a = 0
        entry = reg.runs.get(rid)             # take the live handle BEFORE reading stored events
        if entry is not None and not entry.done:
            with entry.cond:
                if entry.streams >= MAX_STREAMS_PER_RUN:
                    return _err(429, "rate_limited", "too many open streams for this run", retry_after=2)
                entry.streams += 1
        else:
            entry = None
        stored = fetch_events(p, rid, a)

        def gen():
            try:
                yield from gen2()
            finally:
                if entry is not None:
                    with entry.cond:
                        entry.streams -= 1

        def gen2():
            last = a
            for ev in stored:
                yield frame(ev)
                last = ev["seq"]
                if ev["kind"] == "finished":
                    return
            synthetic = {"seq": last + 1, "kind": "finished", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                         "payload": {"status": "interrupted", "summary": "", "synthetic": True}}
            if entry is None:
                if not row.get("fin"):   # finished runs have nothing left to send
                    yield frame(synthetic)
                return
            idx = 0
            while True:
                with entry.cond:
                    if idx >= len(entry.buf) and not entry.done:
                        entry.cond.wait(timeout=KEEPALIVE_S)
                    batch = entry.buf[idx:]
                    idx += len(batch)
                    done = entry.done
                if not batch:
                    if done:
                        yield frame(synthetic)
                        return
                    yield ": keep-alive\n\n"
                    continue
                for ev in batch:
                    if ev["seq"] <= last:
                        continue
                    yield frame(_jsonable({"seq": ev["seq"], "kind": ev["kind"], "payload": ev["payload"],
                                           "tokens_in": ev.get("tokens_in", 0), "tokens_out": ev.get("tokens_out", 0),
                                           "cost_usd": ev.get("cost_usd", 0.0)}))
                    last = ev["seq"]
                    if ev["kind"] == "finished":
                        return

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return reg
