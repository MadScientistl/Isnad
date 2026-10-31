"""Audited dataset import: an approved ingest decision becomes a new workspace (schema ``ws_<slug>`` + files).

``run_import(conn_factory, decision_id, principal, ...) -> ImportResult``. Preconditions: the decision is an approval by
an admin (and, when ISNAD_AUTH=1, not by the session creator), the stored upload still hashes to the recorded digest,
the stored yaml+mapping are exactly what `to_yaml` produces for the stored proposal, the yaml compiles and a fresh
validation of that proposal on the stored bytes passes. Then ONE owner transaction: advisory lock, create the schema,
apply governance + compiled DDL, register the first ontology version (source 'import'), COPY objects then links with
one shared action_id, drop-and-count links whose endpoints do not exist (or duplicate pairs), write ONE audit_event
(`import_dataset`, source 'import', role 'owner', actor = approver) into the new schema and one into the host schema
(this is where the decision -> workspace link is persisted: the ingest_* tables are append-only), write
``workspaces/<slug>/`` (renamed into place just before commit) and commit. A failure rolls the schema back and removes
the directory. A second run for the same decision returns ``already_imported`` and changes nothing.

The graph store is NOT built here: the workspace process rebuilds it from Postgres on first use (no graph_outbox rows
are written for the bulk load).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from engine.db import bitemporal as bt
from engine.db.principal import OWNER, scoped
from engine.db.setup import apply_schema

from . import store as S
from . import workspaces as W
from .store import IngestError

log = logging.getLogger("isnad.ingest_import")
IMPORT_NAME = "import_dataset"


@dataclass
class ImportResult:
    slug: str
    schema: str
    action_id: str
    decision_id: str
    proposal_id: str
    ontology_hash: str
    counts: dict                      # {"objects": {Type: n}, "links": {link: n}}
    dropped_links: dict               # {link: {"unresolved": n, "duplicate": n}}
    dropped_objects: dict             # {Type: duplicates dropped}
    rows: int                         # objects loaded
    seconds: float = 0.0
    already_imported: bool = False
    imported_at: str | None = None
    directory: str | None = None
    materialize: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self.__dict__, default=str))


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, default))
        return v if v > 0 else default
    except ValueError:
        return default


def auth_on() -> bool:
    return os.environ.get("ISNAD_AUTH") == "1"


def _report_dict(r) -> dict:
    if isinstance(r, dict):
        return r
    f = getattr(r, "to_dict", None)
    if callable(f):
        return f()
    return {"ok": bool(getattr(r, "ok", False)), "checks": []}


def default_validate(profile, proposal, source, *, namespace=None, slug=None):
    try:
        from engine.ingest import validate as V
    except ImportError:
        raise IngestError(501, "validator_unavailable", "the validator is not installed") from None
    return V.validate(profile, proposal, source, namespace=namespace, slug=slug)


def default_materialize():
    try:
        from engine.ingest import materialize as M
    except ImportError:
        raise IngestError(501, "materializer_unavailable", "the materializer is not installed") from None
    return M


def _load_chain(conn, principal, decision_id):
    """Read decision / proposal / session under the caller's identity (RLS: admin only)."""
    ctx = scoped(conn, principal) if principal is not OWNER else None
    with conn.transaction():
        if ctx is not None:
            ctx.__enter__()
        try:
            d = S.get_decision(conn, decision_id)
            p = S.get_proposal(conn, d["proposal_id"]) if d else None
            s = S.get_session(conn, p["session_id"]) if p else None
        finally:
            if ctx is not None:
                ctx.__exit__(None, None, None)
    return d, p, s


def _host_import_row(conn, decision_id) -> dict | None:
    try:
        with conn.transaction():
            r = conn.execute("SELECT params FROM audit_event WHERE name = %s AND source = 'import' AND "
                             "params->>'decision_id' = %s ORDER BY seq LIMIT 1",
                             (IMPORT_NAME, str(decision_id))).fetchone()
    except Exception:  # noqa: BLE001 - host without audit_event
        return None
    return r[0] if r else None


