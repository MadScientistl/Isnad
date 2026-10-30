"""Stdlib mini-profiler with the contract's Profile shape (JSON dict). Fallback / test double used when
`engine.ingest.profile` is not importable and in unit tests. It is NOT the production profiler."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

csv.field_size_limit(sys.maxsize)
NULLS = {"", "n/a", "na", "null", "none", "-", "nan"}
TS_FORMATS = [("%Y-%m-%dT%H:%M:%S", "iso8601"), ("%Y-%m-%d %H:%M:%S", "iso8601"), ("%Y-%m-%d", "iso8601"),
              ("%d/%m/%Y", "dd/mm/yyyy"), ("%m-%d-%Y", "mm-dd-yyyy"), ("%d %b %Y", "dd Mon yyyy")]


def parse_ts(v: str, fmt: str | None = None):
    for f, name in TS_FORMATS:
        if fmt and fmt not in (f, name):
            continue
        try:
            return datetime.strptime(v.strip(), f)
        except ValueError:
            pass
    return None


def is_null(v) -> bool:
    return v is None or str(v).strip().lower() in NULLS


def _clean(s, n=80):
    s = "".join(ch for ch in str(s) if ch.isprintable())
    return s if len(s) <= n else s[: n - 1] + "…"


def _kind(vals):
    if not vals:
        return "string", None
    for conv, name in ((int, "int"), (float, "float")):
        try:
            [conv(v) for v in vals]
            return name, None
        except ValueError:
            pass
    counts = Counter()
    for v in vals:
        for f, name in TS_FORMATS:
            if parse_ts(v, f):
                counts[name] += 1
                break
    if sum(counts.values()) >= 0.97 * len(vals):
        return "timestamp", counts.most_common(1)[0][0]
    return "string", None


def read_csv(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        header = next(rd)
        return header, [r for r in rd]


def profile_dir(path, name: str | None = None) -> dict:
    path = Path(path)
    tables, h = [], hashlib.sha256()
    cnum = 0
    for ti, f in enumerate(sorted(path.glob("*.csv")), 1):
        header, rows = read_csv(f)
        h.update(f.read_bytes())
        cols, keys = [], []
        for ci, cname in enumerate(header, 1):
            raw = [r[ci - 1] if ci - 1 < len(r) else "" for r in rows]
            vals = [v for v in raw if not is_null(v)]
            kind, fmt = _kind(vals)
            cnt = Counter(vals)
            distinct = len(cnt)
            lname = cname.strip().lower()
            geo = None
            if kind == "float" and lname in ("lat", "latitude"):
                geo = {"role": "lat", "pair": None}
            elif kind == "float" and lname in ("lon", "lng", "longitude"):
                geo = {"role": "lon", "pair": None}
            col = {"id": f"t{ti}.c{ci}", "name": _clean(cname, 64), "ident": re.sub(r"\W+", "_", lname).strip("_")[:40],
                   "type": kind, "null_rate": round(1 - len(vals) / max(1, len(rows)), 4), "distinct": distinct,
                   "unique": distinct == len(vals) == len(rows), "min": _clean(min(vals)) if vals else None,
                   "max": _clean(max(vals)) if vals else None, "top": [[_clean(k), c] for k, c in cnt.most_common(5)],
                   "enum_candidate": ([_clean(k) for k in sorted(cnt)] if 0 < distinct <= 20 and distinct / max(1, len(rows)) <= 0.2 else None),
                   "format": fmt, "geo": geo, "time": kind == "timestamp", "notes": []}
            if col["unique"] and col["null_rate"] == 0:
                keys.append([col["id"]])
            cols.append(col)
        tables.append({"id": f"t{ti}", "name": _clean(f.stem, 64), "ident": f.stem.title().replace("_", ""), "rows": len(rows),
                       "sampled_rows": len(rows), "columns": cols, "key_candidates": keys, "is_join_table": False})
    prof = {"version": 1, "source": {"kind": "csv", "name": path.name, "sha256": h.hexdigest(), "bytes": None},
            "caps": {}, "truncated": False, "seed": 1, "tables": tables, "fks": [], "warnings": []}
    prof["profile_hash"] = hashlib.sha256(json.dumps(prof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return prof
