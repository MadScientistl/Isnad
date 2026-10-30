"""Generic FastAPI app over a compiled ontology. Run: uvicorn engine.api.main:app

Identity: every request carries `x-role`; the role maps to a `Principal(role, 'demo-<role>', region)`
(there is no real login in v1, but nothing below this seam trusts the client). Unknown roles and the
system role `sim` are refused (403); a missing header is refused unless ISNAD_DEFAULT_ROLE names a role
(a documented dev switch, unset by default). Reads run inside `scoped(conn, principal)` (Postgres RLS +
masked `rd_*` views decide what exists for the caller; hidden == missing, 404 never 403). All mutations go
through `engine.actions.execute / propose / decide / rollback` with the caller's principal.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
import math

from pydantic import BaseModel, Field, ValidationError

from engine.api import masking
from engine import kpis, scenarios
from engine.actions import (ApprovalRequired, CriteriaFailed, MergeConflict, NotFound, PermissionDenied,
                            ProposalExpired, ScenarioNotFound, StaleProposal, RollbackConflict,
                            RollbackRefused, decide, execute, make_principal, propose, rollback)
from engine.actions import outbox as outbox_mod
from engine.compiler import compile_ontology, exprs
from engine.db import bitemporal as bt
from engine.db import connect
from engine.db import scenario_admin
from engine.db.bitemporal import ScenarioLimit
from engine.db.principal import Principal, ScenarioCtx, scoped
from engine.sync import visibility as V
from engine.sync.store import SyncConfig

log = logging.getLogger("isnad.api")

DEFAULT_ONTOLOGY = "ontology/supply_chain.yaml"
HIDDEN = ("valid_during", "recorded_during", "scenario_id", "action_id")
SPEEDS = ("paused", "1", "6", "24")
METRICS = ("shipments_in_transit", "shipments_delayed", "ports_closed", "invoices_flagged",
           "value_at_risk")
MAX_GRAPH_NODES = 300
MAX_SERIES_POINTS = 2000
GRAPH_DEADLINE_S = 4.0
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class ApiError(Exception):
    """An error with a JSON body `{code, detail, ...}` (rendered by the app's exception handler)."""

    def __init__(self, status: int, code: str, detail: str, **extra):
        super().__init__(detail)
        self.status, self.body = status, {"code": code, "detail": detail, **extra}


def map_error(e: Exception) -> ApiError | None:
    """Engine / database exception -> ApiError (None = not ours, re-raise)."""
    if isinstance(e, PermissionDenied):
        return ApiError(403, "permission_denied", e.detail)
    if isinstance(e, CriteriaFailed):
        return ApiError(422, "criteria_failed", str(e), failures=[{"message": f["message"]} for f in e.failures])
    if isinstance(e, ScenarioNotFound):
        return ApiError(404, "scenario_not_found", "scenario not found")
    if isinstance(e, NotFound):
        return ApiError(404, "not_found", str(e))
    if isinstance(e, MergeConflict):
        return ApiError(409, "merge_conflict", "main changed on objects this scenario touched",
                        conflicts=e.conflicts)
    if isinstance(e, ScenarioLimit):
        return ApiError(429, "scenario_limit", str(e))
    if isinstance(e, StaleProposal):
        return ApiError(409, "stale_proposal", str(e), changed=e.changed)
    if isinstance(e, ProposalExpired):
        return ApiError(409, "proposal_expired", str(e))
    if isinstance(e, RollbackConflict):
        return ApiError(409, "rollback_conflict", str(e), keys=e.keys)
    if isinstance(e, RollbackRefused):
        return ApiError(409, "rollback_refused", str(e))
    if isinstance(e, ValueError):
        m = str(e)
        if "already exists" in m or "already decided" in m or "already rolled back" in m \
                or "already delivered" in m:
            return ApiError(409, "conflict", m)
        return ApiError(422, "invalid", m)
    if isinstance(e, psycopg.errors.QueryCanceled):
        return ApiError(408, "timeout", "statement timeout")
    if isinstance(e, (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable)):
        log.info("busy: %s", str(e).splitlines()[0])  # review L6: retryable contention, never a 500
        return ApiError(409, "busy", "the resource is busy; retry")
    if isinstance(e, psycopg.errors.ExclusionViolation):
        log.info("conflict: %s", str(e).splitlines()[0])
        return ApiError(409, "conflict", "conflicting concurrent or overlapping change")
    if isinstance(e, (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation, psycopg.DataError)):
        log.info("invalid: %s", str(e).splitlines()[0])
        return ApiError(422, "invalid", "value rejected by a data constraint")
    if isinstance(e, psycopg.IntegrityError):
        log.info("conflict: %s", str(e).splitlines()[0])
        return ApiError(409, "conflict", "conflicting change")
    return None


def _row(r: dict, history: bool = False) -> dict:
    out = {k: v for k, v in r.items() if k not in HIDDEN}
    if "valid_during" in r:
        out["valid_from"] = r["valid_during"].lower
        out["valid_to"] = bt._open(r["valid_during"].upper)
    if history:
        out["recorded_from"] = r["recorded_during"].lower
        out["recorded_to"] = bt._open(r["recorded_during"].upper)
    return jsonable_encoder(out)


def parse_ts(v: str | None, name: str = "as_of") -> datetime | None:
    """ISO datetime; None/''/'live' -> None (= current)."""
    if v is None or v == "" or v.lower() == "live":
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise HTTPException(422, f"{name} must be an ISO datetime or 'live', got {v!r}")
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(UTC).isoformat() if d else None


def _vt(alias: str, t: str) -> str:
    """Bitemporal 'valid at t, currently believed' predicate for a table alias."""
    a = f"{alias}." if alias else ""
    return (f"{a}scenario_id = 0 AND {a}valid_during @> {t} AND "
            f"(upper_inf({a}recorded_during) OR upper({a}recorded_during) = 'infinity')")


T_ASOF = "coalesce(%(t)s::timestamptz, now())"


def _o(type_: str) -> str:
    """Masked, RLS-filtered read relation of an object type (all versions)."""
    return f"rd_obj_{bt.snake(type_)}"


def _var_sql(t: str) -> str:
    """Scalar subquery: value at risk (cargo of non-arrived shipments delayed or via a closed port)."""
    return (
        f"(SELECT coalesce(sum(s.cargo_value), 0)::float8 FROM {_o('Shipment')} s "
        f"WHERE {_vt('s', t)} AND s.status IS DISTINCT FROM 'arrived' AND (s.status = 'delayed' OR EXISTS ("
        f"SELECT 1 FROM link_ships_via l JOIN {_o('Port')} p ON p.id = l.dst "
        f"WHERE l.src = s.id AND {_vt('l', t)} AND {_vt('p', t)} AND p.status = 'closed')))")


def _metric_sql(metric: str, t: str) -> str:
    cnt = "(SELECT count(*)::float8 FROM {tbl} x WHERE {vt} AND x.{col} = '{val}')"
    if metric == "shipments_in_transit":
        return cnt.format(tbl=_o("Shipment"), vt=_vt("x", t), col="status", val="in_transit")
    if metric == "shipments_delayed":
        return cnt.format(tbl=_o("Shipment"), vt=_vt("x", t), col="status", val="delayed")
    if metric == "ports_closed":
        return cnt.format(tbl=_o("Port"), vt=_vt("x", t), col="status", val="closed")
    if metric == "invoices_flagged":
        return cnt.format(tbl=_o("Invoice"), vt=_vt("x", t), col="status", val="flagged")
    return _var_sql(t)


class ControlBody(BaseModel):
    speed: str | int | None = None
    step: bool | None = None
    reset: bool | None = None


class InjectBody(BaseModel):
    kind: Literal["port_closure", "shipment_delay", "invoice_anomaly"]
    target: UUID | None = None
    at: datetime | None = None
    duration_h: float | None = Field(default=None, gt=0, le=24 * 365, allow_inf_nan=False)


MAX_TEXT = 4000          # comment / rationale / reason / note fields
MAX_BODY_BYTES = int(os.environ.get("ISNAD_MAX_BODY_BYTES", str(2 * 1024 * 1024)))
MAX_INJECTIONS = 20


def _sim_days_cap() -> int:
    return int(os.environ.get("ISNAD_SCN_SIM_MAX_DAYS", "90"))


class ExecBody(BaseModel):
    params: dict[str, Any] = {}
    valid_at: datetime | None = None
    rationale: str | None = Field(default=None, max_length=MAX_TEXT)


class ProposeBody(BaseModel):
    params: dict[str, Any] = {}
    rationale: str | None = Field(default=None, max_length=MAX_TEXT)
    valid_at: datetime | None = None


class CommentBody(BaseModel):
    comment: str | None = Field(default=None, max_length=MAX_TEXT)


class RollbackBody(BaseModel):
    reason: str | None = Field(default=None, max_length=MAX_TEXT)


class SparqlBody(BaseModel):
    query: str = Field(max_length=50_000)


class ScenarioBody(BaseModel):
    name: str = Field(max_length=200)
    expires_in_h: float | None = Field(default=None, gt=0, le=720, allow_inf_nan=False)
    shared_roles: list[str] = Field(default=[], max_length=10)


class MergeBody(BaseModel):
    comment: str | None = Field(default=None, max_length=MAX_TEXT)


class InjectionBody(BaseModel):
    """One what-if injection. Bounds (review L4): finite numbers only; magnitude in (0, 100]; times within the
    simulated horizon (ISNAD_SCN_SIM_MAX_DAYS); target is a UUID (validated by pydantic)."""
    kind: Literal["port_closure", "shipment_delay", "supplier_delay", "price_shock", "invoice_anomaly"]
    target: UUID | None = None
    at_h: float | None = Field(default=None, ge=0, le=24 * 365, allow_inf_nan=False)
    duration_h: float | None = Field(default=None, gt=0, le=24 * 365, allow_inf_nan=False)
    magnitude: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False)


class SimulateBody(BaseModel):
    days: int = Field(ge=1, le=3650)
    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    injections: list[InjectionBody] = Field(default=[], max_length=MAX_INJECTIONS)