def _host_import_for_slug(conn, slug: str) -> bool:
    """True when the audit shows a completed import into workspace ``slug`` (by any decision)."""
    try:
        with conn.transaction():
            r = conn.execute("SELECT 1 FROM audit_event WHERE name = %s AND source = 'import' AND "
                             "params->>'workspace' = %s LIMIT 1", (IMPORT_NAME, slug)).fetchone()
    except Exception:  # noqa: BLE001
        return True      # cannot tell: be conservative and keep the directory
    return r is not None


def _result_from_row(params: dict, root) -> ImportResult:
    slug = params["workspace"]
    rep = W.read_report(slug, root)
    if rep:
        known = {k: rep[k] for k in ImportResult.__dataclass_fields__ if k in rep}
        known["already_imported"] = True
        return ImportResult(**known)
    counts = params.get("counts") or {"objects": {}, "links": {}}
    return ImportResult(slug=slug, schema=params.get("schema") or W.schema_for(slug),
                        action_id=str(params.get("action_id") or ""), decision_id=str(params["decision_id"]),
                        proposal_id=str(params["proposal_id"]), ontology_hash=params.get("ontology_hash", ""),
                        counts=counts, dropped_links=params.get("dropped_links") or {},
                        dropped_objects=params.get("dropped_objects") or {},
                        rows=sum((counts.get("objects") or {}).values()), already_imported=True)


def _write_files(tmp: Path, c, yaml_text: str, mapping: dict, report: dict) -> None:
    from engine.compiler import write_outputs
    tmp.mkdir(parents=True)
    (tmp / "ontology.yaml").write_text(yaml_text)
    (tmp / "mapping.json").write_text(json.dumps(mapping, indent=2, sort_keys=True))
    write_outputs(c, tmp / "build")
    (tmp / "import_report.json").write_text(json.dumps(report, indent=2, default=str))


