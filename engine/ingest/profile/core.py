"""profile_source: deterministic, streaming, memory-bounded profiler."""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from itertools import combinations

from ..names import clean_text, safe_prop, safe_type
from ..sources.base import Source, SourceError, SourceTable
from . import textsim
from .caps import Caps
from .infer import ColAcc

BLOCK = 2000
KEY_TYPES = ("int", "string", "timestamp")
LAT_RE = re.compile(r"^(?:(?P<pre>.*?)_)?(?P<role>lat|latitude)(?:_(?P<post>.*))?$")
LON_RE = re.compile(r"^(?:(?P<pre>.*?)_)?(?P<role>lon|lng|long|longitude)(?:_(?P<post>.*))?$")
INJECTION_RE = re.compile(
    r"(ignore (all |any )?(the )?(previous|prior|above)|disregard (the )?(previous|above)|system prompt|"
    r"you are now|new instructions|reveal your|as an ai|<\s*/?\s*(system|assistant|tool)|assistant:|BEGIN (SYSTEM|INSTRUCTION))",
    re.I)
HASH_FIELD = "profile_hash"


class Profile(dict):
    """JSON-serialisable profile (a dict with the keys of the Phase 6 contract plus ``profile_hash``)."""

    @property
    def profile_hash(self) -> str:
        return self[HASH_FIELD]

    @staticmethod
    def compute_hash(d: dict) -> str:
        body = {k: v for k, v in d.items() if k != HASH_FIELD}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self))


@dataclass
class _TState:
    table: SourceTable
    tid: str
    ncols: int
    accs: list[ColAcc]
    plan: list[int] | None
    processed: int
    total: int
    key_cols: list[int] = field(default_factory=list)


def _iter_rows(table: SourceTable, plan: list[int] | None, limit: int | None):
    if plan is None:
        yield from table.rows(limit)
        return
    it = iter(plan)
    nxt = next(it, None)
    for i, row in enumerate(table.rows()):
        if nxt is None:
            return
        if i == nxt:
            yield row
            nxt = next(it, None)


def _run(table: SourceTable, ncols: int, topcap: int, plan: list[int] | None, limit: int | None):
    """Stream rows into accumulators. Returns (accs, processed, overflow)."""
    accs = [ColAcc(c.name, c.declared_type, topcap) for c in table.columns[:ncols]]
    block: list[tuple] = []
    processed = 0
    overflow = False
    take = limit
    for row in _iter_rows(table, plan, None if take is None else take + 1):
        if take is not None and processed >= take:
            overflow = True
            break
        processed += 1
        block.append(row if len(row) == ncols else row[:ncols])
        if len(block) >= BLOCK:
            for acc, col in zip(accs, zip(*block)):
                acc.add_block(col)
            block = []
    if block:
        for acc, col in zip(accs, zip(*block)):
            acc.add_block(col)
    return accs, processed, overflow


def _sample_plan(n: int, k: int, seed: int, tname: str) -> list[int]:
    rng = random.Random(f"{seed}:{tname}")
    return sorted(rng.sample(range(n), k))


def _geo_key(m) -> str:
    return (m.group("pre") or "") + "|" + (m.group("post") or "")


def _geo_pairs(cols: list[dict]) -> None:
    lats: dict[str, dict] = {}
    lons: dict[str, dict] = {}
    for c in cols:
        if c["type"] != "float" or c["geo"] is not None or c["min"] is None:
            continue
        try:
            lo, hi = float(c["min"]), float(c["max"])
        except (TypeError, ValueError):
            continue
        m = LAT_RE.match("_".join(textsim.tokens(c["name"])))
        if m and -90 <= lo and hi <= 90:
            lats.setdefault(_geo_key(m), c)
            continue
        m = LON_RE.match("_".join(textsim.tokens(c["name"])))
        if m and -180 <= lo and hi <= 180:
            lons.setdefault(_geo_key(m), c)
    for key, lat in lats.items():
        lon = lons.get(key)
        lat["geo"] = {"role": "lat", "pair": lon["id"] if lon else None}
        if lon:
            lon["geo"] = {"role": "lon", "pair": lat["id"]}
            lat["notes"].append("lat/lon pair by name and range")
            lon["notes"].append("lat/lon pair by name and range")
        else:
            lat["notes"].append("latitude-like name and range, no matching longitude column")
    for lon in lons.values():
        if lon["geo"] is None:
            lon["geo"] = {"role": "lon", "pair": None}
            lon["notes"].append("longitude-like name and range, no matching latitude column")


