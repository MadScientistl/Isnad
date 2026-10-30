"""Validation loop for an ontology proposal (Phase 6): ``validate(profile, proposal, source, ...) -> ValidationReport``.

Everything runs in a scratch schema ``gen_<8 hex>`` (name generated here, never from input) on its own connection and
is dropped in a ``finally`` on a fresh connection; ``sweep_stale_scratch`` removes leftovers by age (the creation time
is stored in ``COMMENT ON SCHEMA``). The function never raises for a bad proposal or bad data: each failure is a failed
check with a sanitised message (<= 3 examples, values cut to 80 characters, control characters removed).

Checks (thresholds are env-overridable, ``ISNAD_INGEST_THRESH_*``):
  schema_ok, ddl_ok, [source_ok, load_ok], key_violations (== 0), required_null_rate (<= 0.0),
  type_coercion_fail_rate (<= 0.02, worst property), enum_violation_rate (<= 0.02, worst property),
  link_resolution:<link> (>= 0.95 each), shacl_conforms, reasoner_consistent (completes, no owl:Nothing,
  inferred <= 50x asserted).

Sampling: types with more than ``caps.max_sample`` rows load a seeded sample of that many rows; rows referenced by
loaded rows through foreign keys / join tables are added (closure, bounded), so link resolution is measured on a
foreign-key-closed subset. Smaller tables load fully. ``stats.sampled`` says which.

A key repeated in the source (a denormalised entity such as Carrier inside shipments) is fine when the repeated rows
agree (case-insensitive, trimmed); only conflicting rows are key violations. ``stats.rows_merged`` counts merged rows.
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

from . import materialize as M
from .names import clean_text
from .profile.caps import Caps

SCRATCH_RE = re.compile(r"gen_[0-9a-f]{8}")
COMMENT_PREFIX = "isnad-scratch "
VALID_FROM = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def thresholds() -> dict:
    return {"type_coercion_fail_rate": _envf("ISNAD_INGEST_THRESH_COERCION", 0.02),
            "enum_violation_rate": _envf("ISNAD_INGEST_THRESH_ENUM", 0.02),
            "link_resolution": _envf("ISNAD_INGEST_THRESH_LINK_RESOLUTION", 0.95),
            "required_null_rate": _envf("ISNAD_INGEST_THRESH_REQUIRED_NULL", 0.0),
            "key_violations": _envf("ISNAD_INGEST_THRESH_KEY_VIOLATIONS", 0),
            "inferred_ratio": _envf("ISNAD_INGEST_THRESH_INFERRED_RATIO", 50)}


@dataclass
class ValidationReport:
    ok: bool
    checks: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    seconds: float = 0.0
    validator: str = "real"

    def to_dict(self) -> dict:
        return json.loads(json.dumps({"ok": self.ok, "checks": self.checks, "stats": self.stats,
                                      "seconds": round(self.seconds, 3), "validator": self.validator}, default=str))

    def check(self, name: str) -> dict | None:
        return next((c for c in self.checks if c["name"] == name), None)


class _Budget(Exception):
    pass


def _s(x: object, n: int = 80) -> str:
    return clean_text(x, n)


# ---------------------------------------------------------------------------------------------- scratch schema
def scratch_schemas(conn) -> list[str]:
    rows = conn.execute("SELECT nspname FROM pg_namespace WHERE nspname ~ '^gen_[0-9a-f]{8}$' ORDER BY 1").fetchall()
    conn.rollback()
    return [r[0] for r in rows]


def _drop(name: str, dsn: str | None = None) -> str | None:
    """Drop a scratch schema on a fresh connection. Returns an error text or None."""
    if not SCRATCH_RE.fullmatch(name):
        return "refused: not a scratch schema name"
    from engine.db.connection import connect
    try:
        with connect(dsn) as c:
            c.execute("SET lock_timeout = '20s'")
            c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
            c.commit()
    except Exception as e:  # noqa: BLE001
        return _s(f"{type(e).__name__}: {e}", 160)
    return None


def sweep_stale_scratch(conn, max_age_s: float = 3600) -> list[str]:
    """Drop ``gen_*`` schemas whose creation stamp is older than ``max_age_s``. Returns the dropped names.
    Schemas without a stamp are never touched."""
    rows = conn.execute("SELECT nspname, obj_description(oid, 'pg_namespace') FROM pg_namespace "
                        "WHERE nspname ~ '^gen_[0-9a-f]{8}$'").fetchall()
    conn.rollback()
    now = time.time()
    dropped = []
    for name, comment in rows:
        if not comment or not comment.startswith(COMMENT_PREFIX):
            continue
        try:
            made = float(comment[len(COMMENT_PREFIX):])
        except ValueError:
            continue
        if now - made > max_age_s:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
            conn.commit()
            dropped.append(name)
    return dropped


def _create_scratch(dsn: str | None):
    from engine.db.connection import connect
    name = "gen_" + secrets.token_hex(4)
    conn = connect(dsn)
    try:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            sql.Identifier(name), sql.Literal(f"{COMMENT_PREFIX}{time.time():.0f}")))
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return name, conn


# ---------------------------------------------------------------------------------------------- sampling
def _count_rows(table) -> int:
    n = table.row_count()
    if n is None:
        n = sum(1 for _ in table.rows())
    return n


def _guard(it, deadline: float):
    for i, x in enumerate(it):
        if i % 500 == 0 and time.monotonic() > deadline:
            raise _Budget("time budget exceeded")
        yield x


def _plan_selection(source, mapping: dict, cap: int, seed: int, deadline: float):
    """-> (rows, extra_ids, sampled): per type a row-index set (None = load everything) and extra ids to add."""
    types, links = mapping["types"], mapping["links"]
    rows: dict[str, set | None] = {}
    join_rows: dict[str, set | None] = {}
    for t, spec in types.items():
        n = _count_rows(M._table(source, spec["table"]))
        rows[t] = None if n <= cap else set(random.Random(f"{seed}:{t}").sample(range(n), cap))
    for ln, spec in links.items():
        if spec.get("kind") == "join_table":
            n = _count_rows(M._table(source, spec["table"]))
            join_rows[ln] = None if n <= cap else set(random.Random(f"{seed}:{ln}").sample(range(n), cap))
    sampled = any(v is not None for v in rows.values()) or any(v is not None for v in join_rows.values())
    extra: dict[str, set] = {t: set() for t in types}
    if not sampled:
        return rows, {t: None for t in types}, False
    base: dict[str, set | None] = {}
    for t, r in rows.items():
        if r is None:
            base[t] = None
        else:
            base[t] = {u for u, _ in _guard(M.iter_objects(source, mapping, t, rows=r, stats=M.new_stats(track_ids=False)),
                                            deadline)}
    limit = 10 * cap
    for _ in range(4):
        changed = False
        for ln, spec in links.items():
            ft, tt = spec["from_type"], spec["to_type"]
            if rows[ft] is None and rows[tt] is None and (spec["kind"] != "join_table" or join_rows[ln] is None):
                continue
            st = M.new_stats(track_ids=False)
            if spec["kind"] == "fk":
                if rows[ft] is None:
                    src_ids = None
                else:
                    src_ids = set(base[ft]) | extra[ft]
                it = M.iter_links(source, mapping, ln, stats=st, src_ids=src_ids)
            else:
                it = M.iter_links(source, mapping, ln, stats=st, rows=join_rows[ln])
            for s, d in _guard(it, deadline):
                for t, u in ((tt, d), (ft, s)) if spec["kind"] == "join_table" else ((tt, d),):
                    if rows[t] is None:
                        continue
                    if u not in extra[t] and u not in base[t] and len(extra[t]) < limit:
                        extra[t].add(u)
                        changed = True
        if not changed:
            break
    return rows, extra, True


# ---------------------------------------------------------------------------------------------- the report
class _Rep:
    def __init__(self):
        self.checks: list = []

    def add(self, name, ok, value=None, threshold=None, detail="", examples=None):
        self.checks.append({"name": name, "ok": bool(ok), "value": value, "threshold": threshold,
                            "detail": _s(detail, 200), "examples": [_s(e) for e in (examples or [])][:3]})

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks)


def _pyd_msg(e) -> str:
    try:
        errs = e.errors()
        return "; ".join(f"{'.'.join(str(x) for x in er.get('loc', ()))[:60]}: {er.get('msg', '')}" for er in errs[:3])
    except Exception:  # noqa: BLE001
        return type(e).__name__


def validate(profile, proposal, source, *, caps=None, namespace=None, slug=None, seed: int = 1,
             dsn: str | None = None) -> ValidationReport:
    t0 = time.monotonic()
    budget = _envf("ISNAD_INGEST_VALIDATE_BUDGET_S", 120)
    deadline = t0 + budget
    rep = _Rep()
    stats: dict = {"rows_loaded": {}, "links_loaded": {}}
    try:
        _run(profile, proposal, source, caps, namespace, slug, seed, dsn, rep, stats, deadline)
    except _Budget:
        rep.add("time_budget", False, round(time.monotonic() - t0, 1), budget, "validation exceeded its time budget")
    except Exception as e:  # noqa: BLE001
        rep.add("internal_error", False, None, None, f"{type(e).__name__}: {e}")
    return ValidationReport(rep.ok, rep.checks, stats, time.monotonic() - t0)


def _run(profile, proposal, source, caps, namespace, slug, seed, dsn, rep: _Rep, stats: dict, deadline):
    th = thresholds()
    caps = Caps.coerce(caps)
    from .proposal import Proposal, ProposalError, to_yaml
    # ---- schema_ok -----------------------------------------------------------------------------
    try:
        prop = proposal if isinstance(proposal, Proposal) else Proposal.model_validate(proposal)
    except Exception as e:  # noqa: BLE001 - pydantic ValidationError and anything odd in the input
        rep.add("schema_ok", False, 0, 1, f"proposal is invalid: {_pyd_msg(e)}")
        return
    try:
        yaml_text, mapping = to_yaml(prop, profile, slug=slug)
        if namespace:
            import yaml as _y
            d = _y.safe_load(yaml_text)
            d["namespace"] = namespace
            yaml_text = _y.safe_dump(d, sort_keys=False, default_flow_style=False, allow_unicode=False, width=100)
        from engine.compiler import CompiledOntology
        comp = CompiledOntology.from_text(yaml_text)
        mapping = M.enrich_mapping(mapping, yaml_text)
        ddl = comp.ddl()
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()
        rep.add("schema_ok", False, 0, 1, "proposal does not resolve: " + (msg[0] if msg else type(e).__name__),
                [x.strip() for x in msg[1:4]])
        return
    rep.add("schema_ok", True, 1, 1, "proposal resolves against the profile and compiles")
    # RDF predicates are global: a property and a link (or inverse) sharing a name would collide in the graph.
    m = comp.model
    props = {p.name for o in m.object_types.values() for p in o.properties}
    linknames = {l.name for l in m.link_types.values()} | {l.inverse for l in m.link_types.values() if l.inverse}
    clash = sorted(props & linknames)
    rep.add("name_collisions", not clash, len(clash), 0,
            "a property and a link share a name (they map to the same RDF predicate); rename one" if clash
            else "property and link names are distinct", clash)
    if time.monotonic() > deadline:
        raise _Budget()

    name = conn = None
    try:
        try:
            name, conn = _create_scratch(dsn)
            _sweep_quietly(conn)
            conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
            conn.execute(sql.SQL("SET statement_timeout = {}").format(
                sql.Literal(f"{int(_envf('ISNAD_INGEST_STATEMENT_TIMEOUT_MS', 60000))}ms")))
            conn.commit()
            from engine.db.setup import apply_schema
            apply_schema(conn, ddl)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            rep.add("ddl_ok", False, 0, 1, f"DDL failed: {type(e).__name__}: {e}")
            return
        rep.add("ddl_ok", True, 1, 1, "generated schema applies")
        if time.monotonic() > deadline:
            raise _Budget()
        _load_and_check(comp, mapping, source, caps, seed, conn, rep, stats, th, deadline)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if name is not None:
            err = _drop(name, dsn)
            stats["scratch_cleanup_error"] = err
            stats["scratch_dropped"] = err is None


def _sweep_quietly(conn):
    try:
        sweep_stale_scratch(conn, _envf("ISNAD_INGEST_SCRATCH_MAX_AGE_S", 3600))
    except Exception:  # noqa: BLE001
        conn.rollback()


def _load_and_check(comp, mapping, source, caps, seed, conn, rep: _Rep, stats: dict, th: dict, deadline):
    from engine.db import bitemporal as bt
    types = sorted(mapping["types"])
    # ---- sample + load ---------------------------------------------------------------------------
    ms = M.new_stats()
    try:
        rows, extra, sampled = _plan_selection(source, mapping, caps.max_sample, seed, deadline)
        for t in types:
            gen = _guard(M.iter_objects(source, mapping, t, stats=ms, rows=rows[t],
                                        ids=(extra[t] if rows[t] is not None else None)), deadline)
            bt.bulk_load_objects(conn, t, gen, VALID_FROM, "validation", outbox=False)
        for ln in sorted(mapping["links"]):
            spec = mapping["links"][ln]
            jr = None
            if spec["kind"] == "join_table":
                n = _count_rows(M._table(source, spec["table"]))
                jr = None if n <= caps.max_sample else set(random.Random(f"{seed}:{ln}").sample(range(n), caps.max_sample))
            gen = _guard(M.iter_links(source, mapping, ln, stats=ms, rows=jr, only_loaded_src=True), deadline)
            bt.bulk_load_links(conn, ln, gen, VALID_FROM, "validation", outbox=False)
        conn.commit()
    except M.MappingError as e:
        conn.rollback()
        rep.add("source_ok", False, 0, 1, f"mapping does not fit the source: {e}")
        return
    except _Budget:
        raise
    except psycopg.Error as e:
        conn.rollback()
        rep.add("load_ok", False, 0, 1, f"database refused the data: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        return
    sd = ms.to_dict()
    stats.update({"rows_loaded": {t: ms.types[t].rows_loaded for t in types},
                  "links_loaded": {l: ms.links[l].links_loaded for l in sorted(mapping["links"]) if l in ms.links},
                  "rows_read": {t: ms.types[t].rows_read for t in types},
                  "rows_merged": {t: ms.types[t].rows_merged for t in types if ms.types[t].rows_merged},
                  "dropped_links": {l: v.dropped_links for l, v in ms.links.items() if v.dropped_links},
                  "sampled": sampled, "sample_cap": caps.max_sample})
    # ---- data checks -----------------------------------------------------------------------------
    key_ex, key_n, merged = [], 0, 0
    for t in types:
        s = ms.types[t]
        n = s.duplicate_keys + s.null_keys
        key_n += n
        merged += s.rows_merged
        for k in ("duplicate_keys", "null_keys"):
            key_ex += [f"{t}: {e}" for e in s.examples.get(k, [])]
    rep.add("key_violations", key_n <= th["key_violations"], key_n, th["key_violations"],
            "rows with an empty key or conflicting rows for the same key"
            + (f"; {merged} identical repeated rows merged" if merged else ""), key_ex)

    def worst(getter):
        best = (0.0, "", [])
        for t in types:
            for pn, p in ms.types[t].props.items():
                num, den, ex = getter(ms.types[t], p)
                r = num / den if den else 0.0
                if r > best[0]:
                    best = (r, f"{t}.{pn}", [f"{t}.{pn}: {e}" for e in ex])
        return best

    r, where, ex = worst(lambda ts, p: (p.null_required, ts.rows_evaluated, [f"{p.null_required} rows dropped"]
                                         if p.null_required else []))
    rep.add("required_null_rate", r <= th["required_null_rate"], round(r, 6), th["required_null_rate"],
            f"worst {where}" if where else "no nulls in required properties", ex)
    r, where, ex = worst(lambda ts, p: (p.coercion_failures, p.nonnull, p.coercion_examples))
    rep.add("type_coercion_fail_rate", r <= th["type_coercion_fail_rate"], round(r, 6), th["type_coercion_fail_rate"],
            f"worst {where}" if where else "all values coerce", ex)
    r, where, ex = worst(lambda ts, p: (p.enum_violations, p.nonnull, p.enum_examples))
    rep.add("enum_violation_rate", r <= th["enum_violation_rate"], round(r, 6), th["enum_violation_rate"],
            f"worst {where}" if where else "all values within their enums", ex)
    if mapping["links"]:
        for ln in sorted(mapping["links"]):
            l = ms.links.get(ln)
            res = l.resolution if l else 1.0
            rep.add(f"link_resolution:{ln}", res >= th["link_resolution"], round(res, 6), th["link_resolution"],
                    f"{l.rows_read - l.unresolved_links if l else 0} of {l.rows_read if l else 0} references resolve",
                    l.examples if l else [])
    else:
        rep.add("link_resolution", True, 1.0, th["link_resolution"], "no links")
    if time.monotonic() > deadline:
        raise _Budget()
    _semantic_checks(comp, mapping, ms, conn, rep, stats, th, deadline)


# ---------------------------------------------------------------------------------------------- SHACL + reasoner
def _sync_cfg(comp, tmp: Path):
    from engine.sync import SyncConfig
    try:
        cfg = SyncConfig.from_ontology(comp)
        if cfg.ontology_turtle and cfg.shapes_turtle:
            return cfg, None
    except (AttributeError, TypeError):
        pass
    m = comp.model
    (tmp / "ontology.ttl").write_text(comp.owl_turtle())
    (tmp / "shapes.ttl").write_text(comp.shacl_turtle())
    cfg = SyncConfig(namespace=m.namespace, object_types=tuple(m.object_types), link_types=tuple(m.link_types),
                     ontology_ttl=tmp / "ontology.ttl")
    return cfg, tmp / "shapes.ttl"


def _semantic_checks(comp, mapping, ms, conn, rep: _Rep, stats: dict, th: dict, deadline):
    from engine.reason import materialize, materialize_owlrl, validate_changed
    from engine.reason.shacl import pg_props_loader
    from engine.sync import open_store, rebuild
    from engine.sync import iri
    ns = comp.model.namespace
    with tempfile.TemporaryDirectory(prefix="isnad_val_") as td:
        try:
            cfg, shapes_path = _sync_cfg(comp, Path(td))
            store = open_store(None, cfg)
            rebuild(conn, store, cfg)
        except Exception as e:  # noqa: BLE001
            rep.add("shacl_conforms", False, None, None, f"graph build failed: {type(e).__name__}: {e}")
            rep.add("reasoner_consistent", False, None, None, "skipped (graph build failed)")
            return
        if time.monotonic() > deadline:
            raise _Budget()
        # SHACL on a bounded, deterministic subset of the loaded objects
        cap = int(_envf("ISNAD_INGEST_SHACL_MAX", 2000))
        per = max(1, cap // max(1, len(ms.types)))
        focus = []
        for t, ts in sorted(ms.types.items()):
            focus += [__import__("uuid").UUID(int=i) for i in sorted(ts.ids or ())[:per]]
        try:
            kw = {"shapes_turtle": cfg.shapes_turtle} if shapes_path is None else {"shapes_path": shapes_path}
            res = validate_changed(store, focus, ns, props_loader=pg_props_loader(conn, list(mapping["types"]), ns), **kw)
            conn.rollback()
            summ: dict = {}
            for v in res.violations:
                path = str(v.get("path", "")).replace(ns, "")
                key = (v.get("component", ""), path)
                summ[key] = summ.get(key, 0) + 1
            ex = [f"{c} on {_s(p, 40)}: {n} nodes" for (c, p), n in sorted(summ.items(), key=lambda kv: -kv[1])[:3]]
            rep.add("shacl_conforms", res.conforms, len(res.violations), 0,
                    f"checked {len(focus)} objects" + (f" of {sum(len(t.ids or ()) for t in ms.types.values())} loaded"
                                                       if len(focus) < sum(len(t.ids or ()) for t in ms.types.values()) else ""),
                    ex)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            rep.add("shacl_conforms", False, None, None, f"SHACL run failed: {type(e).__name__}: {e}")
        if time.monotonic() > deadline:
            raise _Budget()
        # reasoner
        try:
            asserted = sum(1 for _ in store.quads_for_pattern(None, None, None, iri.G_ASSERTED))
            try:
                mr = materialize(store, ns)
            except Exception:  # noqa: BLE001 - `reasonable` missing or failing: documented fallback
                mr = materialize_owlrl(store, ns)
            from pyoxigraph import NamedNode
            nothing = NamedNode("http://www.w3.org/2002/07/owl#Nothing")
            inconsistent = any(True for _ in store.quads_for_pattern(None, None, nothing, iri.G_INFERRED))
            ratio = mr.inferred_triples / max(1, asserted)
            ok = (not inconsistent) and ratio <= th["inferred_ratio"]
            stats["reasoner"] = {"backend": mr.backend, "asserted": asserted, "inferred": mr.inferred_triples}
            rep.add("reasoner_consistent", ok, round(ratio, 3), th["inferred_ratio"],
                    ("inconsistent: owl:Nothing instances derived" if inconsistent else
                     f"{mr.backend}: {mr.inferred_triples} inferred for {asserted} asserted triples"))
        except Exception as e:  # noqa: BLE001
            rep.add("reasoner_consistent", False, None, None, f"reasoner failed: {type(e).__name__}: {e}")