def run_import(conn_factory, decision_id, principal, *, validate_fn=None, materialize_mod=None,
               source_registry=None, workspaces_root=None, ingest_root=None, on_event=None, cancel=None,
               max_rows: int | None = None, timeout_s: int | None = None) -> ImportResult:
    t0 = time.monotonic()
    emit = on_event or (lambda kind, payload=None: None)
    max_rows = max_rows or _int_env("ISNAD_INGEST_MAX_IMPORT_ROWS", 1_000_000)
    timeout_s = timeout_s or _int_env("ISNAD_INGEST_IMPORT_TIMEOUT_S", 900)

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise IngestError(409, "cancelled", "the import was cancelled")

    if principal is not OWNER and getattr(principal, "role", None) != "admin":
        raise IngestError(404, "not_found", "decision not found")
    decision_id = uuid.UUID(str(decision_id))
    host = conn_factory()
    try:
        host_schema = host.execute("SELECT current_schema()").fetchone()[0]
        d, p, s = _load_chain(host, principal, decision_id)
        host.commit()
        if d is None or p is None or s is None:
            raise IngestError(404, "not_found", "decision not found")
        if d["decision"] != "approved":
            raise IngestError(409, "not_approved", "the decision is not an approval")
        if d["approver_role"] != "admin":
            raise IngestError(403, "approver_not_admin", "only an admin approval can trigger an import")
        if auth_on() and d["approver"] == s["created_by"]:
            raise IngestError(403, "self_approval", "the approver must not be the session creator")
        ctx = scoped(host, principal) if principal is not OWNER else None
        with host.transaction():
            if ctx is not None:
                ctx.__enter__()
            try:
                late_reject = S.latest_rejection_after(host, p["id"], d["at"])
            finally:
                if ctx is not None:
                    ctx.__exit__(None, None, None)
        host.commit()
        if late_reject:
            raise IngestError(409, "rejected_after_approval", "the proposal was rejected after it was approved")
        prior = _host_import_row(host, decision_id)
        host.commit()
        if prior is not None:
            return _result_from_row(prior, workspaces_root)

        slug = W.session_slug(s["source_name"], s["id"])
        schema = W.schema_for(slug)
        from .proposal import Proposal, ProposalError, derive_namespace, to_yaml
        ns = derive_namespace(slug)
        try:
            prop = Proposal.model_validate(p["proposal"])
            yaml_text, mapping = to_yaml(prop, s["profile"], slug=slug)
        except (ProposalError, ValueError):
            raise IngestError(409, "proposal_invalid", "the stored proposal does not resolve against its profile") from None
        if yaml_text != p["yaml"] or json.loads(json.dumps(mapping)) != p["mapping"]:
            raise IngestError(409, "proposal_inconsistent", "the stored yaml or mapping differs from the proposal")
        from engine.compiler import CompiledOntology
        try:
            c = CompiledOntology.from_text(yaml_text)
            ddl = c.ddl()
        except Exception:  # noqa: BLE001
            raise IngestError(422, "ontology_invalid", "the ontology does not compile") from None
        check_cancel()

        emit("validating", {"slug": slug})
        validate = validate_fn or default_validate
        mat = materialize_mod or default_materialize()
        enrich = getattr(mat, "enrich_mapping", None)
        mapping = enrich(mapping, yaml_text, namespace=ns) if enrich else dict(mapping, namespace=ns)
        with S.open_session_source(s, source_registry, root=ingest_root) as src:
            rep = _report_dict(validate(s["profile"], prop, src, namespace=ns, slug=slug))
            if not rep.get("ok"):
                failed = [str(ch.get("name"))[:40] for ch in rep.get("checks", []) if not ch.get("ok")]
                raise IngestError(422, "validation_failed", "the proposal no longer validates", failed=failed[:20])
            check_cancel()
            try:
                res = _transaction(conn_factory, host_schema, schema, slug, ns, d, p, s, c, ddl, yaml_text, mapping,
                                   src, mat, workspaces_root, max_rows, timeout_s, emit, check_cancel)
            except IngestError:
                raise
            except Exception as e:  # noqa: BLE001 - rolled back and cleaned up by _transaction
                log.warning("import failed: %s", type(e).__name__)
                raise IngestError(500, "import_failed", "the import failed and was rolled back") from None
        res.seconds = round(time.monotonic() - t0, 3)
        emit("finished", {"slug": slug, "rows": res.rows})
        return res
    finally:
        try:
            host.close()
        except Exception:  # noqa: BLE001
            pass