def _is_idish_name(name: str) -> bool:
    return textsim.has_idish_suffix(name) or textsim.tokens(name) == ["id"]


def profile_source(source: Source, *, caps: Caps | dict | None = None, seed: int = 1) -> Profile:
    caps = Caps.coerce(caps)
    warnings: list[str] = [clean_text(w, 160) for w in source.warnings]
    truncated = False
    all_tables = source.tables()
    if len(all_tables) > caps.max_tables:
        truncated = True
        warnings.append(f"only the first {caps.max_tables} of {len(all_tables)} tables were profiled")
    stables = all_tables[: caps.max_tables]
    topcap = max(caps.max_sample, 100)

    taken_types: set[str] = set()
    states: list[_TState] = []
    tables: list[dict] = []
    for ti, st in enumerate(stables, 1):
        tid = f"t{ti}"
        ncols = min(len(st.columns), caps.max_columns)
        if len(st.columns) > ncols:
            truncated = True
            warnings.append(f"{tid}: only the first {ncols} of {len(st.columns)} columns were profiled")
        total = st.row_count()
        plan = None
        if total is not None and total > caps.max_rows_profiled:
            plan = _sample_plan(total, caps.max_rows_profiled, seed, st.name)
            accs, processed, _ = _run(st, ncols, topcap, plan, None)
        else:
            accs, processed, overflow = _run(st, ncols, topcap, None, caps.max_rows_profiled)
            if overflow:
                total = sum(1 for _ in st.rows())
                plan = _sample_plan(total, caps.max_rows_profiled, seed, st.name)
                accs, processed, _ = _run(st, ncols, topcap, plan, None)
            elif total is None:
                total = processed
        if getattr(st, "truncated_rows", False):
            warnings.append(f"{tid}: reading stopped at the source row limit")
        if plan is not None:
            truncated = True
            warnings.append(f"{tid}: {total} rows, profiled a seeded sample of {processed}; "
                            "foreign keys into this table may look incomplete")
        if getattr(st, "ragged_rows", 0):
            warnings.append(f"{tid}: {st.ragged_rows} rows had a different number of fields than the header")
        state = _TState(st, tid, ncols, accs, plan, processed, total)
        states.append(state)

        taken_cols: set[str] = set()
        cols: list[dict] = []
        for ci, (info, acc) in enumerate(zip(st.columns[:ncols], accs), 1):
            typ, fmt, geo, notes = acc.resolve()
            distinct, exact = acc.distinct()
            if not exact:
                notes.append("distinct count is an estimate (very many distinct values)")
            if not acc.counts_full:
                notes.append("top values come from the first rows only")
            mn, mx = acc.minmax(typ, fmt)
            uniq = exact and acc.n > 0 and distinct == acc.n
            rows_seen = acc.rows
            enum = None
            if (acc.counts_full and typ in ("string", "int") and 0 < distinct <= 20 and rows_seen
                    and distinct / rows_seen <= 0.2 and not uniq and geo is None):
                enum = [v for v, _c in acc.top(20)]
            col = {
                "id": f"{tid}.c{ci}", "name": clean_text(info.name, 64), "ident": safe_prop(info.name, taken_cols),
                "type": typ, "null_rate": round(acc.nulls / rows_seen, 6) if rows_seen else 0.0,
                "distinct": distinct, "unique": bool(uniq), "min": mn, "max": mx,
                "top": acc.top(caps.top_k), "enum_candidate": enum, "format": fmt, "geo": geo,
                "time": typ == "timestamp", "notes": notes,
            }
            if INJECTION_RE.search(info.name) or any(INJECTION_RE.search(str(v)) for v, _ in col["top"]):
                warnings.append(f"{col['id']}: contains instruction-like text (treated as data only)")
                col["notes"].append("instruction-like text found in this column; treated as data")
            cols.append(col)
        _geo_pairs(cols)
        tables.append({"id": tid, "name": clean_text(st.name, 64), "ident": safe_type(st.name, taken_types),
                       "rows": int(total), "sampled_rows": int(processed), "columns": cols,
                       "key_candidates": [], "is_join_table": False})

    by_name = {t["name"].lower(): t for t in tables}
    colmap = {(t["name"].lower(), c["name"].lower()): c for t in tables for c in t["columns"]}
    colidx = {c["id"]: (states[i], j) for i, t in enumerate(tables) for j, c in enumerate(t["columns"])}
    declared_pk = {k.lower(): [x.lower() for x in v] for k, v in source.declared_keys().items()}

    # ---- keys -------------------------------------------------------------------------------
    for st, t in zip(states, tables):
        singles = [c for c in t["columns"] if c["unique"] and c["null_rate"] == 0.0 and c["type"] in KEY_TYPES
                   and not (c["type"] == "string" and c["geo"])]
        pk = declared_pk.get(t["name"].lower(), [])
        singles.sort(key=lambda c: (0 if [c["name"].lower()] == pk else 1,
                                    0 if _is_idish_name(c["name"]) or c["ident"] == "id" else 1,
                                    int(c["id"].split(".c")[1])))
        t["key_candidates"] = [[c["id"]] for c in singles[:5]]
        if not singles:
            comp = _composite_keys(st, t, pk, caps)
            t["key_candidates"] = comp

    # ---- foreign keys -----------------------------------------------------------------------
    fks: list[dict] = []
    seen_from: set[str] = set()
    for d in source.declared_fks():
        ft, tt = by_name.get(d.from_table.lower()), by_name.get(d.to_table.lower())
        if ft is None or tt is None:
            continue
        if len(d.from_cols) != 1 or len(d.to_cols) != 1:
            warnings.append(f"{ft['id']}: a composite foreign key to {tt['id']} was not profiled")
            continue
        fc = colmap.get((ft["name"].lower(), d.from_cols[0].lower()))
        tc = colmap.get((tt["name"].lower(), d.to_cols[0].lower()))
        if fc is None or tc is None:
            continue
        fks.append({"from": fc["id"], "to": tc["id"], "declared": True,
                    "name_score": round(textsim.name_score(fc["name"], tt["name"], tc["name"], ft is tt), 4),
                    "confidence": 1.0})
        seen_from.add(fc["id"])
    fks.extend(_infer_fks(tables, states, seen_from))
    fks.sort(key=lambda f: (not f["declared"], int(f["from"][1:].split(".")[0]), int(f["from"].split(".c")[1]), f["to"]))
    _fk_stats(fks, colidx, states)
    for i, f in enumerate(fks, 1):
        f["id"] = f"f{i}"
        f["cardinality"] = "one-to-one" if _col(tables, f["from"])["unique"] else "many-to-one"
    fks = [{k: f[k] for k in ("id", "from", "to", "declared", "containment", "name_score", "confidence",
                              "cardinality", "orphans")} for f in fks]

    # ---- join tables + key preference -------------------------------------------------------
    incoming: dict[str, int] = {}
    for f in fks:
        incoming[f["to"]] = incoming.get(f["to"], 0) + 1
    for t in tables:
        t["key_candidates"].sort(key=lambda kc: -sum(incoming.get(c, 0) for c in kc) if len(kc) == 1 else 0)
        # stable: keep declared/idish order among equals (sort is stable)
        fkcols = {f["from"] for f in fks if f["from"].startswith(t["id"] + ".")}
        if len(fkcols) >= 2:
            others = [c for c in t["columns"] if c["id"] not in fkcols]
            others = [c for c in others if not (c["unique"] and _is_idish_name(c["name"]))]
            if len(others) <= 2 and len(fkcols) * 2 >= len(t["columns"]) - 1:
                t["is_join_table"] = True

    src = {"kind": source.kind, "name": clean_text(source.name, 64), "sha256": source.sha256, "bytes": source.bytes}
    prof = Profile(version=1, source=src, caps=caps.to_dict(), truncated=truncated, seed=int(seed),
                   tables=tables, fks=fks, warnings=[clean_text(w, 200) for w in warnings])
    prof[HASH_FIELD] = Profile.compute_hash(prof)
    return prof


def _col(tables: list[dict], cid: str) -> dict:
    t = tables[int(cid[1:].split(".")[0]) - 1]
    return t["columns"][int(cid.split(".c")[1]) - 1]


def _composite_keys(st: _TState, t: dict, pk: list[str], caps: Caps) -> list[list[str]]:
    cands = [c for c in t["columns"] if c["null_rate"] == 0.0 and c["type"] in KEY_TYPES and c["distinct"] >= 2
             and not c["unique"] and t["sampled_rows"] > 1]
    cands.sort(key=lambda c: (0 if _is_idish_name(c["name"]) else 1, -c["distinct"], c["id"]))
    cands = cands[:10]
    n = t["sampled_rows"]
    pairs = [(a, b) for a, b in combinations(cands, 2) if a["distinct"] * b["distinct"] >= n]
    if pk and len(pk) == 2:
        names = {c["name"].lower(): c for c in t["columns"]}
        if pk[0] in names and pk[1] in names and names[pk[0]]["null_rate"] == 0 and names[pk[1]]["null_rate"] == 0:
            pairs.insert(0, (names[pk[0]], names[pk[1]]))
    pairs = pairs[:15]
    if not pairs:
        return []
    idx = {c["id"]: int(c["id"].split(".c")[1]) - 1 for c in t["columns"]}
    seen = [set() for _ in pairs]
    dup = [False] * len(pairs)
    for row in _iter_rows(st.table, st.plan, st.processed if st.plan is None else None):
        for p, (a, b) in enumerate(pairs):
            if dup[p]:
                continue
            key = (row[idx[a["id"]]], row[idx[b["id"]]])
            if key in seen[p]:
                dup[p] = True
            else:
                seen[p].add(key)
        if all(dup):
            break
    out = [[a["id"], b["id"]] for (a, b), d in zip(pairs, dup) if not d]
    return out[:3]


