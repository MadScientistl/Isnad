"""Ingest + workspace API (Phase 6). Mounted by the main session:

    app.include_router(ingest_routes.make_router(get_principal, wconn, ontology=om, schema_name=schema,
                                                 model_factory=..., source_registry=None))

Admin only: everyone else gets a plain 404 (`not_found`); with `ISNAD_AUTH` != 1 the routes also need
`ISNAD_ALLOW_IMPORT=1` (else 404 `import_disabled`). Errors are `{code, detail}` JSON and never echo request or data
content (request-validation errors are rewritten to field names only). Uploads are raw bodies streamed to disk with the
`ISNAD_MAX_UPLOAD_BYTES` limit enforced while reading. All writes to ingest_* go through `engine.ingest.store` under
`scoped(conn, principal)`; the heavy work (model proposals, repairs, imports) runs as jobs in `engine.ingest.jobs`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from engine.db import connect
from engine.db.principal import Principal, scoped
from engine.ingest import jobs as J
from engine.ingest import store as S
from engine.ingest import workspaces as W
from engine.ingest.importer import auth_on, default_validate, run_import, _report_dict
from engine.ingest.names import clean_text
from engine.ingest.profile import Caps, baseline_proposal, profile_source
from engine.ingest.sources import load_named_sources
from engine.ingest.sources.base import SourceError, SourceLimits
from engine.ingest.store import IngestError

log = logging.getLogger("isnad.ingest_api")
KEEPALIVE_S = 10.0
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")
TOP_EDITABLE = {"objectTypes", "linkTypes", "classes", "actions", "roles", "rationale", "notes"}
ACCESS_RANK = {"read": 0, "write": 1, "admin": 2}


class ApiErr(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


def _err_response(status: int, code: str, detail: str, extra: dict | None = None, headers=None) -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail, **(extra or {})}, status_code=status, headers=headers)


class _Route(APIRoute):
    """Turns ApiErr / IngestError / request-validation errors into sanitised `{code, detail}` bodies."""

    def get_route_handler(self) -> Callable:
        inner = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await inner(request)
            except ApiErr as e:
                hdr = {"Retry-After": str(e.extra["retry_after"])} if "retry_after" in e.extra else None
                return _err_response(e.status, e.code, e.detail, e.extra, hdr)
            except IngestError as e:
                return _err_response(e.status, e.code, e.message, e.extra)
            except RequestValidationError as e:
                errs = []
                for x in e.errors()[:10]:
                    loc = [str(l) if re.fullmatch(r"[A-Za-z_0-9]{1,40}", str(l)) else "?" for l in x.get("loc", ())[:4]]
                    if x.get("type") == "extra_forbidden" and loc:
                        loc[-1] = "?"          # the name of an unknown field is client input: never echoed
                    errs.append({"loc": loc, "type": str(x.get("type", ""))[:40]})
                return _err_response(422, "invalid_request", "the request is invalid", {"errors": errs})
        return handler


# ---------------------------------------------------------------- request models
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposalBody(_Body):
    mode: Literal["baseline", "model"] = "baseline"


class ItemBody(_Body):
    path: str = Field(min_length=1, max_length=300)
    action: Literal["accept", "reject", "edit"]
    value: Any = None


class ItemsBody(_Body):
    items: list[ItemBody] = Field(min_length=1, max_length=50)


class ApproveBody(_Body):
    comment: str = Field(default="", max_length=4000)
    items: list[ItemBody] = Field(default_factory=list, max_length=50)


class RejectBody(_Body):
    comment: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------- proposal edits
def _seg_list(path: str) -> list[str]:
    segs = [s for s in path.strip().split("/") if s != ""]
    if not segs or len(segs) > 8 or any(len(s) > 80 for s in segs):
        raise ApiErr(422, "invalid_path", "the item path is invalid")
    return segs


def _step(node, seg: str, *, last: bool):
    """Resolve ``seg`` inside a dict (key) or list (index, '-' to append, or the element whose name equals seg)."""
    if isinstance(node, dict):
        return seg
    if isinstance(node, list):
        if seg == "-":
            return len(node) if last else None
        if seg.isdigit() and int(seg) < len(node):
            return int(seg)
        for i, el in enumerate(node):
            if isinstance(el, dict) and el.get("name") == seg:
                return i
    return None


def apply_items(prop: dict, items: list[dict]) -> dict:
    """Apply accept / reject / edit items (JSON-pointer-like paths, list elements addressed by index or name)."""
    out = copy.deepcopy(prop)
    for it in items:
        segs = _seg_list(it["path"])
        if segs[0] not in TOP_EDITABLE:
            raise ApiErr(422, "path_not_editable", "that part of the proposal cannot be edited")
        if it["action"] == "accept":
            continue
        node = out
        for seg in segs[:-1]:
            k = _step(node, seg, last=False)
            if k is None:
                raise ApiErr(422, "invalid_path", "the item path does not exist")
            node = node[k]
        k = _step(node, segs[-1], last=True)
        if it["action"] == "reject":
            if k is None or (isinstance(node, list) and k >= len(node)):
                raise ApiErr(422, "invalid_path", "the item path does not exist")
            del node[k]
        else:
            if k is None:
                if isinstance(node, dict):
                    k = segs[-1]
                else:
                    raise ApiErr(422, "invalid_path", "the item path does not exist")
            if isinstance(node, list) and k == len(node):
                node.append(it.get("value"))
            else:
                old_name = node.get("name") if (len(segs) == 3 and segs[0] == "objectTypes" and segs[2] == "name"
                                                and isinstance(node, dict)) else None
                node[k] = it.get("value")
                if isinstance(old_name, str) and isinstance(it.get("value"), str) and old_name != it["value"]:
                    _cascade_type_rename(out, old_name, it["value"])
    return out


def _cascade_type_rename(prop: dict, old: str, new: str) -> None:
    """Renaming an object type keeps the proposal consistent: links, actions, classes and `extends` follow the new name."""
    for ot in prop.get("objectTypes", []):
        if ot.get("extends") == old:
            ot["extends"] = new
    for lt in prop.get("linkTypes", []):
        for end in ("from", "to"):
            if lt.get(end) == old:
                lt[end] = new
    for a in prop.get("actions", []):
        if a.get("type") == old:
            a["type"] = new


def check_edit_policy(old: dict, new: dict) -> None:
    """An edit may not add roles or widen a role's access (roles come from the fixed template)."""
    old_roles = {r["name"]: r for r in old.get("roles", [])}
    for r in new.get("roles", []):
        o = old_roles.get(r.get("name"))
        if o is None:
            raise ApiErr(422, "role_not_allowed", "edits cannot add roles beyond the template")
        if ACCESS_RANK.get(r.get("access"), 9) > ACCESS_RANK.get(o.get("access"), 0):
            raise ApiErr(422, "role_not_allowed", "edits cannot widen a role's access")
    blob = json.dumps(new, default=str).lower()
    for bad in ('"functions"', '"webhooks"', '"webhook"'):
        if bad in blob and bad not in json.dumps(old, default=str).lower():
            raise ApiErr(422, "not_allowed", "functions and webhooks cannot be added by an edit")