def _transaction(conn_factory, host_schema, schema, slug, ns, d, p, s, c, ddl, yaml_text, mapping, src, mat,
                 root, max_rows, timeout_s, emit, check_cancel) -> ImportResult:
    final = W.dir_for(slug, root)
    base = final.parent
    base.mkdir(parents=True, exist_ok=True)
    tmpdir = base / f".tmp-{slug}-{uuid.uuid4().hex[:8]}"
    moved = False
    approver = d["approver"]
    action_id = uuid.uuid4()
    w = conn_factory()
    try:
        with w.transaction():
            w.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(f"{timeout_s}s")))
            w.execute("SET LOCAL lock_timeout = '15s'")
            w.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("ingest-import:" + str(d["id"]),))
            prior = _host_import_row(w, d["id"])
            if prior is not None:
                return _result_from_row(prior, root)
            exists = w.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
            if exists:
                raise IngestError(409, "workspace_exists", "a workspace for this session already exists")
            if final.exists():
                # The schema does not exist (checked above), so this directory is an orphan. It may be replaced only
                # when the audit shows no completed import for this workspace (a crashed or hand-made leftover).
                if _host_import_for_slug(w, slug):
                    raise IngestError(409, "workspace_exists", "an orphaned workspace directory exists for a completed import")
                shutil.rmtree(final, ignore_errors=True)
            now = w.execute("SELECT now()").fetchone()[0]
            w.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            w.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
            apply_schema(w, ddl)
            from engine import versioning
            ver = versioning.register_import(w, yaml_text, approver, f"import decision {d['id']}")
            emit("loading", {"schema": schema})

            ids: dict[str, set] = {}
            objects, dropped_objects = {}, {}
            st = mat.new_stats()          # ONE stats object for the whole load (the materializer resolves links by it)
            for tname in sorted(mapping["types"]):
                check_cancel()
                seen: set = set()
                dup = [0]

                def gen(tname=tname, st=st, seen=seen, dup=dup):
                    for id_, props in mat.iter_objects(src, mapping, tname, limit=max_rows + 1, stats=st):
                        if id_ in seen:
                            dup[0] += 1
                            continue
                        if len(seen) >= max_rows:
                            raise IngestError(413, "too_many_rows", "a table exceeds the import row limit")
                        seen.add(id_)
                        if len(seen) % 20000 == 0:
                            check_cancel()
                        yield id_, props
                n = bt.bulk_load_objects(w, tname, gen(), now, approver, action_id=action_id, outbox=False)
                ids[tname], objects[tname] = seen, n
                if dup[0]:
                    dropped_objects[tname] = dup[0]
                emit("loaded", {"type": tname, "rows": n})

            links, dropped = {}, {}
            for lname in sorted(mapping["links"]):
                check_cancel()
                m = mapping["links"][lname]
                src_ids, dst_ids = ids.get(m["from_type"], set()), ids.get(m["to_type"], set())
                pairs: set = set()
                cnt = {"unresolved": 0, "duplicate": 0}

                def lgen(lname=lname, st=st, pairs=pairs, cnt=cnt, src_ids=src_ids, dst_ids=dst_ids):
                    for a, b in mat.iter_links(src, mapping, lname, stats=st):
                        if a not in src_ids or b not in dst_ids:
                            cnt["unresolved"] += 1
                        elif (a, b) in pairs:
                            cnt["duplicate"] += 1
                        else:
                            pairs.add((a, b))
                            yield a, b
                links[lname] = bt.bulk_load_links(w, lname, lgen(), now, approver, action_id=action_id, outbox=False)
                ls = getattr(st, "links", {}).get(lname)     # the materializer drops/merges on its own as well
                if ls is not None:
                    cnt["unresolved"] += int(getattr(ls, "unresolved_links", 0))
                    cnt["duplicate"] += int(getattr(ls, "duplicate_links", 0))
                dropped[lname] = cnt
                emit("loaded", {"link": lname, "rows": links[lname]})

            counts = {"objects": objects, "links": links}
            params = {"counts": counts, "source_sha256": s["source_sha256"], "profile_hash": s["profile_hash"],
                      "ontology_hash": c.hash, "proposal_id": str(p["id"]), "decision_id": str(d["id"]),
                      "dropped_links": dropped, "dropped_objects": dropped_objects}
            ins = ("INSERT INTO {} (action_id, name, actor, source, params, sim_time, role, status) "
                   "VALUES (%s,%s,%s,'import',%s,%s,'owner','executed')")
            w.execute(sql.SQL(ins).format(sql.Identifier("audit_event")),
                      (action_id, IMPORT_NAME, approver, Jsonb(params), now))
            w.execute(sql.SQL(ins).format(sql.Identifier(host_schema, "audit_event")),
                      (action_id, IMPORT_NAME, approver, Jsonb({**params, "workspace": slug, "schema": schema,
                                                                 "action_id": str(action_id),
                                                                 "ontology_version": ver["id"]}), now))
            res = ImportResult(slug=slug, schema=schema, action_id=str(action_id), decision_id=str(d["id"]),
                               proposal_id=str(p["id"]), ontology_hash=c.hash, counts=counts, dropped_links=dropped,
                               dropped_objects=dropped_objects, rows=sum(objects.values()),
                               imported_at=now.astimezone(UTC).isoformat(), directory=str(final),
                               materialize=json.loads(json.dumps(st.to_dict() if hasattr(st, "to_dict") else {}, default=str)))
            check_cancel()
            _write_files(tmpdir, c, yaml_text, mapping, res.to_dict())
            os.replace(tmpdir, final)
            moved = True
        return res
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if moved:
            shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass
