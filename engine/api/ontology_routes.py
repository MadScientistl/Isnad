"""Admin-only ontology versioning routes. Mounted by the main session:
    app.include_router(ontology_routes.make_router(get_principal, wconn))
`get_conn` is a FastAPI dependency yielding a plain (owner) connection; this module commits its own writes.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from engine import versioning as V


def _j(rows):
    return rows  # FastAPI's jsonable_encoder handles datetime/uuid


def make_router(get_principal, get_conn) -> APIRouter:
    r = APIRouter(prefix="/ontology", tags=["ontology"])

    def admin(p=Depends(get_principal)):
        if getattr(p, "role", None) != "admin":
            raise HTTPException(403, "admin only")
        return p

    def run(conn, fn, *a, **kw):
        try:
            out = fn(conn, *a, **kw)
            conn.commit()
            return out
        except V.VersioningError as e:
            conn.rollback()
            raise HTTPException(e.status, e.message)
        except V.OntologyError as e:
            conn.rollback()
            raise HTTPException(422, str(e))

    @r.get("/versions")
    def versions(p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.list_versions, p)

    @r.get("/versions/{vid}")
    def version(vid: int, p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.version_detail, p, vid)

    @r.get("/diff")
    def diff(from_: int = Query(..., alias="from"), to: int = Query(...), p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.diff_versions, p, from_, to)

    @r.post("/proposals")
    def propose(body: dict[str, Any] = Body(...), p=Depends(admin), conn=Depends(get_conn)):
        y = body.get("yaml")
        if not isinstance(y, str) or not y.strip():
            raise HTTPException(422, "yaml (string) required")
        if len(y) > 200_000:
            raise HTTPException(422, "yaml too large (max 200000 characters)")
        return run(conn, V.propose, p, y, str(body.get("rationale") or ""))

    @r.get("/proposals")
    def proposals(p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.list_proposals, p)

    @r.post("/proposals/{pid}/approve")
    def approve(pid: UUID, body: dict[str, Any] = Body(default={}), p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.decide, p, pid, True, str(body.get("comment") or ""))

    @r.post("/proposals/{pid}/reject")
    def reject(pid: UUID, body: dict[str, Any] = Body(default={}), p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.decide, p, pid, False, str(body.get("comment") or ""))

    @r.post("/versions/{vid}/rollback")
    def rollback(vid: int, p=Depends(admin), conn=Depends(get_conn)):
        return run(conn, V.rollback, p, vid)

    return r