# ---------------------------------------------------------------- router
def make_router(get_principal, wconn, *, ontology=None, schema_name: str | None = None, model_factory=None,
                source_registry=None, validate_fn=None, materialize_mod=None, workspaces_root=None,
                ingest_root=None) -> APIRouter:
    r = APIRouter(route_class=_Route, tags=["ingest"])
    registry = J.JobRegistry()
    r.registry = registry  # type: ignore[attr-defined]  (tests / main may inspect it)

    def conn_factory():
        return connect(schema=schema_name)

    def named():
        if source_registry is None:
            return load_named_sources()
        return source_registry() if callable(source_registry) else source_registry

    def vfn(profile, proposal, source, *, namespace=None, slug=None):
        return (validate_fn or default_validate)(profile, proposal, source, namespace=namespace, slug=slug)

    # ------------------------------------------------------------ guards + helpers
    def admin(p: Principal = Depends(get_principal)) -> Principal:
        if getattr(p, "role", None) != "admin":
            raise ApiErr(404, "not_found", "not found")
        if not auth_on() and os.environ.get("ISNAD_ALLOW_IMPORT") != "1":
            raise ApiErr(404, "import_disabled", "ontology import is disabled")
        return p

    @contextmanager
    def tx(conn, p):
        with conn.transaction():
            with scoped(conn, p):
                yield

    def parse_id(raw: str) -> uuid.UUID:
        try:
            return uuid.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            raise ApiErr(404, "not_found", "not found") from None

    def load_session(conn, p, sid: uuid.UUID) -> dict:
        s = S.get_session(conn, sid)
        if s is None:
            raise ApiErr(404, "not_found", "session not found")
        return s

    def session_ns(s: dict):
        slug = W.session_slug(s["source_name"], s["id"])
        from engine.ingest.proposal import derive_namespace
        return slug, derive_namespace(slug)

    def summary(prof: dict) -> dict:
        tabs = prof.get("tables", [])
        return {"tables": len(tabs), "columns": sum(len(t.get("columns", [])) for t in tabs),
                "rows": sum(int(t.get("rows") or 0) for t in tabs), "fks": len(prof.get("fks", [])),
                "warnings": len(prof.get("warnings", [])), "truncated": bool(prof.get("truncated")),
                "profile_hash": prof.get("profile_hash"), "source": prof.get("source")}

    def session_json(s: dict, detail: bool = False, proposals=None) -> dict:
        slug = W.session_slug(s["source_name"], s["id"])
        named_src = str(s["source_name"]).startswith(S.NAMED_PREFIX)
        out = {"id": str(s["id"]), "created_by": s["created_by"], "created_at": s["created_at"],
               "source_kind": s["source_kind"], "named": named_src,
               "source_name": str(s["source_name"])[len(S.NAMED_PREFIX):] if named_src else s["source_name"],
               "source_sha256": s["source_sha256"], "profile_hash": s["profile_hash"], "slug": slug,
               "workspace": W.schema_for(slug)}
        if "proposals" in s:
            out["proposals"] = s["proposals"]
        if detail:
            out["profile"] = s["profile"]
            out["profile_summary"] = summary(s["profile"])
            out["proposals"] = [prop_brief(x) for x in (proposals or [])]
        return out

    def prop_brief(x: dict) -> dict:
        return {"id": str(x["id"]), "seq": x["seq"], "parent_id": str(x["parent_id"]) if x["parent_id"] else None,
                "kind": x["kind"], "model": x["model"], "cost_usd": float(x["cost_usd"]), "ok": x["ok"],
                "created_by": x["created_by"], "created_at": x["created_at"]}

    def check_quota(conn, p):
        with tx(conn, p):
            n = S.recent_sessions(conn, p.user)
        try:
            cap = int(os.environ.get("ISNAD_INGEST_SESSIONS_PER_USER", 10))
        except ValueError:
            cap = 10
        if n >= cap:
            raise ApiErr(429, "rate_limited", "too many ingest sessions in the last 24 hours", retry_after=3600)

    def source_error(e: SourceError) -> ApiErr:
        m = str(e).lower()
        reason = ("limit" if any(k in m for k in ("limit", "too many", "expands", "ratio", "total size")) else
                  "unsafe_archive" if any(k in m for k in ("unsafe", "encrypted", "inconsistent")) else
                  "unreadable")
        return ApiErr(422, "unreadable_source", "the source could not be read safely", reason=reason)

    # ------------------------------------------------------------ uploads
    @r.post("/ingest/uploads", status_code=201)
    async def upload(request: Request, filename: str = Query("", max_length=300), p: Principal = Depends(admin),
                     conn=Depends(wconn)):
        await run_in_threadpool(check_quota, conn, p)
        limit = S.max_upload_bytes()
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            raise ApiErr(413, "too_large", "the upload exceeds the size limit", max_bytes=limit)
        tmp = S.tmp_path(ingest_root)
        h, n, head = hashlib.sha256(), 0, b""
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    n += len(chunk)
                    if n > limit:
                        raise ApiErr(413, "too_large", "the upload exceeds the size limit", max_bytes=limit)
                    if len(head) < 1024:
                        head += chunk[: 1024 - len(head)]
                    h.update(chunk)
                    f.write(chunk)
            if n == 0:
                raise ApiErr(422, "empty_upload", "the upload is empty")
            name = S.safe_filename(filename)
            S.detect_kind(name, head)
            out = await run_in_threadpool(finish_upload, conn, p, tmp, h.hexdigest(), name)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return out

    def finish_upload(conn, p, tmp, sha: str, name: str) -> dict:
        sid = uuid.uuid4()
        path = S.commit_upload(tmp, sid, sha, ingest_root)
        try:
            with S.open_stored(path, name, SourceLimits.from_env()) as src:
                prof = profile_source(src, caps=Caps.from_env())
            with tx(conn, p):
                S.insert_session(conn, sid, p.user, prof["source"]["kind"], name, sha, prof)
        except SourceError as e:
            S.remove_session_files(sid, ingest_root)
            raise source_error(e) from None
        except BaseException:
            S.remove_session_files(sid, ingest_root)
            raise
        return {"session_id": str(sid), "profile_summary": summary(prof)}

    # ------------------------------------------------------------ sources
    @r.get("/ingest/sources")
    def sources(p: Principal = Depends(admin)):
        return [ns.listing() for ns in named().values()]

    @r.post("/ingest/sources/{name}/profile", status_code=201)
    def profile_named(name: str, p: Principal = Depends(admin), conn=Depends(wconn)):
        ns = named().get(name) if NAME_RE.match(name) else None
        if ns is None:
            raise ApiErr(404, "not_found", "source not found")
        check_quota(conn, p)
        try:
            with ns.open(SourceLimits.from_env()) as src:
                prof = profile_source(src, caps=Caps.from_env())
        except SourceError as e:
            raise source_error(e) from None
        except Exception as e:  # noqa: BLE001
            log.warning("named source failed: %s", type(e).__name__)
            raise ApiErr(502, "source_unavailable", "the source could not be reached") from None
        sid = uuid.uuid4()
        with tx(conn, p):
            S.insert_session(conn, sid, p.user, prof["source"]["kind"], S.NAMED_PREFIX + name,
                             prof["source"]["sha256"], prof)
        return {"session_id": str(sid), "profile_summary": summary(prof)}

    # ------------------------------------------------------------ sessions
    @r.get("/ingest/sessions")
    def sessions(p: Principal = Depends(admin), conn=Depends(wconn)):
        with tx(conn, p):
            rows = S.list_sessions(conn)
        return [session_json(s) for s in rows]

    @r.get("/ingest/sessions/{sid}")
    def session_detail(sid: str, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(sid)
        with tx(conn, p):
            s = load_session(conn, p, u)
            props = S.list_proposals(conn, u)
        return session_json(s, True, props)

    # ------------------------------------------------------------ proposals
    def run_validation(s: dict, profile: dict, prop, slug: str, ns: str) -> dict:
        with S.open_session_source(s, named(), root=ingest_root) as src:
            return _jsonable(_report_dict(vfn(profile, prop, src, namespace=ns, slug=slug)))

    def _jsonable(x):
        return json.loads(json.dumps(x, default=str))

    def build(prop_dict: dict, profile: dict, slug: str, ns: str):
        from engine.ingest.proposal import Proposal, ProposalError, to_yaml
        try:
            prop = Proposal.model_validate(prop_dict).model_copy(update={"namespace": ns})
            yaml_text, mapping = to_yaml(prop, profile, slug=slug)
        except (ProposalError, ValueError) as e:
            raise ApiErr(422, "invalid_proposal", clean_text(str(e), 300) or "the proposal is invalid") from None
        return prop, yaml_text, _jsonable(mapping), _jsonable(prop.model_dump(by_alias=True, mode="json"))

    def store_proposal(conn, p, s, kind, prop_json, yaml_text, mapping, validation, *, parent_id=None, model=None,
                       cost=0.0) -> dict:
        with tx(conn, p):
            return S.insert_proposal(conn, s["id"], p.user, kind, prop_json, yaml_text, mapping, validation,
                                     parent_id=parent_id, model=model, cost_usd=cost)

    def new_model(p):
        factory = model_factory
        try:
            if factory is None:
                from engine.api.agent_routes import default_model_factory as factory
            from engine.api.agent_routes import _SafeModel
            return _SafeModel(factory(p))
        except Exception as e:  # noqa: BLE001
            log.warning("ingest model unavailable: %s", type(e).__name__)
            raise ApiErr(503, "agent_unavailable", "the model is not configured") from None

    def start_job(kind, p, work):
        try:
            return registry.start(kind, p.user, work)
        except J.JobLimit as e:
            raise ApiErr(429, "rate_limited", e.message, retry_after=e.retry_after) from None

    def budget(conn, p) -> float:
        lim = J.limits()
        with tx(conn, p):
            used = S.daily_cost(conn, p.user)
        if used >= lim["daily_usd"]:
            raise ApiErr(429, "rate_limited", "daily generation budget used up", retry_after=3600)
        return max(0.0, min(1.0, lim["daily_usd"] - used))

    def run_generate(job, p, s, kind: str, model, parent: dict | None, max_cost: float) -> None:
        from agent.ontogen import GenCaps, generate  # noqa: PLC0415
        from engine.ingest.proposal import to_yaml
        slug, ns = session_ns(s)
        profile = s["profile"]
        c = conn_factory()
        try:
            def vf(prof, prop):
                return run_validation(s, prof, prop, slug, ns)
            if parent is not None:
                model = _Seeded(model, parent)
            job.push("started", {"mode": kind})
            res = generate(profile, model=model, validate_fn=vf, caps=GenCaps(max_cost_usd=max_cost), namespace=ns,
                           cancel=job.cancel, on_event=lambda ev: job.push(ev["kind"], ev["payload"]),
                           to_yaml_fn=lambda pr, prf: to_yaml(pr, prf, slug=slug))
            job.cost_usd = res.cost_usd
            if res.proposal is None or res.yaml is None:
                job.finish("cancelled" if res.status == "cancelled" else "failed",
                           error={"code": "no_proposal", "detail": "the model did not produce a usable proposal",
                                  "status": res.status})
                return
            prop_json = _jsonable(res.proposal.model_dump(by_alias=True, mode="json"))
            row = store_proposal(c, p, s, kind, prop_json, res.yaml, _jsonable(res.mapping),
                                 _jsonable(res.validation), parent_id=parent["id"] if parent else None,
                                 model=res.model or None, cost=res.cost_usd)
            job.proposal_id = str(row["id"])
            job.finish("done", result={"proposal_id": job.proposal_id, "ok": res.ok, "status": res.status,
                                       "repair_rounds": res.repair_rounds, "cost_usd": res.cost_usd})
        finally:
            c.close()

    class _Seeded:
        """Model wrapper for a repair run: the first user message also carries the previous proposal and its report."""

        def __init__(self, inner, parent: dict):
            self.inner, self.name = inner, getattr(inner, "name", "")
            from agent.ontogen.generate import feedback_text
            view = parent.get("validation") or {"ok": False, "checks": []}
            self.extra = (feedback_text(view, 1, 3, "Start from the previous proposal below and fix it.") +
                          "\n\nPrevious proposal (JSON, untrusted names):\n" +
                          json.dumps(parent["proposal"], default=str)[:20000])
            self.done = False

        def complete(self, *, system, messages, tools, **kw):
            if not self.done and messages and isinstance(messages[0].get("content"), str):
                messages = [dict(messages[0], content=messages[0]["content"] + "\n\n" + self.extra), *messages[1:]]
            self.done = True
            return self.inner.complete(system=system, messages=messages, tools=tools, **kw)

    @r.post("/ingest/sessions/{sid}/proposals", status_code=202)
    def create_proposal(sid: str, body: ProposalBody, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(sid)
        with tx(conn, p):
            s = load_session(conn, p, u)
        slug, ns = session_ns(s)
        if body.mode == "baseline":
            prop, yaml_text, mapping, pj = build(baseline_proposal(s["profile"]), s["profile"], slug, ns)
            rep = run_validation(s, s["profile"], prop, slug, ns)
            row = store_proposal(conn, p, s, "baseline", pj, yaml_text, mapping, rep)
            job = registry.completed("baseline", p.user, result={"proposal_id": str(row["id"]), "ok": rep.get("ok")},
                                     proposal_id=str(row["id"]))
            return {"job_id": job.id, "proposal_id": str(row["id"])}
        model = new_model(p)
        max_cost = budget(conn, p)
        job = start_job("model", p, lambda j: run_generate(j, p, s, "model", model, None, max_cost))
        return {"job_id": job.id}

    @r.post("/ingest/proposals/{pid}/repair", status_code=202)
    def repair(pid: str, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(pid)
        with tx(conn, p):
            parent = S.get_proposal(conn, u)
            if parent is None:
                raise ApiErr(404, "not_found", "proposal not found")
            s = S.get_session(conn, parent["session_id"])
            depth = S.repair_depth(conn, u)
        if depth >= 5:
            raise ApiErr(409, "repair_limit", "the repair limit for this proposal chain is reached")
        model = new_model(p)
        max_cost = budget(conn, p)
        job = start_job("repair", p, lambda j: run_generate(j, p, s, "repair", model, parent, max_cost))
        return {"job_id": job.id}

    @r.patch("/ingest/proposals/{pid}/items", status_code=201)
    def edit_items(pid: str, body: ItemsBody, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(pid)
        with tx(conn, p):
            parent = S.get_proposal(conn, u)
            if parent is None:
                raise ApiErr(404, "not_found", "proposal not found")
            s = S.get_session(conn, parent["session_id"])
        items = [i.model_dump() for i in body.items]
        new = apply_items(parent["proposal"], items)
        check_edit_policy(parent["proposal"], new)
        slug, ns = session_ns(s)
        prop, yaml_text, mapping, pj = build(new, s["profile"], slug, ns)
        rep = run_validation(s, s["profile"], prop, slug, ns)
        rep["edit_items"] = items
        row = store_proposal(conn, p, s, "edit", pj, yaml_text, mapping, rep, parent_id=parent["id"])
        return proposal_json(row, s, include_diff=True)

    def diff_for(s: dict, row: dict) -> dict:
        from engine.ingest.proposal import proposal_diff
        slug = W.session_slug(s["source_name"], s["id"])
        old = None
        try:
            old = (W.dir_for(slug, workspaces_root) / "ontology.yaml").read_text()
        except OSError:
            pass
        try:
            return proposal_diff(old, row["yaml"])
        except Exception as e:  # noqa: BLE001
            log.warning("diff failed: %s", type(e).__name__)
            return {"error": "diff_unavailable", "sensitive": ["diff unavailable"], "breaking": []}

    def proposal_json(row: dict, s: dict, *, include_diff: bool) -> dict:
        out = {"id": str(row["id"]), "session_id": str(row["session_id"]), "seq": row["seq"],
               "parent_id": str(row["parent_id"]) if row["parent_id"] else None, "kind": row["kind"],
               "model": row["model"], "cost_usd": float(row["cost_usd"]), "created_by": row["created_by"],
               "created_at": row["created_at"], "proposal": row["proposal"], "yaml": row["yaml"],
               "mapping": row["mapping"], "validation": row["validation"]}
        if include_diff:
            out["diff"] = diff_for(s, row)
        return out

    @r.get("/ingest/proposals/{pid}")
    def proposal_detail(pid: str, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(pid)
        with tx(conn, p):
            row = S.get_proposal(conn, u)
            if row is None:
                raise ApiErr(404, "not_found", "proposal not found")
            s = S.get_session(conn, row["session_id"])
            decisions = S.decisions_for(conn, u)
        out = proposal_json(row, s, include_diff=True)
        out["decisions"] = [{"id": str(d["id"]), "decision": d["decision"], "approver": d["approver"],
                             "comment": d["comment"], "at": d["at"]} for d in decisions]
        return out

    @r.post("/ingest/proposals/{pid}/approve", status_code=201)
    def approve(pid: str, body: ApproveBody, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(pid)
        with tx(conn, p):
            row = S.get_proposal(conn, u)
            if row is None:
                raise ApiErr(404, "not_found", "proposal not found")
            s = S.get_session(conn, row["session_id"])
            existing = S.decisions_for(conn, u)
        if any(d["decision"] == "approved" for d in existing):
            raise ApiErr(409, "already_decided", "this proposal is already approved")
        if not (row["validation"] or {}).get("ok"):
            raise ApiErr(409, "validation_failed", "only a proposal that passes validation can be approved")
        if auth_on() and p.user == s["created_by"]:
            raise ApiErr(403, "self_approval", "the approver must not be the session creator")
        sensitive = diff_for(s, row).get("sensitive") or []
        if sensitive and not body.comment.strip():
            raise ApiErr(422, "comment_required", "this import defines roles and permissions; approval needs a comment",
                         sensitive=len(sensitive))
        with tx(conn, p):
            d = S.insert_decision(conn, u, "approved", p.user, p.role, body.comment.strip() or None,
                                  [i.model_dump() for i in body.items])
        return {"decision_id": str(d["id"]), "proposal_id": str(u), "decision": "approved", "approver": d["approver"],
                "slug": W.session_slug(s["source_name"], s["id"])}

    @r.post("/ingest/proposals/{pid}/reject", status_code=201)
    def reject(pid: str, body: RejectBody, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(pid)
        with tx(conn, p):
            if S.get_proposal(conn, u) is None:
                raise ApiErr(404, "not_found", "proposal not found")
            d = S.insert_decision(conn, u, "rejected", p.user, p.role, body.comment.strip() or None, [])
        return {"decision_id": str(d["id"]), "proposal_id": str(u), "decision": "rejected"}

    # ------------------------------------------------------------ import
    @r.post("/ingest/decisions/{did}/import", status_code=202)
    def start_import(did: str, p: Principal = Depends(admin), conn=Depends(wconn)):
        u = parse_id(did)
        with tx(conn, p):
            d = S.get_decision(conn, u)
        if d is None:
            raise ApiErr(404, "not_found", "decision not found")
        if d["decision"] != "approved":
            raise ApiErr(409, "not_approved", "the decision is not an approval")

        def work(job):
            job.push("started", {"decision_id": str(u)})
            res = run_import(conn_factory, u, p, validate_fn=validate_fn, materialize_mod=materialize_mod,
                             source_registry=named(), workspaces_root=workspaces_root, ingest_root=ingest_root,
                             on_event=job.push, cancel=job.cancel)
            job.result = res.to_dict()
        job = start_job("import", p, work)
        return {"job_id": job.id}

    @r.get("/workspaces")
    def workspaces(p: Principal = Depends(admin)):
        return W.list_workspaces(workspaces_root)

    # ------------------------------------------------------------ jobs
    def get_job(job_id: str):
        j = registry.get(job_id)
        if j is None:
            raise ApiErr(404, "not_found", "job not found")
        return j

    @r.get("/ingest/jobs/{job_id}")
    def job_status(job_id: str, after: int = Query(0, ge=0), p: Principal = Depends(admin)):
        return get_job(job_id).to_dict(after)

    @r.post("/ingest/jobs/{job_id}/cancel")
    def job_cancel(job_id: str, p: Principal = Depends(admin)):
        j = get_job(job_id)
        if not j.finished:
            j.cancel.set()
        return {"job_id": j.id, "status": "cancelling" if not j.finished else j.status}

    @r.get("/ingest/jobs/{job_id}/stream")
    def job_stream(job_id: str, after: int = Query(0, ge=0), p: Principal = Depends(admin)):
        j = get_job(job_id)
        if not registry.open_stream(j):
            raise ApiErr(429, "rate_limited", "too many open streams", retry_after=2)

        def frame(ev):
            return f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {json.dumps(ev, default=str)}\n\n"

        def gen():
            last = after
            try:
                while True:
                    with j.cond:
                        if not any(e["seq"] > last for e in j.events) and not j.finished:
                            j.cond.wait(timeout=KEEPALIVE_S)
                        batch = [e for e in j.events if e["seq"] > last]
                        done = j.finished
                    if not batch:
                        if done:
                            return
                        yield ": keep-alive\n\n"
                        continue
                    for ev in batch:
                        yield frame(ev)
                        last = ev["seq"]
                        if ev["kind"] == "finished":
                            return
            finally:
                registry.close_stream(j)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return r
