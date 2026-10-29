"""Generic FastAPI app over a compiled ontology. Run: uvicorn engine.api.main:app"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from engine.compiler import compile_ontology
from engine.db import bitemporal as bt
from engine.db import connect

ROLES = ("planner", "analyst", "admin")
DEFAULT_ONTOLOGY = "ontology/supply_chain.yaml"
HIDDEN = ("valid_during", "recorded_during", "scenario_id", "action_id")


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


def create_app(ontology_path: str | None = None, schema: str | None = None,
               dsn: str | None = None) -> FastAPI:
    path = ontology_path or os.environ.get("AMARH_ONTOLOGY", DEFAULT_ONTOLOGY)
    schema = schema or os.environ.get("AMARH_SCHEMA")
    comp = compile_ontology(path)
    om = comp.model
    models = comp.pydantic_models()
    app = FastAPI(title="Amarh ontology API")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    app.state.ontology = comp

    def db():
        conn = connect(dsn, schema=schema)
        try:
            yield conn
        finally:
            conn.close()

    def otype(t: str):
        if t not in om.object_types:
            raise HTTPException(404, f"unknown object type '{t}'")
        return getattr(models, t)

    def ltype(l: str):
        if l not in om.link_types:
            raise HTTPException(404, f"unknown link type '{l}'")
        return om.link_types[l]

    def write(conn, fn, *a, **kw):
        try:
            changed = fn(conn, *a, **kw)
            conn.commit()
            return changed
        except ValueError as e:
            conn.rollback()
            raise HTTPException(422, str(e))
        except psycopg.errors.ExclusionViolation as e:
            conn.rollback()
            raise HTTPException(409, str(e))

    def validated(model, id_: UUID, body: dict) -> dict:
        try:
            m = model.model_validate({**body, "id": id_})
        except ValidationError as e:
            raise HTTPException(422, jsonable_encoder(e.errors(include_url=False, include_context=False, include_input=False)))
        return m.model_dump(exclude={"id"})

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
                     role: str = Depends(get_role), conn=Depends(db)):
        otype(type)
        rows = bt.list_objects(conn, type, as_of_valid, as_of_recorded)
        return [_row(r) for r in rows[offset:offset + limit]]

    @app.post("/objects/{type}", status_code=201)
    def create_object(type: str, body: dict[str, Any] = Body(...), role: str = Depends(get_role),
                      conn=Depends(db)):
        model = otype(type)
        body = dict(body)
        id_ = UUID(str(body.pop("id"))) if body.get("id") else uuid4()
        vf = body.pop("valid_from", None)
        vf = datetime.fromisoformat(vf) if vf else datetime.now(UTC)
        if bt.get_object(conn, type, id_) is not None:
            conn.rollback()
            raise HTTPException(409, f"{type} {id_} already exists")
        props = validated(model, id_, body)
        write(conn, bt.write_object, type, id_, props, vf, role)
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
        vf = body.pop("valid_from", None)
        vf = datetime.fromisoformat(vf) if vf else datetime.now(UTC)
        if bt.get_object(conn, type, id) is None:
            conn.rollback()
            raise HTTPException(404, f"{type} {id} not found")
        props = validated(model, id, body)
        write(conn, bt.write_object, type, id, props, vf, role)
        return _row(bt.get_object(conn, type, id))

    @app.delete("/objects/{type}/{id}")
    def delete_object(type: str, id: UUID, role: str = Depends(get_role), conn=Depends(db)):
        otype(type)
        if bt.get_object(conn, type, id) is None:
            conn.rollback()
            raise HTTPException(404, f"{type} {id} not found")
        write(conn, bt.delete_object, type, id, datetime.now(UTC), role)
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
        lt = ltype(link)
        try:
            src, dst = UUID(str(body["src"])), UUID(str(body["dst"]))
        except (KeyError, ValueError):
            raise HTTPException(422, "body needs uuid 'src' and 'dst'")
        vf = body.get("valid_from")
        vf = datetime.fromisoformat(vf) if vf else datetime.now(UTC)
        write(conn, bt.write_link, link, src, dst, vf, role, src_type=lt.src, dst_type=lt.dst)
        return {"src": str(src), "dst": str(dst)}

    @app.delete("/links/{link}")
    def delete_link(link: str, src: UUID, dst: UUID, role: str = Depends(get_role), conn=Depends(db)):
        ltype(link)
        write(conn, bt.end_link, link, src, dst, datetime.now(UTC), role)
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
                    o = bt.get_object(conn, other_t, oid, as_of)
                    out.append({"link": l.name, "direction": direction, "type": other_t,
                                "id": str(oid), "object": _row(o) if o else None})
        return out

    return app


def _default_app() -> FastAPI:
    return create_app()


app = _default_app()