class _BodyLimit:
    """ASGI middleware: requests above MAX_BODY_BYTES are answered 413 `{code: body_too_large}` (declared
    Content-Length is checked up front; streamed bodies are counted)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        too_big = False
        try:
            cl = int(dict(scope["headers"]).get(b"content-length", b"0") or 0)
        except ValueError:
            cl = 0
        if cl > self.max:
            too_big = True
        seen = 0
        started = False

        async def rcv():
            nonlocal seen, too_big
            if too_big:
                return {"type": "http.request", "body": b"", "more_body": False}
            m = await receive()
            if m["type"] == "http.request":
                seen += len(m.get("body", b""))
                if seen > self.max:
                    too_big = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return m

        async def snd(m):
            nonlocal started
            if too_big and not started:
                started = True
                body = json.dumps({"code": "body_too_large", "detail": "request body too large"}).encode()
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            if too_big:
                return
            started = True
            await send(m)

        if too_big:  # declared too large: never run the app
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            return
        await self.app(scope, rcv, snd)


def create_app(ontology_path: str | None = None, schema: str | None = None,
               dsn: str | None = None, runner: Any = None, pipeline: Any = None,
               agent_model_factory: Any = None) -> FastAPI:
    """`runner` / `pipeline`: optional injected doubles. `pipeline=False` disables sync entirely.
    `agent_model_factory(principal) -> model client` (tests inject MockModel; default: Anthropic, needs a key)."""
    path = ontology_path or os.environ.get("ISNAD_ONTOLOGY", DEFAULT_ONTOLOGY)  # CLI default only
    from pathlib import Path as _Path
    schema = schema or os.environ.get("ISNAD_SCHEMA")
    comp = compile_ontology(path)
    om = comp.model
    models = comp.pydantic_models()
    cfg = SyncConfig.from_ontology(comp)   # namespace, types, link semantics, ttl/shapes: all from this ontology

    state = {"runner": runner, "runner_failed": False,
             "pipeline": pipeline if pipeline is not None else
             (False if os.environ.get("ISNAD_PIPELINE", "1") == "0" else None),
             "reasoning": False}
    plock = threading.RLock()
    worker = {"stop": None, "thread": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            from engine.api.seed import apply_schema
            c = connect(dsn, schema=schema)
            try:
                apply_schema(c, comp.ddl())
                try:  # ontology versioning: record the running definition (idempotent)
                    from engine import versioning
                    versioning.register_current(c, _Path(path).read_text(), "startup")
                    c.commit()
                except Exception as e:  # noqa: BLE001
                    c.rollback()
                    log.warning("ontology version registration failed: %s", e)
                try:  # lazy janitor: scenarios past their lifetime
                    scenarios.expire_due(c)
                except Exception as e:  # noqa: BLE001
                    c.rollback()
                    log.warning("scenario expiry failed: %s", e)
            finally:
                c.close()
        except Exception as e:  # database may be down at startup; endpoints will report it
            log.warning("startup schema check failed: %s", e)
        if os.environ.get("ISNAD_OUTBOX_WORKER", "1") != "0":
            stop = threading.Event()
            th = threading.Thread(
                target=outbox_mod.run_worker, name="outbox-worker", daemon=True,
                args=(stop, float(os.environ.get("ISNAD_OUTBOX_INTERVAL_S", "2"))),
                kwargs={"conn_factory": lambda: connect(dsn, schema=schema), "ontology": comp})
            th.start()
            worker["stop"], worker["thread"] = stop, th
        if os.environ.get("ISNAD_SIM_AUTOSTART", "0") == "1" and om.sim is not None:
            try:
                get_runner().start()
            except HTTPException:
                pass
        yield
        if worker["stop"] is not None:
            worker["stop"].set()
        r = state["runner"]
        if r is not None:
            try:
                r.stop()
            except Exception:  # noqa: BLE001
                pass

    app = FastAPI(title="Isnad ontology API", lifespan=lifespan)
    app.add_middleware(_BodyLimit, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    app.state.ontology = comp
    app.state.runtime = state

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse(e.body, status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        # the default handler echoes the offending input, which cannot be serialised for NaN / inf (-> 500) and
        # may be huge; report location + message only
        errs = [{"type": x.get("type"), "loc": [str(l) for l in x.get("loc", ())], "msg": x.get("msg")}
                for x in e.errors()[:20]]
        return JSONResponse({"code": "invalid", "detail": errs}, status_code=422)

    # ---------------------------------------------------------- identity + connections
    def get_principal(x_role: str | None = Header(default=None)) -> Principal:
        """The single identity seam. Unknown role / `sim` / missing header => 403."""
        role = x_role or os.environ.get("ISNAD_DEFAULT_ROLE") or None
        if not role:
            raise ApiError(403, "role_required", "x-role header required")
        if role == "sim" or role not in om.roles:
            raise ApiError(403, "unknown_role", f"unknown role {role!r}")
        return make_principal(role, f"demo-{role}", comp)

    def parse_scenario_header(raw: str | None) -> int:
        """0 = main. A non-integer / negative value is a client error (400)."""
        if raw is None or raw.strip() == "":
            return 0
        n = int(raw.strip()) if re.fullmatch(r"[0-9]{1,10}", raw.strip()) else -1  # ASCII digits only (no "1_0", "+1")
        if n > 2**31 - 1:  # beyond the serial range: cannot exist
            raise ApiError(404, "scenario_not_found", "scenario not found")
        if n < 0:
            raise ApiError(400, "invalid_scenario", "x-scenario must be a non-negative integer")
        return n

    def get_scenario(x_scenario: str | None = Header(default=None),
                     p: Principal = Depends(get_principal)) -> ScenarioCtx | None:
        """Header `x-scenario` -> ScenarioCtx, resolved ONLY server-side after a visibility check. Unknown,
        invisible and closed scenarios are all 404 `scenario_not_found`. Missing / 0 = main (None)."""
        n = parse_scenario_header(x_scenario)
        if n == 0:
            return None
        c = connect(dsn, schema=schema)
        try:
            return scenarios.resolve(c, p, n)
        except ScenarioNotFound:
            raise ApiError(404, "scenario_not_found", "scenario not found") from None
        finally:
            c.close()

    def no_scenario(x_scenario: str | None = Header(default=None)) -> None:
        """Dependency for routes that only exist on main (/sim/*, /webhooks/*, approve/reject)."""
        if x_scenario is not None and x_scenario.strip() not in ("", "0"):
            raise ApiError(400, "scenario_unsupported", "this route is not available inside a scenario")

    def rdb(p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario)):
        """Connection inside ONE transaction scoped to the caller (RLS + masking) and, with `x-scenario`, to the
        resolved scenario overlay. Rolled back at the end."""
        conn = connect(dsn, schema=schema)
        try:
            with conn.transaction():
                with scoped(conn, p, scn):
                    yield conn
        finally:
            conn.close()

    def wconn():
        """Plain connection for engine entry points (they open + scope their own transaction)."""
        conn = connect(dsn, schema=schema)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read_scope(conn, p: Principal, scn: ScenarioCtx | None = None):
        with conn.transaction():
            with scoped(conn, p, scn):
                yield conn

    def fail(conn, e: Exception):
        """Roll back and raise the mapped ApiError (or re-raise unknown exceptions)."""
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        ae = map_error(e)
        if ae is None:
            raise e
        raise ae from e

    # ---------------------------------------------------------- runtime pieces
    def get_pipeline():
        p = state["pipeline"]
        if p is False:
            return None
        if p is None:
            try:
                from engine.sync.pipeline import SyncPipeline
                p = state["pipeline"] = SyncPipeline(dsn=dsn, schema=schema, cfg=cfg)
                with plock:  # first use: mirror what is already in Postgres, then reason once
                    p.sync()
                    p.maybe_reason(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("sync pipeline unavailable: %s", e)
                state["pipeline"] = False
                return None
        return p

    def sim_unavailable() -> ApiError:
        return ApiError(404, "sim_unavailable", "this ontology has no simulation")

    def get_runner():
        if om.sim is None:  # the runner is never constructed for an ontology without a `sim:` block
            raise sim_unavailable()
        if state["runner"] is None and not state["runner_failed"]:
            try:
                from sim.runner import SimRunner
                state["runner"] = SimRunner(dsn=dsn, schema=schema, pipeline=get_pipeline())
            except Exception as e:  # noqa: BLE001
                log.warning("simulation unavailable: %s", e)
                state["runner_failed"] = True
        if state["runner"] is None:
            raise HTTPException(503, "simulation unavailable")
        return state["runner"]

    def _reason_bg(p):
        try:
            with plock:
                p.maybe_reason()
        except Exception as e:  # noqa: BLE001
            log.warning("reasoning failed: %s", e)
        finally:
            state["reasoning"] = False

    def after_write():
        """Cheap sync of the semantic layer after API writes; reasoning runs off-thread."""
        p = get_pipeline()
        if p is None:
            return
        try:
            with plock:
                p.sync()
        except Exception as e:  # noqa: BLE001
            log.warning("sync failed: %s", e)
            return
        if not state["reasoning"]:
            state["reasoning"] = True
            threading.Thread(target=_reason_bg, args=(p,), daemon=True).start()

    def now_utc() -> datetime:
        return datetime.now(UTC)

    MAX_PARAMS_BYTES = 64 * 1024

    def cap_params(params) -> None:
        try:
            n = len(json.dumps(params, default=str))
        except (TypeError, ValueError):
            n = MAX_PARAMS_BYTES + 1
        if n > MAX_PARAMS_BYTES:
            raise ApiError(413, "payload_too_large", "params exceed 64 KB")

    def run_action(conn, name: str, params: dict, p: Principal, valid_at: datetime,
                   scn: ScenarioCtx | None = None):
        """execute() with error mapping (ApprovalRequired propagates to the caller). Scenario writes never
        trigger the semantic-layer sync / reasoner (the main graph is unaffected)."""
        try:
            res = execute(conn, name, params, p, valid_at=valid_at, source="api", ontology=comp,
                          scenario_id=scn.id if scn else 0)
        except ApprovalRequired:
            raise
        except Exception as e:  # noqa: BLE001
            fail(conn, e)
        if scn is None:
            after_write()
        return res

    def otype(t: str):
        if t not in om.object_types:
            raise HTTPException(404, f"unknown object type '{t}'")
        return getattr(models, t)

    def ltype(l: str):
        if l not in om.link_types:
            raise HTTPException(404, f"unknown link type '{l}'")
        return om.link_types[l]

    def validated(model, id_: UUID, body: dict) -> dict:
        try:
            m = model.model_validate({**body, "id": id_})
        except ValidationError as e:
            raise HTTPException(422, jsonable_encoder(e.errors(include_url=False, include_context=False, include_input=False)))
        return m.model_dump(exclude={"id"})

    def vfrom(v) -> datetime:
        if not v:
            return now_utc()
        d = parse_ts(str(v), "valid_from")
        return d or now_utc()

    def geo_col(type_: str, conn) -> str | None:
        g = bt.geometry_columns(conn, type_)
        return g[0] if g else None

    # ---------------------------------------------------------- meta
    @app.get("/health")
    def health():
        ok = True
        try:
            c = connect(dsn, schema=schema)
            try:
                c.execute("SELECT 1")
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            ok = False
        return {"status": "ok" if ok else "degraded", "db": ok, "schema": schema,
                "pipeline": state["pipeline"] not in (False, None),
                "simulation": state["runner"] is not None}

    @app.get("/types")
    def types(p: Principal = Depends(get_principal)):
        """Ontology metadata the UI builds its pages from. Additive over the generated client's TypesResponse:
        per type `geo` (has a point/linestring property), `volatile` (some property changes every tick),
        `links` (`out`/`in`: link names with the other end), and `volatile` per property."""
        geo = set(om.geo_types)
        out_l: dict[str, list] = {t: [] for t in om.object_types}
        in_l: dict[str, list] = {t: [] for t in om.object_types}
        for l in om.link_types.values():
            if l.src in out_l:
                out_l[l.src].append({"link": l.name, "to": l.dst, "cardinality": l.cardinality})
            if l.dst in in_l:
                in_l[l.dst].append({"link": l.name, "from": l.src, "cardinality": l.cardinality})
        return {
            "name": om.name,
            "namespace": om.namespace,
            "classes": {c.name: {"extends": c.extends} for c in om.classes.values()},
            "objectTypes": [{"name": t.name, "label": t.name, "geo": t.name in geo,
                             "volatile": any(pr.volatile for pr in t.properties),
                             "properties": [
                                 {"name": pr.name, "type": pr.type, "required": pr.required, "enum": list(pr.enum),
                                  "volatile": pr.volatile} for pr in t.properties],
                             "links": {"out": out_l[t.name], "in": in_l[t.name]}}
                            for t in om.object_types.values()],
            "linkTypes": [{"name": l.name, "from": l.src, "to": l.dst, "cardinality": l.cardinality,
                           "required": l.required, "transitive": l.transitive, "inverse": l.inverse}
                          for l in om.link_types.values()],
            "role": p.role,
            "kpis": {k: {"description": v.description, "expr": v.expr} for k, v in om.kpis.items()},
            "sim": om.sim is not None,
        }

    @app.get("/me")
    def me(p: Principal = Depends(get_principal)):
        r = om.roles[p.role]
        tmap = {}
        for t in om.object_types:
            a = r.types.get(t)
            tmap[t] = {"read": bool(a and a.read), "write": bool(a and a.write),
                       "masked": list(a.mask) if a and a.read else []}
        return {"role": p.role, "user": p.user, "region": p.region or None, "types": tmap,
                "actions": [n for n in om.actions if om.role_can_execute(p.role, n)],
                "approves": [n for n in om.actions if om.role_can_approve(p.role, n)]}

    @app.get("/roles")
    def roles():
        return [{"role": n, "description": r.description, "region": r.region, "user": f"demo-{n}"}
                for n, r in om.roles.items() if n != "sim"]

    # ---------------------------------------------------------- objects
    @app.get("/objects/{type}")
    def list_objects(type: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                     as_of_valid: datetime | None = None, as_of_recorded: datetime | None = None,
                     bbox: str | None = None, order_by: str | None = None, desc: bool = False,
                     p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                     conn=Depends(rdb)):
        otype(type)
        where, params = None, ()
        if bbox:
            try:
                w, s_, e, n = (float(x) for x in bbox.split(","))
            except ValueError:
                raise HTTPException(422, "bbox must be 'west,south,east,north' numbers")
            col = geo_col(type, conn)
            if col is None:
                raise HTTPException(422, f"{type} has no geometry column; bbox not applicable")
            where, params = f'"{col}" && ST_MakeEnvelope(%s,%s,%s,%s,4326)', [w, s_, e, n]
        try:
            rows = bt.list_objects(conn, type, as_of_valid, as_of_recorded, scn.id if scn else 0, where=where,
                                   params=params, limit=limit, offset=offset, order_by=order_by, desc=desc)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return [_row(r) for r in rows]

    @app.post("/objects/{type}", status_code=201)
    def create_object(type: str, body: dict[str, Any] = Body(...), p: Principal = Depends(get_principal),
                      scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(wconn)):
        model = otype(type)
        body = dict(body)
        try:
            id_ = UUID(str(body.pop("id"))) if body.get("id") else uuid4()
        except ValueError:
            raise HTTPException(422, "id must be a uuid")
        vf = vfrom(body.pop("valid_from", None))
        sid = scn.id if scn else 0
        with read_scope(conn, p, scn):
            exists = bt.get_object(conn, type, id_, scenario_id=sid) is not None
        if exists:
            raise ApiError(409, "conflict", f"{type} {id_} already exists")
        props = validated(model, id_, body)
        for c in masked_cols(p, type):
            props[c] = None
        run_action(conn, "create_object", {"type": type, "id": id_, "props": props}, p, vf, scn)
        with read_scope(conn, p, scn):
            r = bt.get_object(conn, type, id_, scenario_id=sid)
        return _row(r) if r else {"id": str(id_)}

    @app.get("/objects/{type}/{id}")
    def get_object(type: str, id: UUID, as_of_valid: datetime | None = None,
                   as_of_recorded: datetime | None = None, p: Principal = Depends(get_principal),
                   scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        otype(type)
        r = bt.get_object(conn, type, id, as_of_valid, as_of_recorded, scn.id if scn else 0)
        if r is None:
            raise HTTPException(404, f"{type} {id} not found")
        return _row(r)

    def masked_cols(p: Principal, type_: str) -> set[str]:
        a = om.roles[p.role].types.get(type_)
        return set(a.mask) if a else set()

    @app.put("/objects/{type}/{id}")
    def put_object(type: str, id: UUID, body: dict[str, Any] = Body(...),
                   p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                   conn=Depends(wconn)):
        model = otype(type)
        body = dict(body)
        body.pop("id", None)
        vf = vfrom(body.pop("valid_from", None))
        sid = scn.id if scn else 0
        with read_scope(conn, p, scn):
            cur = bt.get_object(conn, type, id, scenario_id=sid)
            raw = bt.get_object(conn, type, id, masked=False, scenario_id=sid) if cur is not None else None
        if cur is None:
            raise HTTPException(404, f"{type} {id} not found")
        props = validated(model, id, body)
        for c in masked_cols(p, type):  # masked props: client input ignored, stored value kept
            props[c] = raw.get(c)
        run_action(conn, "update_object", {"type": type, "id": id, "props": props}, p, vf, scn)
        with read_scope(conn, p, scn):
            r = bt.get_object(conn, type, id, scenario_id=sid)
        return _row(r) if r else {"id": str(id)}

    @app.delete("/objects/{type}/{id}")
    def delete_object(type: str, id: UUID, p: Principal = Depends(get_principal),
                      scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(wconn)):
        otype(type)
        with read_scope(conn, p, scn):
            cur = bt.get_object(conn, type, id, scenario_id=scn.id if scn else 0)
        if cur is None:
            raise HTTPException(404, f"{type} {id} not found")
        run_action(conn, "delete_object", {"type": type, "id": id}, p, now_utc(), scn)
        return {"deleted": str(id)}

    @app.get("/objects/{type}/{id}/history")
    def object_history(type: str, id: UUID, p: Principal = Depends(get_principal),
                       scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        otype(type)
        rows = bt.history(conn, type, id, scn.id if scn else 0)
        if not rows:  # hidden (D4: no access to past versions either) == missing
            raise HTTPException(404, f"{type} {id} not found")
        return [_row(r, history=True) for r in rows]

    # ------------------------------------------------------------ links
    @app.get("/links/{link}")
    def get_links(link: str, id: UUID | None = None, direction: str = Query("out", pattern="^(out|in)$"),
                  as_of: datetime | None = None, as_of_recorded: datetime | None = None,
                  p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                  conn=Depends(rdb)):
        ltype(link)
        rows = bt.list_links(conn, link, as_of, as_of_recorded, scn.id if scn else 0)
        if id is not None:
            key = "src" if direction == "out" else "dst"
            rows = [r for r in rows if r[key] == id]
        return [{"src": str(r["src"]), "dst": str(r["dst"])} for r in rows]

    @app.post("/links/{link}", status_code=201)
    def create_link(link: str, body: dict[str, Any] = Body(...), p: Principal = Depends(get_principal),
                    scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(wconn)):
        ltype(link)
        try:
            src, dst = UUID(str(body["src"])), UUID(str(body["dst"]))
        except (KeyError, ValueError):
            raise HTTPException(422, "body needs uuid 'src' and 'dst'")
        run_action(conn, "create_link", {"link": link, "src": src, "dst": dst}, p, vfrom(body.get("valid_from")), scn)
        return {"src": str(src), "dst": str(dst)}

    @app.delete("/links/{link}")
    def delete_link(link: str, src: UUID, dst: UUID, p: Principal = Depends(get_principal),
                    scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(wconn)):
        ltype(link)
        run_action(conn, "end_link", {"link": link, "src": src, "dst": dst}, p, now_utc(), scn)
        return {"deleted": {"src": str(src), "dst": str(dst)}}

    @app.get("/neighbours/{type}/{id}")
    def neighbours(type: str, id: UUID, as_of: datetime | None = None,
                   p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                   conn=Depends(rdb)):
        otype(type)
        sid = scn.id if scn else 0
        if scn is not None and as_of is not None:
            raise ApiError(422, "invalid", "as_of is not supported inside a scenario")
        if id not in V.visible_ids(conn, [id], as_of=as_of, cfg=cfg, scenario=scn):
            raise HTTPException(404, f"{type} {id} not found")
        edges = V.visible_edges(conn, [id], as_of=as_of, cfg=cfg, scenario=scn)
        order = {n: i for i, n in enumerate(om.link_types)}
        items = []
        for s_, l, d in edges:
            lt = om.link_types[l]
            if s_ == id and lt.src == type:
                items.append((order[l], 0, str(d), l, "out", lt.dst, d))
            if d == id and lt.dst == type:
                items.append((order[l], 1, str(s_), l, "in", lt.src, s_))
        items.sort(key=lambda x: x[:3])
        infos = V.node_infos(conn, [x[6] for x in items], as_of=as_of, cfg=cfg, model=om, scenario=scn)
        out = []
        for _, _, _, l, direction, other_t, oid in items:
            info = infos.get(str(oid))
            t = info["type"] if info else other_t
            o = bt.get_object(conn, t, oid, as_of, scenario_id=sid) if t in om.object_types else None
            out.append({"link": l, "direction": direction, "type": t, "id": str(oid),
                        "object": _row(o) if o else None})
        return out

    # ------------------------------------------------------------ simulation
    def sim_state(r) -> dict:
        s = dict(r.state())
        out = {k: s.get(k) for k in ("sim_now", "epoch", "speed", "running", "seed", "tick",
                                     "achieved_hours_per_s", "last_error")}
        out["speed"] = None if out["speed"] is None else str(out["speed"])
        out["running"] = bool(out["running"])
        return jsonable_encoder(out)

    @app.get("/sim/state")
    def get_sim_state(p: Principal = Depends(get_principal), _ns: None = Depends(no_scenario)):
        out = sim_state(get_runner())
        if p.role != "admin":
            out["last_error"] = None  # engine error text is admin-only
        return out

    def sim_audit(conn, p: Principal, name: str, params: dict) -> None:
        """Audit row for an admin sim action (actor = on_behalf_of = the calling user)."""
        conn.execute(
            "INSERT INTO audit_event (action_id, name, actor, source, params, role, on_behalf_of, status) "
            "VALUES (%s,%s,%s,'api',%s::jsonb,%s,%s,'executed')",
            [uuid4(), name, p.user, json.dumps(jsonable_encoder(params)), p.role, p.user])
        conn.commit()

    def need_admin(p: Principal) -> None:
        if p.role != "admin":
            raise ApiError(403, "permission_denied", "admin only")

    def reset_data(conn):
        """Delete obj_*, link_*, graph_outbox and the MAIN inferred_support rows (scenario_id = 0) ONLY (never
        audit/governance tables). Open scenarios are discarded first: their data lives in the same tables and
        their base snapshot would be meaningless after a reset (the 'discarded' events and audit stay)."""
        open_ids = [r[0] for r in conn.execute(
            "SELECT s.id FROM scenario s WHERE NOT EXISTS (SELECT 1 FROM scenario_event e WHERE "
            "e.scenario_id = s.id AND e.kind IN ('merged','discarded','expired')) ORDER BY s.id").fetchall()]
        for sid in open_ids:
            conn.execute("INSERT INTO scenario_event (scenario_id, kind, actor, payload) VALUES "
                         "(%s,'discarded','system:reset','{\"reason\": \"reset\"}'::jsonb)", [sid])
            scenario_admin.purge(conn, sid)
        tables = [row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE' AND (table_name LIKE 'obj\\_%' OR table_name LIKE 'link\\_%' "
            "OR table_name = 'graph_outbox')").fetchall()]
        if tables:
            conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in tables)))
        conn.execute("DELETE FROM inferred_support WHERE scenario_id = 0")
        conn.execute(
            "INSERT INTO audit_event (action_id, name, actor, source, params, role, status) "
            "VALUES (%s,'reset','demo-admin','api','{}'::jsonb,'admin','executed')", [uuid4()])
        conn.commit()

    @app.post("/sim/control")
    def sim_control(body: ControlBody, p: Principal = Depends(get_principal), _ns: None = Depends(no_scenario),
                    conn=Depends(wconn)):
        need_admin(p)
        r = get_runner()
        if body.speed is not None and str(body.speed) not in SPEEDS:
            raise HTTPException(422, f"speed must be one of {list(SPEEDS)}")
        if body.reset:
            if os.environ.get("ISNAD_ALLOW_RESET") != "1":
                raise HTTPException(403, "reset deletes all data in this schema; set ISNAD_ALLOW_RESET=1 to allow it")
        try:
            if body.reset:
                r.stop()
                reset_data(conn)
                r.reset()
                from engine.api.seed import seed
                seed(conn)
                pl = get_pipeline()
                if pl is not None:
                    with plock:
                        pl.rebuild()
            if body.speed is not None:
                r.set_speed(str(body.speed))
                if str(body.speed) == "paused":
                    r.stop()
                else:
                    r.start()
            if body.step:
                r.step()
        except ValueError as e:
            conn.rollback()
            raise HTTPException(422, str(e))
        sim_audit(conn, p, "sim_control", {"reset": body.reset, "speed": body.speed, "step": body.step})
        if body.step or body.reset:
            after_write()
        return sim_state(r)

    @app.post("/sim/inject")
    def sim_inject(body: InjectBody, p: Principal = Depends(get_principal), _ns: None = Depends(no_scenario),
                   conn=Depends(wconn)):
        need_admin(p)
        r = get_runner()
        kw: dict[str, Any] = {"target": body.target, "at": body.at}
        if body.duration_h is not None:
            kw["duration_h"] = body.duration_h
        try:
            res = r.inject(body.kind, **kw) or {}
        except ValueError as e:
            raise HTTPException(422, str(e))
        inj = res.get("injected", res) if isinstance(res, dict) else {}
        out = {"kind": inj.get("kind", body.kind),
               "target": inj.get("target", body.target),
               "at": inj.get("at", body.at)}
        sim_audit(conn, p, "sim_inject", {"kind": body.kind, "target": body.target, "at": body.at,
                                          "duration_h": body.duration_h})
        return jsonable_encoder({"injected": out})

    # ------------------------------------------------------------ events
    label_cols = {t.name: next((c for c in ("name", "ref", "kind") if any(pr.name == c for pr in t.properties)), None)
                  for t in om.object_types.values()}

    def names_for(conn, ids: set[str], sid: int = 0) -> dict[str, str]:
        """Display labels (masked, RLS-filtered). `sid` > 0: read the scenario-effective rows instead of main."""
        if not ids:
            return {}
        arr = [UUID(i) for i in ids]
        out: dict[str, str] = {}
        with conn.cursor(row_factory=dict_row) as cur:
            for t, col in label_cols.items():
                if col is None:
                    continue
                if sid:  # the connection is scoped to scenario `sid` (eff view = current effective version)
                    cur.execute(f'SELECT id, "{col}" AS label FROM rd_eff_obj_{bt.snake(t)} '
                                "WHERE id = ANY(%s::uuid[])", [arr])
                else:
                    cur.execute(
                        f'SELECT DISTINCT ON (id) id, "{col}" AS label FROM {_o(t)} '
                        "WHERE scenario_id = 0 AND id = ANY(%s::uuid[]) AND "
                        "(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity') "
                        "ORDER BY id, lower(valid_during) DESC", [arr])
                for r in cur.fetchall():
                    out[str(r["id"])] = r["label"]
        return out

    def summarize(r: dict, nm: dict[str, str]) -> str:
        pr = r["params"] or {}

        def n(key):
            v = pr.get(key)
            return nm.get(str(v), str(v)[:8]) if v else "?"
        name = r["name"]
        if name == "close_port":
            return f"Port {n('port')} closed"
        if name == "reopen_port":
            return f"Port {n('port')} reopened"
        if name == "delay_shipment":
            return f"Shipment {n('shipment')} delayed {float(pr.get('delay_hours', 0)):g} h"
        if name == "ship_depart":
            return f"Shipment {n('shipment')} departed"
        if name == "ship_arrive":
            return f"Shipment {n('shipment')} arrived"
        if name == "ship_position":
            return f"Shipment {n('shipment')} position updated"
        if name == "issue_invoice":
            return f"Invoice issued for Shipment {n('shipment')} ({float(pr.get('amount', 0)):,.0f} {pr.get('currency') or 'USD'})"
        if name == "flag_invoice":
            return f"Invoice {n('invoice')} flagged: {pr.get('anomaly')}"
        if name == "pay_invoice":
            return f"Invoice {n('invoice')} paid"
        if name in ("create_object", "update_object", "delete_object"):
            verb = {"create_object": "created", "update_object": "updated", "delete_object": "deleted"}[name]
            oid = pr.get("id") or (r["first_key"] or ":").split(":", 1)[1] or None
            return f"{pr.get('type', 'Object')} {nm.get(str(oid), str(oid or '')[:8])} {verb}".replace("  ", " ")
        if name in ("create_link", "end_link"):
            verb = "linked" if name == "create_link" else "unlinked"
            return f"{pr.get('link')}: {n('src')} {verb} {n('dst')}"
        if name == "import_dataset":
            return "Dataset imported"
        if name == "seed":
            return "Demo data seeded"
        if name == "reset":
            return "Simulation data reset"
        if name == "rollback":
            return f"Execution {str(pr.get('original_action_id', ''))[:8]} rolled back"
        return name

    @app.get("/events")
    def events(from_: str | None = Query(None, alias="from"), to: str | None = None,
               limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(get_principal),
               scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        lo, hi = parse_ts(from_, "from"), parse_ts(to, "to")
        sid = scn.id if scn else 0
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(  # main reads never include scenario rows; a scenario header returns that scenario only
                "SELECT seq, action_id, name, actor, role, source, status, sim_time, recorded_at, params, "
                "(SELECT k FROM jsonb_object_keys(after) k LIMIT 1) AS first_key FROM audit_event "
                "WHERE status = 'executed' AND scenario_id = %(sid)s "
                "AND (%(lo)s::timestamptz IS NULL OR sim_time >= %(lo)s) "
                "AND (%(hi)s::timestamptz IS NULL OR sim_time <= %(hi)s) "
                "ORDER BY seq DESC LIMIT %(n)s", {"lo": lo, "hi": hi, "n": limit, "sid": sid})
            rows = cur.fetchall()
        ids: set[str] = set()
        for r in rows:
            for v in (r["params"] or {}).values():
                if isinstance(v, str) and _UUID_RE.match(v):
                    ids.add(v)
            fk = r["first_key"]
            if fk and r["name"].endswith("_object") and ":" in fk:
                ids.add(fk.split(":", 1)[1])
        nm = names_for(conn, ids, sid)
        return jsonable_encoder([{
            "seq": r["seq"], "action_id": r["action_id"], "name": r["name"], "actor": r["actor"],
            "role": r["role"], "source": r["source"], "status": r["status"],
            "sim_time": r["sim_time"], "recorded_at": r["recorded_at"],
            "params": mask_params(p, r["name"], r["params"]), "summary": summarize(r, nm)} for r in rows])

    # ------------------------------------------------------------ geo
    # types that can be the far end of a Shipment's ships_from/ships_to (a `location` point property)
    geo_endpoint_types = tuple(t for t in ("Port", "Warehouse") if t in om.object_types
                               and any(pr.name == "location" and pr.type == "point"
                                       for pr in om.object_types[t].properties))
    @app.get("/geo/{type}")
    def geo(type: str, bbox: str | None = None, as_of: str | None = None,
            p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
            conn=Depends(rdb)):
        sid = scn.id if scn else 0
        if type not in om.geo_types:
            raise HTTPException(404, f"/geo supports {list(om.geo_types)}, got '{type}'")
        otype(type)
        t = parse_ts(as_of)
        col = geo_col(type, conn)
        if col is None:
            raise HTTPException(422, f"{type} has no geometry column")
        where, params = f'"{col}" IS NOT NULL', []
        if bbox:
            try:
                w, s_, e, n = (float(x) for x in bbox.split(","))
            except ValueError:
                raise HTTPException(422, "bbox must be 'west,south,east,north' numbers")
            where += f' AND "{col}" && ST_MakeEnvelope(%s,%s,%s,%s,4326)'
            params = [w, s_, e, n]
        rows = bt.list_objects(conn, type, t, None, sid, where=where, params=params)
        ends: dict[str, dict[str, list | None]] = {}
        # Shipment origin/destination enrichment: only when the ontology has those links and located endpoints
        enrich = (type == "Shipment" and "ships_from" in om.link_types and "ships_to" in om.link_types
                  and bool(geo_endpoint_types))
        if enrich and rows and sid:
            ids = {r["id"] for r in rows}
            for link, key in (("ships_from", "origin"), ("ships_to", "destination")):
                dst = {l["src"]: l["dst"] for l in bt.list_links(conn, link, t, None, sid) if l["src"] in ids}
                locs: dict[Any, list] = {}
                for ft in geo_endpoint_types:
                    for o in bt.list_objects(conn, ft, t, None, sid, where="id = ANY(%s)",
                                             params=[list(set(dst.values()))]):
                        loc = o.get("location")
                        if loc:
                            locs[o["id"]] = loc["coordinates"]
                for src_, d_ in dst.items():
                    if d_ in locs:
                        ends.setdefault(str(src_), {})[key] = list(locs[d_])
        elif enrich and rows:
            ids = [r["id"] for r in rows]
            union = " UNION ALL ".join(
                f"SELECT id, location AS loc FROM {_o(ft)} WHERE {_vt('', T_ASOF)}" for ft in geo_endpoint_types)
            for link, key in (("ships_from", "origin"), ("ships_to", "destination")):
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT l.src, ST_X(g.loc), ST_Y(g.loc) FROM link_{link} l JOIN ({union}) g ON g.id = l.dst "
                        f"WHERE {_vt('l', T_ASOF)} AND l.src = ANY(%(ids)s::uuid[]) AND g.loc IS NOT NULL",
                        {"t": t, "ids": ids})
                    for src, x, y in cur.fetchall():
                        ends.setdefault(str(src), {})[key] = [x, y]
        feats = []
        for r in rows:
            props = {k: v for k, v in r.items()
                     if k not in HIDDEN and k not in ("changed_by", col, "id")}
            props = {"id": r["id"], "type": type, **props}
            if enrich:
                e = ends.get(str(r["id"]), {})
                props["origin"], props["destination"] = e.get("origin"), e.get("destination")
            feats.append({"type": "Feature", "id": r["id"], "geometry": r[col], "properties": props})
        return jsonable_encoder({"type": "FeatureCollection", "features": feats})

    # ------------------------------------------------------------ stats
    # The legacy supply-chain summary keys (and the hand-written value_at_risk SQL) stay available when the
    # ontology has the types/properties they read.
    legacy_stats = kpis._builtin_ok(om, "value_at_risk") and kpis._builtin_ok(om, "invoices_flagged") \
        and "Event" in om.object_types and any(pr.name == "ended" for pr in om.object_types["Event"].properties)

    @app.get("/stats/summary")
    def stats_summary(as_of: str | None = None, p: Principal = Depends(get_principal),
                      scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        """`{as_of, counts: {Type: n}, enums: {Type: {prop: {value: n}}}, kpis: {key: number}}` for the types the
        caller can read (masked enum properties are omitted), plus the legacy supply-chain keys when the ontology
        has those types."""
        t = parse_ts(as_of)
        sid = scn.id if scn else 0
        role = om.roles[p.role]
        readable = [n for n in om.object_types if role.can_read(n)]
        enum_spec = {}
        for n in readable:
            masked = set(role.types[n].mask)
            ps = [pr.name for pr in om.object_types[n].properties if pr.type == "enum" and pr.name not in masked]
            if ps:
                enum_spec[n] = ps
        res: dict[str, Any] = {
            "as_of": _iso(t) if t else "live",
            "counts": kpis.object_counts(conn, om, readable, sid, t),
            "enums": kpis.enum_counts(conn, om, enum_spec, sid, t),
            "kpis": kpis.declared(conn, p, sid, t, ontology=om),
        }
        if not legacy_stats:
            return res
        if scn is not None:  # scenario-effective numbers (same shape), read through the overlay views
            def counts_s(type_: str, col: str, keys: tuple[str, ...]) -> dict[str, int]:
                got = kpis.counts(conn, type_, col, sid, t)
                return {k: got.get(k, 0) for k in keys}
            res.update({
                "shipments": counts_s("Shipment", "status", ("planned", "in_transit", "delayed", "arrived")),
                "ports": counts_s("Port", "status", ("open", "closed")),
                "invoices": counts_s("Invoice", "status", ("open", "approved", "paid", "flagged")),
                "value_at_risk": kpis.METRICS["value_at_risk"](conn, sid, t),
                "delay_hours_total": kpis.METRICS["delay_hours_total"](conn, sid, t),
                "open_events": kpis.open_events(conn, sid, t),
            })
            return res
        prm = {"t": t}

        def counts(type_: str, col: str, keys: tuple[str, ...]) -> dict[str, int]:
            out = dict.fromkeys(keys, 0)
            for k, c in conn.execute(
                    f"SELECT {col}, count(*) FROM {_o(type_)} x WHERE {_vt('x', T_ASOF)} GROUP BY {col}", prm):
                if k in out:
                    out[k] = c
            return out
        res.update({
            "shipments": counts("Shipment", "status", ("planned", "in_transit", "delayed", "arrived")),
            "ports": counts("Port", "status", ("open", "closed")),
            "invoices": counts("Invoice", "status", ("open", "approved", "paid", "flagged")),
        })
        row = conn.execute(
            f"SELECT {_var_sql(T_ASOF)}, "
            f"(SELECT coalesce(sum(delay_hours), 0)::float8 FROM {_o('Shipment')} x WHERE {_vt('x', T_ASOF)}), "
            f"(SELECT count(*) FROM {_o('Event')} x WHERE {_vt('x', T_ASOF)} AND x.ended IS NULL)", prm).fetchone()
        res["value_at_risk"], res["delay_hours_total"], res["open_events"] = row
        return res

    @app.get("/stats/series")
    def stats_series(metric: str, from_: str | None = Query(None, alias="from"), to: str | None = None,
                     step_hours: float = Query(24, gt=0), p: Principal = Depends(get_principal),
                     scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        keys = list(kpis.metric_keys(om))
        legacy = scn is None and legacy_stats and metric in METRICS
        if scn is not None or not legacy:
            if metric not in keys:
                raise HTTPException(422, f"metric must be one of {keys}")
        hi, lo = parse_ts(to, "to"), parse_ts(from_, "from")
        if hi is None:
            hi = (scn.clock if scn is not None and scn.clock else None) or get_state_now()
        if lo is None:
            lo = hi - timedelta(days=30)
        if hi < lo:
            raise HTTPException(422, "'from' must not be after 'to'")
        if (hi - lo) / timedelta(hours=step_hours) > MAX_SERIES_POINTS:
            raise HTTPException(422, f"too many points (max {MAX_SERIES_POINTS}); raise step_hours")
        if not legacy:  # scenarios, and declared / non-legacy KPIs on main
            try:
                res = kpis.series(conn, p, scn.id if scn else 0, metric, lo, hi, step_hours, ontology=om)
            except ValueError as e:
                raise HTTPException(422, str(e))
            return {"metric": metric, "points": [{"t": x["t"], "v": x["v"]} for x in res["points"]]}
        rows = conn.execute(
            f"SELECT ts.t, {_metric_sql(metric, 'ts.t')} FROM generate_series(%(lo)s::timestamptz, "
            "%(hi)s::timestamptz, (%(step)s::float8 * interval '1 hour')) AS ts(t) ORDER BY ts.t",
            {"lo": lo, "hi": hi, "step": step_hours}).fetchall()
        return jsonable_encoder({"metric": metric, "points": [{"t": t, "v": v} for t, v in rows]})

    def get_state_now() -> datetime:
        """Default series end: the runner's sim_now, else the latest sim time (read as the loader: the
        simulation clock is not sensitive, and a scoped role only sees its own audit rows)."""
        r = state["runner"]
        if r is not None:
            try:
                v = r.state().get("sim_now")
                d = v if isinstance(v, datetime) else parse_ts(str(v))
                if d:
                    return d
            except Exception:  # noqa: BLE001
                pass
        v = None
        try:
            c = connect(dsn, schema=schema)
            try:
                v = c.execute("SELECT max(sim_time) FROM audit_event").fetchone()[0]
            finally:
                c.close()
        except psycopg.Error:
            v = None
        return v or now_utc()

    # ------------------------------------------------------------ graph
    @app.get("/graph")
    def graph(center: UUID, depth: int = Query(1, ge=1, le=3), as_of: str | None = None,
              include_inferred: bool = True, p: Principal = Depends(get_principal),
              scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        if scn is not None:
            return _scenario_graph(center, depth, as_of, include_inferred, p, scn, conn)
        t = parse_ts(as_of)
        live = t is None
        pl = get_pipeline() if live else None
        if pl is not None:
            after_write()  # pick up rows written by another process (e.g. the headless sim CLI)
        try:
            res = V.bfs(conn, center, depth, pipeline=pl, as_of=t, include_inferred=include_inferred,
                        max_nodes=MAX_GRAPH_NODES, is_admin=p.role == "admin",
                        deadline=time.monotonic() + GRAPH_DEADLINE_S, cfg=cfg)
        except V.NotVisible:
            raise HTTPException(404, f"object {center} not found")
        except TimeoutError:
            raise ApiError(408, "timeout", "graph query timed out")
        infos = V.node_infos(conn, res.order, as_of=t, cfg=cfg, model=om)
        nodes = [infos[str(i)] for i in res.order if str(i) in infos]
        ok = {n["id"] for n in nodes}
        edges = [{"src": str(s_), "dst": str(d), "link": l, "inferred": False} for s_, l, d in res.edges
                 if str(s_) in ok and str(d) in ok]
        edges += [{"src": str(s_), "dst": str(d), "link": l, "inferred": True} for s_, l, d in res.inferred
                  if str(s_) in ok and str(d) in ok]
        available = res.inferred_available or (live and pl is not None and not include_inferred)
        return {"center": str(center), "as_of": "live" if live else _iso(t),
                "inferred_available": available, "truncated": res.truncated,
                "nodes": nodes, "edges": edges}

    # ------------------------------------------------------------ SPARQL
    sparql_slots = threading.BoundedSemaphore(4)

    # ------------------------------------------------------------ scenario graph / sparql
    def _scenario_store(conn, scn, p=None):
        from engine.sync import scenario_graph as SG
        try:
            return SG.get_store(conn, scn, comp, dsn=dsn, schema=schema)
        except SG.GraphTooLarge:
            # the size is data about ALL of the scenario's rows (also ones the caller cannot see): only the owner
            # and admins are told why; everybody else gets a generic 403 (review L2)
            row = conn.execute("SELECT owner_user FROM scenario WHERE id = %s", [scn.id]).fetchone()
            if p is not None and (p.role == "admin" or (row is not None and row[0] == p.user)):
                raise ApiError(413, "graph_too_large", "scenario is too large for graph queries")
            raise ApiError(403, "graph_unavailable", "graph queries are not available for this scenario")
        except SG.GraphBusy:
            raise ApiError(503, "graph_busy", "graph builds are busy; retry shortly")
        except SG.ScenarioNotVisible:
            raise ApiError(404, "scenario_not_found", "scenario not found")

    def _scenario_graph(center, depth, as_of, include_inferred, p, scn, conn):
        if as_of not in (None, "", "live"):
            raise ApiError(400, "scenario_as_of_unsupported", "as_of is not supported inside a scenario")
        graph_ = _scenario_store(conn, scn, p)
        try:
            res = V.bfs(conn, center, depth, pipeline=graph_, include_inferred=include_inferred,
                        max_nodes=MAX_GRAPH_NODES, is_admin=p.role == "admin",
                        deadline=time.monotonic() + GRAPH_DEADLINE_S, cfg=cfg, scenario=scn)
        except V.NotVisible:
            raise HTTPException(404, f"object {center} not found")
        except TimeoutError:
            raise ApiError(408, "timeout", "graph query timed out")
        infos = V.node_infos(conn, res.order, cfg=cfg, model=om, scenario=scn)
        nodes = [infos[str(i)] for i in res.order if str(i) in infos]
        ok = {n["id"] for n in nodes}
        edges = [{"src": str(s_), "dst": str(d), "link": l, "inferred": False} for s_, l, d in res.edges
                 if str(s_) in ok and str(d) in ok]
        edges += [{"src": str(s_), "dst": str(d), "link": l, "inferred": True} for s_, l, d in res.inferred
                  if str(s_) in ok and str(d) in ok]
        return {"center": str(center), "as_of": "live", "scenario": scn.id,
                "inferred_available": res.inferred_available, "truncated": res.truncated,
                "nodes": nodes, "edges": edges}

    def _scenario_sparql(body, p, scn, conn):
        from engine.sync.sparql import SparqlError, run_query
        graph_ = _scenario_store(conn, scn, p)
        if not sparql_slots.acquire(blocking=False):
            raise ApiError(429, "too_many_requests", "too many concurrent queries")
        try:
            return run_query(graph_, conn, p.role == "admin", body.query, scenario=scn)
        except SparqlError as e:
            raise ApiError(408 if e.code == "timeout" else 400, f"sparql_{e.code}", str(e))
        finally:
            sparql_slots.release()


    @app.post("/sparql")
    def sparql(body: SparqlBody, p: Principal = Depends(get_principal),
               scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        if scn is not None:
            return _scenario_sparql(body, p, scn, conn)
        from engine.sync.sparql import SparqlError, run_query
        pl = get_pipeline()
        if pl is None:
            raise ApiError(503, "unavailable", "semantic layer unavailable")
        if not sparql_slots.acquire(blocking=False):
            raise ApiError(429, "too_many_requests", "too many concurrent queries")
        try:
            after_write()
            return run_query(pl, conn, p.role == "admin", body.query)
        except SparqlError as e:
            raise ApiError(408 if e.code == "timeout" else 400, f"sparql_{e.code}", str(e))
        finally:
            sparql_slots.release()

    # ------------------------------------------------------------ actions
    def param_info(a) -> list[dict]:
        return [{"name": s.name, "type": s.base, "required": s.required, "ref": s.ref} for s in a.param_specs()]

    def applies_to(a, type_: str) -> bool:
        return any(b.type == type_ for b in a.bind.values()) or any(s.ref == type_ for s in a.param_specs())

    @app.get("/actions")
    def list_actions(type: str | None = None, id: UUID | None = None, p: Principal = Depends(get_principal),
                     scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        if type is not None:
            otype(type)
        obj = None
        if type is not None and id is not None:
            obj = bt.get_object(conn, type, id, scenario_id=scn.id if scn else 0)
        out = []
        for n, a in om.actions.items():
            if type is not None and not applies_to(a, type):
                continue
            allowed, reason = True, None
            if not om.role_can_execute(p.role, n):
                allowed, reason = False, f"role {p.role!r} may not execute {n!r}"
            elif type is not None and id is not None:
                if obj is None:
                    allowed, reason = False, f"{type} {id} not found"
                else:
                    allowed, reason = check_criteria(a, type, id, obj)
            out.append({"name": n, "description": a.description, "params": param_info(a),
                        "requires_approval": bool(a.requires_approval and p.role not in a.approval_exempt),
                        "allowed": allowed, "reason": reason})
        return out

    def check_criteria(a, type_, id_, obj) -> tuple[bool, str | None]:
        """Cheap pre-check: criteria that only need the bound object (others are skipped)."""
        for al, b in a.bind.items():
            if b.type == type_ and b.id.startswith("$"):
                params = {b.id[1:]: id_}
                aliases = {al: {**{k: v for k, v in obj.items()}, "id": id_}}
                for cr in a.criteria:
                    try:
                        ok = exprs.evaluate(cr.ast if cr.ast is not None else exprs.parse_expr(cr.expr),
                                            params=params, aliases=aliases, now=now_utc(), actor=None)
                    except Exception:  # noqa: BLE001 - needs other params: cannot decide here
                        continue
                    if not ok:
                        return False, cr.message
        return True, None

    def need_action(name: str):
        if name not in om.actions:
            raise ApiError(404, "unknown_action", f"unknown action {name!r}")
        return om.actions[name]

    def exec_payload(res, p: Principal) -> dict:
        return jsonable_encoder(mask_out(p, {
            "action_id": res.action_id, "status": "executed", "changed": res.changed,
            "webhooks": res.webhooks, "created": res.created, "diff": res.diff}))

    @app.post("/actions/{name}/execute")
    def execute_action(name: str, body: ExecBody = Body(default_factory=ExecBody),
                       p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                       conn=Depends(wconn)):
        need_action(name)
        cap_params(body.params)
        vt = now_utc()
        if body.valid_at is not None:
            if p.role != "admin":
                raise ApiError(403, "permission_denied", "only admin may backdate or set valid_at")
            vt = body.valid_at
        try:
            res = run_action(conn, name, body.params, p, vt, scn)
        except ApprovalRequired:
            try:
                pid = propose(conn, name, body.params, p, body.rationale, valid_at=vt, source="api",
                              ontology=comp, scenario_id=scn.id if scn else 0)
            except Exception as e:  # noqa: BLE001
                fail(conn, e)
            return JSONResponse(jsonable_encoder({"proposal_id": pid, "status": "pending_approval"}),
                                status_code=202)
        return exec_payload(res, p)

    @app.post("/actions/{name}/propose", status_code=201)
    def propose_action(name: str, body: ProposeBody = Body(default_factory=ProposeBody),
                       p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                       conn=Depends(wconn)):
        need_action(name)
        cap_params(body.params)
        if body.valid_at is not None and p.role != "admin":
            raise ApiError(403, "permission_denied", "only admin may set valid_at")
        try:
            pid = propose(conn, name, body.params, p, body.rationale, valid_at=body.valid_at or now_utc(),
                          source="api", ontology=comp, scenario_id=scn.id if scn else 0)
        except Exception as e:  # noqa: BLE001
            fail(conn, e)
        return {"proposal_id": str(pid)}

    # ------------------------------------------------------------ proposals
    PROPOSAL_SQL = (
        "SELECT p.id, p.action, p.params, p.proposer, p.proposer_role, p.rationale, p.dry_run, p.created_at, "
        "p.on_behalf_of, p.evidence, p.run_id, p.expires_at, (p.expires_at < clock_timestamp()) AS expired, "
        "p.scenario_id, "
        "a.decision, a.approver, a.approver_role, a.comment, a.decided_at, a.execution_id "
        "FROM proposal p LEFT JOIN approval a ON a.proposal_id = p.id")

    def proposal_json(r: dict, p: Principal) -> dict:
        r = mask_out(p, {**r, "name": r["action"]})
        r.pop("name", None)
        r["state"] = "pending" if r["decision"] is None else r["decision"]
        r["stale"] = None  # not computed in the list (decide() blocks stale approvals with 409)
        r["approval"] = None if r["decision"] is None else {
            k: r[k] for k in ("decision", "approver", "approver_role", "comment", "decided_at", "execution_id")}
        return jsonable_encoder(r)

    @app.get("/proposals")
    def list_proposals(state_: str = Query("pending", alias="state", pattern="^(pending|all)$"),
                       limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(get_principal),
                       scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        # main inbox: every proposal the caller may see, including those tested in a scenario (their
        # `scenario_id` is the evidence scenario; they execute on main). A scenario header: that scenario's only.
        where = [] if state_ != "pending" else ["a.proposal_id IS NULL"]
        args: list = []
        if scn is not None:
            where.append("p.scenario_id = %s")
            args.append(scn.id)
        q = PROPOSAL_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.seq DESC LIMIT %s"
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(q, [*args, limit])
            return [proposal_json(r, p) for r in cur.fetchall()]

    def decide_route(pid: UUID, approve: bool, body: CommentBody, p: Principal, conn):
        try:
            res = decide(conn, pid, p, approve, body.comment, source="api", ontology=comp)
        except Exception as e:  # noqa: BLE001
            fail(conn, e)
        if approve:
            after_write()
            return {**exec_payload(res, p), "proposal_id": str(pid), "status": "approved"}
        return {"proposal_id": str(pid), "status": "rejected"}

    @app.post("/proposals/{pid}/approve")
    def approve_proposal(pid: UUID, body: CommentBody = Body(default_factory=CommentBody),
                         p: Principal = Depends(get_principal), _ns: None = Depends(no_scenario),
                         conn=Depends(wconn)):
        return decide_route(pid, True, body, p, conn)

    @app.post("/proposals/{pid}/reject")
    def reject_proposal(pid: UUID, body: CommentBody = Body(default_factory=CommentBody),
                        p: Principal = Depends(get_principal), _ns: None = Depends(no_scenario),
                        conn=Depends(wconn)):
        return decide_route(pid, False, body, p, conn)

    # ------------------------------------------------------------ executions
    EXEC_COLS = ("seq, action_id, name, actor, role, source, status, sim_time, recorded_at, on_behalf_of, scenario_id, "
                 "proposal_id, parent_execution_id, params, "
                 "after->>'error' AS error, EXISTS (SELECT 1 FROM rollback rb WHERE rb.original_action_id = "
                 "audit_event.action_id) AS rolled_back")

    @app.get("/executions")
    def list_executions(limit: int = Query(100, ge=1, le=1000), name: str | None = None,
                        actor: str | None = None, status: str | None = None,
                        p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                        conn=Depends(rdb)):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {EXEC_COLS} FROM audit_event WHERE scenario_id = %(sid)s "
                "AND (%(name)s::text IS NULL OR name = %(name)s) "
                "AND (%(actor)s::text IS NULL OR actor = %(actor)s) "
                "AND (%(status)s::text IS NULL OR status = %(status)s) ORDER BY seq DESC LIMIT %(n)s",
                {"name": name, "actor": actor, "status": status, "n": limit, "sid": scn.id if scn else 0})
            return jsonable_encoder(mask_out(p, cur.fetchall()))

    def safe_err(e):
        """Delivery error text for non-admins: only fixed, non-revealing tokens."""
        if e is None:
            return None
        return e if re.fullmatch(r"HTTP \d{3}|no url|no secret|bad url scheme|\w+Error|TimeoutError", e) else "error"

    def mask_out(p: Principal, x):
        """THE masking function for every response (see engine/api/masking.py)."""
        return masking.mask_out(om, p, x)

    def mask_diff(p: Principal, diff):
        return masking.mask_diff(om, p, diff)

    def mask_summary(p: Principal, snap):
        return masking.mask_summary(om, p, snap)

    def mask_params(p: Principal, name, params):
        return masking.mask_params(om, p, name, params)

    @app.get("/executions/{action_id}")
    def get_execution(action_id: UUID, p: Principal = Depends(get_principal),
                      scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {EXEC_COLS}, before, after FROM audit_event WHERE action_id = %s "
                        "AND scenario_id = %s", [action_id, scn.id if scn else 0])
            ex = cur.fetchone()
            if ex is None:
                raise HTTPException(404, f"execution {action_id} not found")
            cur.execute("SELECT kind, type, key, op, before, after FROM execution_diff WHERE action_id = %s "
                        "ORDER BY seq", [action_id])
            diff = cur.fetchall()
            cur.execute("SELECT id, hook, url, idempotency_key, created_at FROM webhook_outbox "
                        "WHERE action_id = %s ORDER BY seq", [action_id])
            hooks = cur.fetchall()
            for h in hooks:
                cur.execute("SELECT seq, attempt, status, http_status, error, next_attempt_at, at "
                            "FROM webhook_delivery WHERE outbox_id = %s ORDER BY seq", [h["id"]])
                dl = cur.fetchall()
                if p.role == "admin":
                    h["outbox"] = {k: h[k] for k in ("id", "hook", "url", "idempotency_key", "created_at")}
                else:  # hook name + host only; never the URL (path/query may carry credentials)
                    host = urlsplit(h["url"]).hostname if h["url"] else None
                    h["outbox"] = {"id": h["id"], "hook": h["hook"], "host": host, "created_at": h["created_at"]}
                    for d_ in dl:
                        d_["error"] = safe_err(d_["error"])
                h["deliveries"] = dl
                h["state"] = dl[-1]["status"] if dl else None
            cur.execute("SELECT id, original_action_id, rollback_action_id, actor, reason, at FROM rollback "
                        "WHERE original_action_id = %s", [action_id])
            rb = cur.fetchone()
        ex, diff = mask_out(p, ex), mask_diff(p, diff)
        return jsonable_encoder({
            "execution": ex, "diff": diff,
            "webhooks": [{"outbox": h["outbox"], "state": h["state"], "deliveries": h["deliveries"]} for h in hooks],
            "rollback": rb})

    @app.post("/executions/{action_id}/rollback")
    def rollback_execution(action_id: UUID, body: RollbackBody = Body(default_factory=RollbackBody),
                           p: Principal = Depends(get_principal), scn: ScenarioCtx | None = Depends(get_scenario),
                           conn=Depends(wconn)):
        try:
            res = rollback(conn, action_id, p, body.reason, source="api", ontology=comp,
                           scenario_id=scn.id if scn else 0)
        except Exception as e:  # noqa: BLE001
            fail(conn, e)
        if scn is None:
            after_write()
        return jsonable_encoder(mask_out(p, {"action_id": res.action_id, "status": "executed",
                                             "changed": res.changed, "rollback_of": action_id,
                                             "diff": res.diff}))

    @app.post("/webhooks/{outbox_id}/retry")
    def retry_webhook(outbox_id: UUID, p: Principal = Depends(get_principal), _ns: None = Depends(no_scenario),
                      conn=Depends(wconn)):
        with read_scope(conn, p):
            seen = conn.execute("SELECT 1 FROM webhook_outbox WHERE id = %s", [outbox_id]).fetchone()
        if not seen:
            raise HTTPException(404, f"webhook {outbox_id} not found")
        oc = connect(dsn, schema=schema)  # delivery bookkeeping is done by the loader, like the worker
        try:
            outbox_mod.retry(oc, outbox_id)
        except Exception as e:  # noqa: BLE001
            fail(oc, e)
        finally:
            oc.close()
        return {"outbox_id": str(outbox_id), "status": "pending"}

    # ------------------------------------------------------------ audit feed
    @app.get("/audit")
    def audit_feed(kind: Literal["execution", "proposal", "approval", "webhook", "rollback"] | None = None,
                   limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(get_principal),
                   scn: ScenarioCtx | None = Depends(get_scenario), conn=Depends(rdb)):
        items: list[dict] = []
        sid = scn.id if scn else 0
        # main: scenario rows are excluded, except proposals tested in a scenario (evidence; they execute on main)
        pscn = "WHERE scenario_id = %s " if scn else ""
        pargs = [sid] if scn else []
        with conn.cursor(row_factory=dict_row) as cur:
            if kind in (None, "execution"):
                cur.execute(f"SELECT {EXEC_COLS} FROM audit_event WHERE scenario_id = %s ORDER BY seq DESC LIMIT %s",
                            [sid, limit])
                for r in cur.fetchall():
                    items.append({"kind": "execution", "at": r["recorded_at"], "actor": r["actor"],
                                  "role": r["role"], "name": r["name"], "status": r["status"],
                                  "id": r["action_id"], "action_id": r["action_id"],
                                  "proposal_id": r["proposal_id"], "data": r})
            if kind in (None, "proposal"):
                cur.execute("SELECT id, action, params, proposer, proposer_role, rationale, created_at, scenario_id "
                            f"FROM proposal {pscn}ORDER BY seq DESC LIMIT %s", [*pargs, limit])
                for r in cur.fetchall():
                    items.append({"kind": "proposal", "at": r["created_at"], "actor": r["proposer"],
                                  "role": r["proposer_role"], "name": r["action"], "status": "pending",
                                  "id": r["id"], "proposal_id": r["id"], "data": r})
            if kind in (None, "approval"):
                cur.execute("SELECT a.id, a.proposal_id, a.decision, a.approver, a.approver_role, a.comment, "
                            "a.decided_at, a.execution_id, p.action FROM approval a "
                            "LEFT JOIN proposal p ON p.id = a.proposal_id "
                            + ("WHERE p.scenario_id = %s " if scn else "") + "ORDER BY a.seq DESC LIMIT %s",
                            [*pargs, limit])
                for r in cur.fetchall():
                    items.append({"kind": "approval", "at": r["decided_at"], "actor": r["approver"],
                                  "role": r["approver_role"], "name": r["action"], "status": r["decision"],
                                  "id": r["id"], "proposal_id": r["proposal_id"],
                                  "action_id": r["execution_id"], "data": r})
            if kind in (None, "webhook") and not scn:  # webhooks never exist inside a scenario
                cur.execute("SELECT d.seq, d.outbox_id, d.attempt, d.status, d.http_status, d.error, d.at, "
                            "o.hook, o.action_id FROM webhook_delivery d JOIN webhook_outbox o ON o.id = d.outbox_id "
                            "ORDER BY d.seq DESC LIMIT %s", [limit])
                for r in cur.fetchall():
                    if p.role != "admin":
                        r["error"] = safe_err(r["error"])
                    items.append({"kind": "webhook", "at": r["at"], "actor": None, "role": None,
                                  "name": r["hook"], "status": r["status"], "id": r["outbox_id"],
                                  "action_id": r["action_id"], "data": r})
            if kind in (None, "rollback"):
                cur.execute("SELECT id, original_action_id, rollback_action_id, actor, reason, at FROM rollback "
                            "WHERE scenario_id = %s ORDER BY seq DESC LIMIT %s", [sid, limit])
                for r in cur.fetchall():
                    items.append({"kind": "rollback", "at": r["at"], "actor": r["actor"], "role": None,
                                  "name": "rollback", "status": "executed", "id": r["id"],
                                  "action_id": r["rollback_action_id"], "data": r})
        for i in items:
            i.setdefault("proposal_id", None)
            i.setdefault("action_id", None)
            d = i["data"]
            i["summary"] = {
                "execution": f"{i['name']} by {i['actor']} ({i['status']})",
                "proposal": f"{i['name']} proposed by {i['actor']}",
                "approval": f"{i['name']} {i['status']} by {i['actor']}",
                "webhook": f"webhook {i['name']} {i['status']} (attempt {d.get('attempt')})",
                "rollback": f"rollback of {str(d.get('original_action_id'))[:8]} by {i['actor']}"}[i["kind"]]
        items.sort(key=lambda i: i["at"], reverse=True)
        return jsonable_encoder(mask_out(p, items[:limit]))

    # ------------------------------------------------------------ scenarios (Phase 5)
    from engine.api import ontology_routes
    app.include_router(ontology_routes.make_router(get_principal, wconn))

    def scn_error(conn, e: Exception):
        fail(conn, e)

    def scn_view(d: dict, p: Principal) -> dict:
        """Review L2: counters and the run id are owner/admin data (they describe rows and activity a shared
        viewer may not see)."""
        if p.role == "admin" or d.get("owner_user") == p.user:
            return d
        return {**d, "override_rows": None, "data_version": None, "run_id": None}

    @app.post("/scenarios", status_code=201)
    def create_scenario(body: ScenarioBody, p: Principal = Depends(get_principal), conn=Depends(wconn)):
        try:
            d = scenarios.create(conn, p, body.name, expires_in_h=body.expires_in_h,
                                 shared_roles=body.shared_roles, ontology=comp)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        return jsonable_encoder(scn_view(d, p))

    @app.get("/scenarios")
    def list_scenarios(status_: str = Query("open", alias="status", pattern="^(open|all|merged|discarded|expired)$"),
                       p: Principal = Depends(get_principal), conn=Depends(wconn)):
        try:
            scenarios.expire_due(conn)  # lazy janitor
            rows = scenarios.list_(conn, p, status_)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        return jsonable_encoder([scn_view(r, p) for r in rows])

    @app.get("/scenarios/{sid}")
    def get_scenario_route(sid: int, p: Principal = Depends(get_principal), conn=Depends(wconn)):
        try:
            return jsonable_encoder(scn_view(scenarios.get(conn, p, sid), p))
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)

    @app.delete("/scenarios/{sid}")
    def discard_scenario(sid: int, p: Principal = Depends(get_principal), conn=Depends(wconn)):
        try:
            return scenarios.discard(conn, p, sid)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)

    @app.get("/scenarios/{sid}/compare")
    def compare_scenario(sid: int, with_: str = Query("main", alias="with"), limit: int = Query(500, ge=1, le=5000),
                         p: Principal = Depends(get_principal), conn=Depends(wconn)):
        # a = the path scenario, b = `with`; diff.before = state on b, diff.after = state on a; delta = a - b
        if with_ == "main":
            other = 0
        else:
            try:
                other = int(with_)
            except ValueError:
                raise ApiError(422, "invalid", "with must be 'main' or a scenario id")
            if other < 0:
                raise ApiError(422, "invalid", "with must be 'main' or a scenario id")
        try:
            res = scenarios.compare(conn, p, sid, other, limit=limit, ontology=comp)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        return jsonable_encoder(mask_out(p, res))

    @app.post("/scenarios/{sid}/merge")
    def merge_scenario(sid: int, body: MergeBody = Body(default_factory=MergeBody),
                       p: Principal = Depends(get_principal), conn=Depends(wconn)):
        try:
            res = scenarios.merge(conn, p, sid, body.comment or "", ontology=comp)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        after_write()  # main changed
        return {"status": "merged", **res}

    @app.get("/scenarios/{sid}/series")
    def scenario_series(sid: int, metric: str | None = None, step_hours: float = Query(24, gt=0),
                        from_: str | None = Query(None, alias="from"), to: str | None = None,
                        p: Principal = Depends(get_principal), conn=Depends(wconn)):
        keys = list(kpis.metric_keys(om))
        if metric is None and keys:
            metric = "value_at_risk" if "value_at_risk" in keys else keys[0]
        if metric not in keys:
            raise ApiError(422, "invalid", f"metric must be one of {keys}")
        hi, lo = parse_ts(to, "to"), parse_ts(from_, "from")
        try:
            info = scenarios.get(conn, p, sid)
            if info["status"] != "open":
                raise ScenarioNotFound(f"scenario {sid} not found")
            hi = hi or info["clock"] or get_state_now()
            lo = lo or hi - timedelta(days=30)
            return jsonable_encoder(kpis.series(conn, p, sid, metric, lo, hi, step_hours, ontology=om))
        except LookupError as e:  # scenario closed / invisible while we worked
            scn_error(conn, ScenarioNotFound(str(e)))
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)

    # what-if simulation: background threads, in-process registry (one uvicorn worker; lost on restart).
    # `sim.whatif.run` is the Simulation role's entry point; until it exists a job ends in error "whatif not available".
    sim_jobs: dict[str, dict] = {}
    sim_lock = threading.Lock()
    from sim import limits as sim_limits
    app.state.sim_jobs = sim_jobs

    def sim_worker(job_id: str, sid: int, p: Principal, days: int, seed: int, injections: list[dict]):
        try:
            _sim_worker_body(job_id, sid, p, days, seed, injections)
        finally:
            sim_limits.release_sim_slot()

    def _sim_worker_body(job_id: str, sid: int, p: Principal, days: int, seed: int, injections: list[dict]):
        try:
            try:
                import importlib
                run = importlib.import_module("sim.whatif").run
            except (ImportError, AttributeError):
                raise RuntimeError("whatif not available") from None
            factory = lambda: connect(dsn, schema=schema)  # noqa: E731
            c0 = factory()
            try:
                ctx = scenario_admin.ctx_of(c0, sid)
            finally:
                c0.close()
            res = run(factory, ctx, days=days, seed=seed, injections=injections)
            c1 = factory()
            try:
                scenarios.record_event(c1, p, sid, "simulated", {
                    "job_id": job_id, "days": days, "seed": seed, "injections": injections,
                    "digest": getattr(res, "digest", None)})
            finally:
                c1.close()
            with sim_lock:
                sim_jobs[job_id].update(
                    status="done", digest=getattr(res, "digest", None), actions=getattr(res, "actions", None),
                    sim_now=getattr(res, "sim_now", None))
        except Exception as e:  # noqa: BLE001 - never leak driver / SQL text
            log.warning("what-if job %s failed: %s", job_id, e)
            with sim_lock:
                sim_jobs[job_id].update(status="error",
                                        error=str(e)[:200] if isinstance(e, (RuntimeError, ValueError)) else type(e).__name__)

    # injection kind -> the object type its target must be (the simulated world is built from the FULL world, so
    # the caller's own visibility is checked here: a hidden target is indistinguishable from a missing one)
    INJECT_TARGET_TYPE = {"port_closure": "Port", "shipment_delay": "Shipment", "supplier_delay": "Supplier",
                          "price_shock": "Supplier", "invoice_anomaly": "Invoice"}

    def check_injection_targets(conn, p: Principal, sid: int, injections) -> None:
        targets = [(i.kind, i.target) for i in injections if i.target is not None]
        if not targets:
            return
        try:
            ctx = scenarios.resolve(conn, p, sid)
            with read_scope(conn, p, ctx):
                for kind, target in targets:
                    typ = INJECT_TARGET_TYPE.get(kind)
                    if typ not in om.object_types or bt.get_object(conn, typ, target, scenario_id=sid) is None:
                        raise ApiError(422, "invalid", "injection target not found")
        except ApiError:
            conn.rollback()
            raise
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        conn.rollback()

    @app.post("/scenarios/{sid}/simulate", status_code=202)
    def simulate_scenario(sid: int, body: SimulateBody, p: Principal = Depends(get_principal), conn=Depends(wconn)):
        if om.sim is None:
            raise sim_unavailable()
        max_days = int(os.environ.get("ISNAD_SCN_SIM_MAX_DAYS", "90"))
        if not 1 <= body.days <= max_days:
            raise ApiError(422, "invalid", f"days must be 1..{max_days}")
        try:
            scenarios.resolve(conn, p, sid)
            info = scenarios.get(conn, p, sid)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        if p.role != "admin" and info["owner_user"] != p.user:
            raise ApiError(403, "permission_denied", "only the owner or an admin can simulate in a scenario")
        check_injection_targets(conn, p, sid, body.injections)
        job_id = uuid4().hex
        with sim_lock:
            for j in [k for k, v in sim_jobs.items() if v["status"] != "running" and time.time() - v["t0"] > 3600]:
                del sim_jobs[j]
            running = [v for v in sim_jobs.values() if v["status"] == "running"]
            if any(v["scenario_id"] == sid for v in running):
                raise ApiError(409, "simulation_running", "a simulation is already running in this scenario")
            if not sim_limits.acquire_sim_slot(0):
                raise ApiError(429, "too_many_requests", "too many simulations are running")
            sim_jobs[job_id] = {"status": "running", "scenario_id": sid, "user": p.user, "t0": time.time(),
                                "sim_now": None, "digest": None, "actions": None, "error": None}
        try:
            threading.Thread(target=sim_worker, name=f"whatif-{job_id[:6]}", daemon=True, args=(
                job_id, sid, p, body.days, body.seed,
                [i.model_dump(mode="json", exclude_none=True) for i in body.injections])).start()
        except Exception:  # noqa: BLE001
            sim_limits.release_sim_slot()
            with sim_lock:
                sim_jobs.pop(job_id, None)
            raise
        return {"job_id": job_id}

    @app.get("/scenarios/{sid}/simulate/{job_id}")
    def simulate_status(sid: int, job_id: str, p: Principal = Depends(get_principal), conn=Depends(wconn)):
        if om.sim is None:
            raise sim_unavailable()
        try:
            scenarios.resolve(conn, p, sid)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        with sim_lock:
            j = dict(sim_jobs.get(job_id) or {})
        if not j or j["scenario_id"] != sid:
            raise ApiError(404, "job_not_found", "job not found")
        try:
            info = scenarios.get(conn, p, sid)
        except Exception as e:  # noqa: BLE001
            scn_error(conn, e)
        mine = p.role == "admin" or info["owner_user"] == p.user  # review L2: digest/actions are owner/admin data
        out = {"status": j["status"], "sim_now": j["sim_now"], "digest": j["digest"] if mine else None,
               "actions": j["actions"] if mine else None, "kpis": None, "error": j["error"]}
        if j["status"] == "done":  # KPIs are recomputed for THIS viewer (RLS), never copied from the starter's view
            try:
                out["kpis"] = kpis.compute(conn, p, sid, ontology=om)
            except Exception as e:  # noqa: BLE001
                scn_error(conn, e)
        return jsonable_encoder(out)

    from engine.api import agent_routes
    agent_routes.mount(app, comp=comp, get_principal=get_principal, connect_fn=lambda: connect(dsn, schema=schema),
                       get_pipeline=get_pipeline, model_factory=agent_model_factory)
    return app


def _default_app() -> FastAPI:
    return create_app()


app = _default_app()
