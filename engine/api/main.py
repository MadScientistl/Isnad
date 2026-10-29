"""Generic FastAPI app over a compiled ontology. Run: uvicorn engine.api.main:app

All mutations go through `engine.actions.apply_action(source="api", actor=role)`.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel, ValidationError

from engine.actions import apply_action, ensure_audit_schema
from engine.compiler import compile_ontology
from engine.db import bitemporal as bt
from engine.db import connect

log = logging.getLogger("amarh.api")

ROLES = ("planner", "analyst", "admin")
DEFAULT_ONTOLOGY = "ontology/supply_chain.yaml"
HIDDEN = ("valid_during", "recorded_during", "scenario_id", "action_id")
GEO_TYPES = ("Port", "Warehouse", "Shipment")
SPEEDS = ("paused", "1", "6", "24")
METRICS = ("shipments_in_transit", "shipments_delayed", "ports_closed", "invoices_flagged",
           "value_at_risk")
MAX_GRAPH_NODES = 300
MAX_SERIES_POINTS = 2000
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def get_role(x_role: str | None = Header(default=None)) -> str:
    """Single seam for permissions. Stub: validates the role name only (no enforcement yet)."""
    role = x_role or "admin"
    if role not in ROLES:
        raise HTTPException(403, f"unknown role '{role}'; expected one of {list(ROLES)}")
    return role


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


def _var_sql(t: str) -> str:
    """Scalar subquery: value at risk (cargo of non-arrived shipments delayed or via a closed port)."""
    return (
        "(SELECT coalesce(sum(s.cargo_value), 0)::float8 FROM obj_shipment s "
        f"WHERE {_vt('s', t)} AND s.status IS DISTINCT FROM 'arrived' AND (s.status = 'delayed' OR EXISTS ("
        "SELECT 1 FROM link_ships_via l JOIN obj_port p ON p.id = l.dst "
        f"WHERE l.src = s.id AND {_vt('l', t)} AND {_vt('p', t)} AND p.status = 'closed')))")


def _metric_sql(metric: str, t: str) -> str:
    cnt = "(SELECT count(*)::float8 FROM {tbl} x WHERE {vt} AND x.{col} = '{val}')"
    if metric == "shipments_in_transit":
        return cnt.format(tbl="obj_shipment", vt=_vt("x", t), col="status", val="in_transit")
    if metric == "shipments_delayed":
        return cnt.format(tbl="obj_shipment", vt=_vt("x", t), col="status", val="delayed")
    if metric == "ports_closed":
        return cnt.format(tbl="obj_port", vt=_vt("x", t), col="status", val="closed")
    if metric == "invoices_flagged":
        return cnt.format(tbl="obj_invoice", vt=_vt("x", t), col="status", val="flagged")
    return _var_sql(t)


class ControlBody(BaseModel):
    speed: str | int | None = None
    step: bool | None = None
    reset: bool | None = None


class InjectBody(BaseModel):
    kind: Literal["port_closure", "shipment_delay", "invoice_anomaly"]
    target: UUID | None = None
    at: datetime | None = None
    duration_h: float | None = None


def create_app(ontology_path: str | None = None, schema: str | None = None,
               dsn: str | None = None, runner: Any = None, pipeline: Any = None) -> FastAPI:
    """`runner` / `pipeline`: optional injected doubles. `pipeline=False` disables sync entirely."""
    path = ontology_path or os.environ.get("AMARH_ONTOLOGY", DEFAULT_ONTOLOGY)
    schema = schema or os.environ.get("AMARH_SCHEMA")
    comp = compile_ontology(path)
    om = comp.model
    models = comp.pydantic_models()

    state = {"runner": runner, "runner_failed": False,
             "pipeline": pipeline if pipeline is not None else
             (False if os.environ.get("AMARH_PIPELINE", "1") == "0" else None),
             "audit_ready": False, "reasoning": False}
    plock = threading.RLock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            c = connect(dsn, schema=schema)
            try:
                ensure_audit_schema(c)
                state["audit_ready"] = True
            finally:
                c.close()
        except Exception as e:  # database may be down at startup; endpoints will report it
            log.warning("startup audit schema check failed: %s", e)
        if os.environ.get("AMARH_SIM_AUTOSTART", "0") == "1":
            try:
                get_runner().start()
            except HTTPException:
                pass
        yield
        r = state["runner"]
        if r is not None:
            try:
                r.stop()
            except Exception:  # noqa: BLE001
                pass

    app = FastAPI(title="Amarh ontology API", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    app.state.ontology = comp
    app.state.runtime = state

    def db():
        conn = connect(dsn, schema=schema)
        try:
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------- runtime pieces
    def get_pipeline():
        p = state["pipeline"]
        if p is False:
            return None
        if p is None:
            try:
                from engine.sync.pipeline import SyncPipeline
                p = state["pipeline"] = SyncPipeline(dsn=dsn, schema=schema)
                with plock:  # first use: mirror what is already in Postgres, then reason once
                    p.sync()
                    p.maybe_reason(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("sync pipeline unavailable: %s", e)
                state["pipeline"] = False
                return None
        return p

    def get_runner():
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

    def ensure_audit(conn):
        if not state["audit_ready"]:
            ensure_audit_schema(conn)
            state["audit_ready"] = True

    def act(conn, name: str, params: dict, role: str, valid_at: datetime):
        try:
            ensure_audit(conn)
            res = apply_action(conn, name, params, role, valid_at=valid_at, source="api", ontology=comp)
        except ValueError as e:
            conn.rollback()
            raise HTTPException(422, str(e))
        except psycopg.errors.ExclusionViolation as e:
            conn.rollback()
            raise HTTPException(409, str(e).splitlines()[0])
        except (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation, psycopg.DataError) as e:
            conn.rollback()
            raise HTTPException(422, str(e).splitlines()[0])
        except psycopg.IntegrityError as e:
            conn.rollback()
            raise HTTPException(409, str(e).splitlines()[0])
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
            return datetime.now(UTC)
        d = parse_ts(str(v), "valid_from")
        return d or datetime.now(UTC)

    def geo_col(type_: str, conn) -> str | None:
        g = bt.geometry_columns(conn, type_)
        return g[0] if g else None

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
    def types(role: str = Depends(get_role)):
        return {
            "namespace": om.namespace,
            "classes": {c.name: {"extends": c.extends} for c in om.classes.values()},
            "objectTypes": [{"name": t.name, "properties": [
                {"name": p.name, "type": p.type, "required": p.required, "enum": list(p.enum)}
                for p in t.properties]} for t in om.object_types.values()],
            "linkTypes": [{"name": l.name, "from": l.src, "to": l.dst, "cardinality": l.cardinality,
                           "required": l.required, "transitive": l.transitive, "inverse": l.inverse}
                          for l in om.link_types.values()],
            "role": role,
        }

    # ---------------------------------------------------------- objects
    @app.get("/objects/{type}")
    def list_objects(type: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                     as_of_valid: datetime | None = None, as_of_recorded: datetime | None = None,
                     bbox: str | None = None, order_by: str | None = None, desc: bool = False,
                     role: str = Depends(get_role), conn=Depends(db)):
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
            rows = bt.list_objects(conn, type, as_of_valid, as_of_recorded, where=where, params=params,
                                   limit=limit, offset=offset, order_by=order_by, desc=desc)
        except ValueError as e:
            conn.rollback()
            raise HTTPException(422, str(e))
        return [_row(r) for r in rows]

    @app.post("/objects/{type}", status_code=201)
    def create_object(type: str, body: dict[str, Any] = Body(...), role: str = Depends(get_role),
                      conn=Depends(db)):
        model = otype(type)
        body = dict(body)
        try:
            id_ = UUID(str(body.pop("id"))) if body.get("id") else uuid4()
        except ValueError:
            raise HTTPException(422, "id must be a uuid")
        vf = vfrom(body.pop("valid_from", None))
        if bt.get_object(conn, type, id_) is not None:
            conn.rollback()
            raise HTTPException(409, f"{type} {id_} already exists")
        props = validated(model, id_, body)
        act(conn, "create_object", {"type": type, "id": id_, "props": props}, role, vf)
        return _row(bt.get_object(conn, type, id_))

    @app.get("/objects/{type}/{id}")
    def get_object(type: str, id: UUID, as_of_valid: datetime | None = None,
                   as_of_recorded: datetime | None = None, role: str = Depends(get_role),
                   conn=Depends(db)):
        otype(type)
        r = bt.get_object(conn, type, id, as_of_valid, as_of_recorded)
        if r is None:
            raise HTTPException(404, f"{type} {id} not found")
        return _row(r)

    @app.put("/objects/{type}/{id}")
    def put_object(type: str, id: UUID, body: dict[str, Any] = Body(...),
                   role: str = Depends(get_role), conn=Depends(db)):
        model = otype(type)
        body = dict(body)
        body.pop("id", None)
        vf = vfrom(body.pop("valid_from", None))
        if bt.get_object(conn, type, id) is None:
            conn.rollback()
            raise HTTPException(404, f"{type} {id} not found")
        props = validated(model, id, body)
        act(conn, "update_object", {"type": type, "id": id, "props": props}, role, vf)
        return _row(bt.get_object(conn, type, id))

    @app.delete("/objects/{type}/{id}")
    def delete_object(type: str, id: UUID, role: str = Depends(get_role), conn=Depends(db)):
        otype(type)
        if bt.get_object(conn, type, id) is None:
            conn.rollback()
            raise HTTPException(404, f"{type} {id} not found")
        act(conn, "delete_object", {"type": type, "id": id}, role, datetime.now(UTC))
        return {"deleted": str(id)}

    @app.get("/objects/{type}/{id}/history")
    def object_history(type: str, id: UUID, role: str = Depends(get_role), conn=Depends(db)):
        otype(type)
        return [_row(r, history=True) for r in bt.history(conn, type, id)]

    # ------------------------------------------------------------ links
    @app.get("/links/{link}")
    def get_links(link: str, id: UUID | None = None, direction: str = Query("out", pattern="^(out|in)$"),
                  as_of: datetime | None = None, as_of_recorded: datetime | None = None,
                  role: str = Depends(get_role), conn=Depends(db)):
        ltype(link)
        rows = bt.list_links(conn, link, as_of, as_of_recorded)
        if id is not None:
            key = "src" if direction == "out" else "dst"
            rows = [r for r in rows if r[key] == id]
        return [{"src": str(r["src"]), "dst": str(r["dst"])} for r in rows]

    @app.post("/links/{link}", status_code=201)
    def create_link(link: str, body: dict[str, Any] = Body(...), role: str = Depends(get_role),
                    conn=Depends(db)):
        ltype(link)
        try:
            src, dst = UUID(str(body["src"])), UUID(str(body["dst"]))
        except (KeyError, ValueError):
            raise HTTPException(422, "body needs uuid 'src' and 'dst'")
        act(conn, "create_link", {"link": link, "src": src, "dst": dst}, role, vfrom(body.get("valid_from")))
        return {"src": str(src), "dst": str(dst)}

    @app.delete("/links/{link}")
    def delete_link(link: str, src: UUID, dst: UUID, role: str = Depends(get_role), conn=Depends(db)):
        ltype(link)
        act(conn, "end_link", {"link": link, "src": src, "dst": dst}, role, datetime.now(UTC))
        return {"deleted": {"src": str(src), "dst": str(dst)}}

    @app.get("/neighbours/{type}/{id}")
    def neighbours(type: str, id: UUID, as_of: datetime | None = None,
                   role: str = Depends(get_role), conn=Depends(db)):
        otype(type)
        out = []
        for l in om.link_types.values():
            for direction, mine, other_t in (("out", l.src, l.dst), ("in", l.dst, l.src)):
                if mine != type:
                    continue
                for oid in bt.neighbours(conn, l.name, id, direction, as_of):
                    o = bt.get_object(conn, other_t, oid, as_of) if other_t in om.object_types else None
                    out.append({"link": l.name, "direction": direction, "type": other_t,
                                "id": str(oid), "object": _row(o) if o else None})
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
    def get_sim_state(role: str = Depends(get_role)):
        return sim_state(get_runner())

    @app.post("/sim/control")
    def sim_control(body: ControlBody, role: str = Depends(get_role), conn=Depends(db)):
        r = get_runner()
        if body.speed is not None and str(body.speed) not in SPEEDS:
            raise HTTPException(422, f"speed must be one of {list(SPEEDS)}")
        try:
            if body.reset:
                if os.environ.get("AMARH_ALLOW_RESET") != "1":
                    raise HTTPException(403, "reset deletes all data in this schema; set AMARH_ALLOW_RESET=1 to allow it")
                r.stop()
                tables = [row[0] for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
                    "AND table_type = 'BASE TABLE' AND (table_name LIKE 'obj\\_%' OR table_name LIKE 'link\\_%' "
                    "OR table_name IN ('graph_outbox', 'audit_event'))").fetchall()]
                if tables:
                    conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
                        sql.SQL(", ").join(sql.Identifier(t) for t in tables)))
                conn.commit()
                state["audit_ready"] = True  # table kept, only emptied
                r.reset()
                from engine.api.seed import seed
                seed(conn)
                p = get_pipeline()
                if p is not None:
                    with plock:
                        p.rebuild()
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
        if body.step or body.reset:
            after_write()
        return sim_state(r)

    @app.post("/sim/inject")
    def sim_inject(body: InjectBody, role: str = Depends(get_role)):
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
        return jsonable_encoder({"injected": out})

    # ------------------------------------------------------------ events
    label_cols = {t.name: next((c for c in ("name", "ref", "kind") if any(p.name == c for p in t.properties)), None)
                  for t in om.object_types.values()}

    def names_for(conn, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        arr = [UUID(i) for i in ids]
        out: dict[str, str] = {}
        with conn.cursor(row_factory=dict_row) as cur:
            for t, col in label_cols.items():
                if col is None:
                    continue
                cur.execute(
                    f'SELECT DISTINCT ON (id) id, "{col}" AS label FROM {bt.obj_table(t)} '
                    "WHERE scenario_id = 0 AND id = ANY(%s::uuid[]) AND "
                    "(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity') "
                    "ORDER BY id, lower(valid_during) DESC", [arr])
                for r in cur.fetchall():
                    out[str(r["id"])] = r["label"]
        return out

    def summarize(r: dict, nm: dict[str, str]) -> str:
        p = r["params"] or {}

        def n(key):
            v = p.get(key)
            return nm.get(str(v), str(v)[:8]) if v else "?"
        name = r["name"]
        if name == "close_port":
            return f"Port {n('port')} closed"
        if name == "reopen_port":
            return f"Port {n('port')} reopened"
        if name == "delay_shipment":
            return f"Shipment {n('shipment')} delayed {float(p.get('delay_hours', 0)):g} h"
        if name == "ship_depart":
            return f"Shipment {n('shipment')} departed"
        if name == "ship_arrive":
            return f"Shipment {n('shipment')} arrived"
        if name == "ship_position":
            return f"Shipment {n('shipment')} position updated"
        if name == "issue_invoice":
            return f"Invoice issued for Shipment {n('shipment')} ({float(p.get('amount', 0)):,.0f} {p.get('currency') or 'USD'})"
        if name == "flag_invoice":
            return f"Invoice {n('invoice')} flagged: {p.get('anomaly')}"
        if name == "pay_invoice":
            return f"Invoice {n('invoice')} paid"
        if name in ("create_object", "update_object", "delete_object"):
            verb = {"create_object": "created", "update_object": "updated", "delete_object": "deleted"}[name]
            oid = p.get("id") or (r["first_key"] or ":").split(":", 1)[1] or None
            return f"{p.get('type', 'Object')} {nm.get(str(oid), str(oid or '')[:8])} {verb}".replace("  ", " ")
        if name in ("create_link", "end_link"):
            verb = "linked" if name == "create_link" else "unlinked"
            return f"{p.get('link')}: {n('src')} {verb} {n('dst')}"
        return name

    @app.get("/events")
    def events(from_: str | None = Query(None, alias="from"), to: str | None = None,
               limit: int = Query(100, ge=1, le=1000), role: str = Depends(get_role), conn=Depends(db)):
        ensure_audit(conn)
        lo, hi = parse_ts(from_, "from"), parse_ts(to, "to")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT seq, action_id, name, actor, source, sim_time, recorded_at, params, "
                "(SELECT k FROM jsonb_object_keys(after) k LIMIT 1) AS first_key FROM audit_event "
                "WHERE (%(lo)s::timestamptz IS NULL OR sim_time >= %(lo)s) "
                "AND (%(hi)s::timestamptz IS NULL OR sim_time <= %(hi)s) "
                "ORDER BY seq DESC LIMIT %(n)s", {"lo": lo, "hi": hi, "n": limit})
            rows = cur.fetchall()
        ids: set[str] = set()
        for r in rows:
            for v in (r["params"] or {}).values():
                if isinstance(v, str) and _UUID_RE.match(v):
                    ids.add(v)
            fk = r["first_key"]
            if fk and r["name"].endswith("_object") and ":" in fk:
                ids.add(fk.split(":", 1)[1])
        nm = names_for(conn, ids)
        return jsonable_encoder([{
            "seq": r["seq"], "action_id": r["action_id"], "name": r["name"], "actor": r["actor"],
            "source": r["source"], "sim_time": r["sim_time"], "recorded_at": r["recorded_at"],
            "params": r["params"], "summary": summarize(r, nm)} for r in rows])

    # ------------------------------------------------------------ geo
    @app.get("/geo/{type}")
    def geo(type: str, bbox: str | None = None, as_of: str | None = None,
            role: str = Depends(get_role), conn=Depends(db)):
        if type not in GEO_TYPES:
            raise HTTPException(404, f"/geo supports {list(GEO_TYPES)}, got '{type}'")
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
        rows = bt.list_objects(conn, type, t, None, where=where, params=params)
        ends: dict[str, dict[str, list | None]] = {}
        if type == "Shipment" and rows:
            ids = [r["id"] for r in rows]
            for link, key in (("ships_from", "origin"), ("ships_to", "destination")):
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT l.src, ST_X(g.loc), ST_Y(g.loc) FROM link_{link} l JOIN ("
                        f"SELECT id, location AS loc FROM obj_port WHERE {_vt('', T_ASOF)} UNION ALL "
                        f"SELECT id, location FROM obj_warehouse WHERE {_vt('', T_ASOF)}) g ON g.id = l.dst "
                        f"WHERE {_vt('l', T_ASOF)} AND l.src = ANY(%(ids)s::uuid[]) AND g.loc IS NOT NULL",
                        {"t": t, "ids": ids})
                    for src, x, y in cur.fetchall():
                        ends.setdefault(str(src), {})[key] = [x, y]
        feats = []
        for r in rows:
            props = {k: v for k, v in r.items()
                     if k not in HIDDEN and k not in ("changed_by", col, "id")}
            props = {"id": r["id"], "type": type, **props}
            if type == "Shipment":
                e = ends.get(str(r["id"]), {})
                props["origin"], props["destination"] = e.get("origin"), e.get("destination")
            feats.append({"type": "Feature", "id": r["id"], "geometry": r[col], "properties": props})
        return jsonable_encoder({"type": "FeatureCollection", "features": feats})

    # ------------------------------------------------------------ stats
    @app.get("/stats/summary")
    def stats_summary(as_of: str | None = None, role: str = Depends(get_role), conn=Depends(db)):
        t = parse_ts(as_of)
        p = {"t": t}

        def counts(table: str, col: str, keys: tuple[str, ...]) -> dict[str, int]:
            out = dict.fromkeys(keys, 0)
            for k, c in conn.execute(
                    f"SELECT {col}, count(*) FROM {table} x WHERE {_vt('x', T_ASOF)} GROUP BY {col}", p):
                if k in out:
                    out[k] = c
            return out
        res = {
            "as_of": _iso(t) if t else "live",
            "shipments": counts("obj_shipment", "status", ("planned", "in_transit", "delayed", "arrived")),
            "ports": counts("obj_port", "status", ("open", "closed")),
            "invoices": counts("obj_invoice", "status", ("open", "approved", "paid", "flagged")),
        }
        row = conn.execute(
            f"SELECT {_var_sql(T_ASOF)}, "
            f"(SELECT coalesce(sum(delay_hours), 0)::float8 FROM obj_shipment x WHERE {_vt('x', T_ASOF)}), "
            f"(SELECT count(*) FROM obj_event x WHERE {_vt('x', T_ASOF)} AND x.ended IS NULL)", p).fetchone()
        res["value_at_risk"], res["delay_hours_total"], res["open_events"] = row
        return res

    @app.get("/stats/series")
    def stats_series(metric: str, from_: str | None = Query(None, alias="from"), to: str | None = None,
                     step_hours: float = Query(24, gt=0), role: str = Depends(get_role), conn=Depends(db)):
        if metric not in METRICS:
            raise HTTPException(422, f"metric must be one of {list(METRICS)}")
        hi, lo = parse_ts(to, "to"), parse_ts(from_, "from")
        if hi is None:
            r = get_state_now(conn)
            hi = r
        if lo is None:
            lo = hi - timedelta(days=30)
        if hi < lo:
            raise HTTPException(422, "'from' must not be after 'to'")
        if (hi - lo) / timedelta(hours=step_hours) > MAX_SERIES_POINTS:
            raise HTTPException(422, f"too many points (max {MAX_SERIES_POINTS}); raise step_hours")
        rows = conn.execute(
            f"SELECT ts.t, {_metric_sql(metric, 'ts.t')} FROM generate_series(%(lo)s::timestamptz, "
            "%(hi)s::timestamptz, (%(step)s::float8 * interval '1 hour')) AS ts(t) ORDER BY ts.t",
            {"lo": lo, "hi": hi, "step": step_hours}).fetchall()
        return jsonable_encoder({"metric": metric, "points": [{"t": t, "v": v} for t, v in rows]})

    def get_state_now(conn) -> datetime:
        """Default series end: injected/created runner's sim_now, else the latest audited sim time."""
        r = state["runner"]
        if r is not None:
            try:
                v = r.state().get("sim_now")
                d = v if isinstance(v, datetime) else parse_ts(str(v))
                if d:
                    return d
            except Exception:  # noqa: BLE001
                pass
        try:
            ensure_audit(conn)
            v = conn.execute("SELECT max(sim_time) FROM audit_event").fetchone()[0]
        except psycopg.Error:
            conn.rollback()
            v = None
        return v or datetime.now(UTC)

    # ------------------------------------------------------------ graph
    ot_info = []
    for t in om.object_types.values():
        pn = {p.name for p in t.properties}
        lab = next((c for c in ("name", "ref", "kind") if c in pn), None)
        ot_info.append((t.name, f'"{lab}"::text' if lab else "NULL::text",
                        '"status"::text' if "status" in pn else "NULL::text"))
    link_names = list(om.link_types)

    def node_infos(conn, ids: list[UUID], t) -> dict[str, dict]:
        if not ids:
            return {}
        q = " UNION ALL ".join(
            f"SELECT id, '{n}'::text AS type, {lab} AS label, {st} AS status FROM {bt.obj_table(n)} x "
            f"WHERE {_vt('x', T_ASOF)} AND id = ANY(%(ids)s::uuid[])" for n, lab, st in ot_info)
        out = {}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(q, {"t": t, "ids": ids})
            for r in cur.fetchall():
                out[str(r["id"])] = {"id": str(r["id"]), "type": r["type"],
                                     "label": r["label"] or str(r["id"])[:8], "status": r["status"]}
        return out

    def edges_touching(conn, ids: list[UUID], t) -> list[tuple[str, str, str]]:
        q = " UNION ALL ".join(
            f"SELECT src, dst, '{l}'::text AS link FROM {bt.link_table(l)} x WHERE {_vt('x', T_ASOF)} "
            "AND (src = ANY(%(ids)s::uuid[]) OR dst = ANY(%(ids)s::uuid[]))" for l in link_names)
        return [(str(s), str(d), l) for s, d, l in conn.execute(q, {"t": t, "ids": ids}).fetchall()]

    @app.get("/graph")
    def graph(center: UUID, depth: int = Query(1, ge=1, le=3), as_of: str | None = None,
              include_inferred: bool = True, role: str = Depends(get_role), conn=Depends(db)):
        t = parse_ts(as_of)
        nodes = node_infos(conn, [center], t)
        if str(center) not in nodes:
            raise HTTPException(404, f"object {center} not found")
        order = [str(center)]
        seen_edges: dict[tuple[str, str, str], None] = {}
        frontier, truncated = [center], False
        for _ in range(depth):
            if not frontier or truncated:
                break
            edges = edges_touching(conn, frontier, t)
            cand = []
            for s, d, l in edges:
                seen_edges[(s, d, l)] = None
                for x in (s, d):
                    if x not in nodes and x not in cand:
                        cand.append(x)
            found = node_infos(conn, [UUID(x) for x in cand], t)
            frontier = []
            for x in cand:  # BFS order
                if x not in found:
                    continue
                if len(nodes) >= MAX_GRAPH_NODES:
                    truncated = True
                    break
                nodes[x] = found[x]
                order.append(x)
                frontier.append(UUID(x))
        edges_out = [{"src": s, "dst": d, "link": l, "inferred": False}
                     for (s, d, l) in seen_edges if s in nodes and d in nodes]
        live = t is None
        p = get_pipeline() if live else None
        if p is not None:
            after_write()  # pick up rows written by another process (e.g. the headless sim CLI)
        available = p is not None
        if available and include_inferred:
            try:
                have = {(e["src"], e["dst"], e["link"]) for e in edges_out}
                with plock:
                    inf = p.inferred_edges(list(order))
                for s, l, d in inf:
                    s, d = str(s), str(d)
                    if s in nodes and d in nodes and (s, d, l) not in have:
                        have.add((s, d, l))
                        edges_out.append({"src": s, "dst": d, "link": l, "inferred": True})
            except Exception as e:  # noqa: BLE001
                log.warning("inferred edges unavailable: %s", e)
                available = False
        return {"center": str(center), "as_of": "live" if live else _iso(t),
                "inferred_available": available, "truncated": truncated,
                "nodes": [nodes[i] for i in order], "edges": edges_out}

    return app


def _default_app() -> FastAPI:
    return create_app()


app = _default_app()