def _infer_fks(tables: list[dict], states: list[_TState], seen_from: set[str]) -> list[dict]:
    keys: list[tuple[dict, dict, set]] = []          # (table, column, value set) of single-column keys
    for st, t in zip(states, tables):
        for kc in t["key_candidates"]:
            if len(kc) == 1:
                c = _col(tables, kc[0])
                vals = st.accs[int(kc[0].split(".c")[1]) - 1].vals
                if vals is not None:
                    keys.append((t, c, vals))
    out: list[dict] = []
    for st, t in zip(states, tables):
        own_keys = {kc[0] for kc in t["key_candidates"] if len(kc) == 1}
        for j, c in enumerate(t["columns"]):
            if c["id"] in seen_from or c["type"] not in ("int", "string") or c["geo"] or c["distinct"] < 2:
                continue
            fvals = st.accs[j].vals
            if not fvals:
                continue
            best = None
            for tt, kc, kvals in keys:
                if kc["id"] == c["id"] or kc["type"] != c["type"]:
                    continue
                same = tt is t
                score = textsim.name_score(c["name"], tt["name"], kc["name"], same)
                if c["id"] in own_keys and score < 0.5:
                    continue
                if same and score < 0.5:
                    continue
                if len(fvals) > len(kvals) / 0.9 + 1:
                    continue
                if score < 0.3 and not (c["type"] == "string" and c["distinct"] >= 10 and kc["distinct"] >= 10):
                    continue
                if c["distinct"] <= 5 and score < 0.5:
                    continue
                cont = len(fvals & kvals) / len(fvals)
                if cont < 0.9:
                    continue
                cont_f = 0.5 + 0.5 * min(1.0, (cont - 0.9) / 0.1)
                conf = 0.45 * cont_f + 0.55 * score
                if score < 0.3 and cont >= 0.98:
                    conf += 0.2
                conf = round(min(conf, 0.99), 4)
                if conf < 0.6:
                    continue
                rank = (conf, score, -int(tt["id"][1:]))
                if best is None or rank > best[0]:
                    best = (rank, kc, round(score, 4), conf)
            if best:
                out.append({"from": c["id"], "to": best[1]["id"], "declared": False, "name_score": best[2],
                            "confidence": best[3]})
    return out


def _fk_stats(fks: list[dict], colidx: dict, states: list[_TState]) -> None:
    """Row-level containment and orphans: one extra pass per referencing table (profiled rows)."""
    by_table: dict[int, list[dict]] = {}
    for f in fks:
        st, _j = colidx[f["from"]]
        by_table.setdefault(states.index(st), []).append(f)
    for si, group in by_table.items():
        st = states[si]
        spec = []
        for f in group:
            _s, j = colidx[f["from"]]
            ts, tj = colidx[f["to"]]
            tvals = ts.accs[tj].vals
            spec.append((f, j, tvals, [0, 0]))
        if all(sp[2] is None for sp in spec):
            for f, *_ in spec:
                f["containment"], f["orphans"] = 1.0, 0
            continue
        from .infer import NULL_TOKENS
        for row in _iter_rows(st.table, st.plan, None if st.plan else st.processed):
            for f, j, tvals, cnt in spec:
                v = row[j]
                if v is None:
                    continue
                v = v.strip()
                if len(v) < 5 and v.lower() in NULL_TOKENS:
                    continue
                cnt[0] += 1
                if tvals is not None and v not in tvals:
                    cnt[1] += 1
        for f, j, tvals, (nn, miss) in spec:
            f["orphans"] = miss
            f["containment"] = round(1 - miss / nn, 6) if nn else 1.0
