"""Stdlib stand-in for `engine.ingest.validate.validate` over a CSV directory (used when the real validator or its
database is unavailable, and in unit tests). It checks the same measurable things the contract names (key violations,
required nulls, coercion, enum violations, link resolution) but does NOT build DDL, run SHACL or the reasoner.
The mock-mode report says which validator ran."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from agent.ontogen.tools import as_dict
from evals.ontogen.lite_profile import is_null, parse_ts, read_csv

THRESH = {"type_coercion_fail_rate": 0.02, "enum_violation_rate": 0.02, "link_resolution": 0.95}


def _tables(profile: dict, data_dir: Path):
    out = {}
    for t in profile["tables"]:
        header, rows = read_csv(data_dir / f"{t['name']}.csv")
        idx = {c["id"]: header.index(c["name"]) if c["name"] in header else None for c in t["columns"]}
        for c in t["columns"]:
            if idx[c["id"]] is None:  # truncated name: match by prefix
                idx[c["id"]] = next((i for i, h in enumerate(header) if h.startswith(c["name"].rstrip("…"))), None)
        out[t["id"]] = (rows, idx)
    return out


def _get(rows_idx, col_id, r):
    rows, idx = rows_idx
    i = idx.get(col_id)
    return None if i is None or i >= len(r) else r[i]


def lite_validate(profile, proposal, *, data_dir) -> dict:
    pd, prop = as_dict(profile), as_dict(proposal)
    tabs = _tables(pd, Path(data_dir))
    checks, stats_rows = [], {}

    def add(name, ok, value, thr, detail="", examples=None):
        checks.append({"name": name, "ok": ok, "value": value, "threshold": thr, "detail": detail,
                       "examples": (examples or [])[:3]})

    key_bad, req_bad, coerce, enum_bad = [], [], [], []
    key_vals: dict[str, set] = {}
    for o in prop["objectTypes"]:
        data = tabs.get(o["source_table"])
        if not data:
            continue
        rows, _ = data
        seen: dict[tuple, tuple] = {}
        nrows = 0
        for r in rows:
            k = tuple(_get(data, c, r) for c in o["key"])
            if any(is_null(x) for x in k):
                key_bad.append(f"{o['name']}: null key")
                continue
            sig = tuple(str(_get(data, p["column"], r)).strip().lower() for p in o["properties"] if p.get("column"))
            if k in seen:
                if seen[k] != sig:
                    key_bad.append(f"{o['name']}: key {str(k[0])[:40]} maps to different values")
                continue
            seen[k] = sig
            nrows += 1
            for p in o["properties"]:
                tr = p.get("transform", "none")
                if p.get("column"):
                    v = _get(data, p["column"], r)
                    if is_null(v):
                        if p.get("required"):
                            req_bad.append(f"{o['name']}.{p['name']}: null in required property")
                        continue
                    if tr in ("int", "float"):
                        try:
                            (int if tr == "int" else float)(v)
                        except ValueError:
                            coerce.append(f"{o['name']}.{p['name']}: {str(v)[:40]!r}")
                    elif tr.startswith("timestamp:"):
                        if parse_ts(v, tr[10:]) is None:
                            coerce.append(f"{o['name']}.{p['name']}: {str(v)[:40]!r}")
                    elif tr.startswith("enum_map:"):
                        m = json.loads(tr[9:])
                        canon = m.get(v, m.get(v.strip()))
                        allowed = p["type"].get("enum", []) if isinstance(p["type"], dict) else []
                        if canon is None or canon not in allowed:
                            enum_bad.append(f"{o['name']}.{p['name']}: {str(v)[:40]!r}")
                    elif isinstance(p["type"], dict) and v not in p["type"].get("enum", []):
                        enum_bad.append(f"{o['name']}.{p['name']}: {str(v)[:40]!r}")
        stats_rows[o["name"]] = nrows
        key_vals[o["name"]] = {k[0] for k in seen}
    total = sum(stats_rows.values()) or 1
    add("key_violations", not key_bad, len(key_bad), 0, "null or conflicting keys", key_bad)
    add("required_null_rate", not req_bad, len(req_bad), 0, "nulls in required properties", req_bad)
    add("type_coercion_fail_rate", len(coerce) / total <= THRESH["type_coercion_fail_rate"], round(len(coerce) / total, 4),
        THRESH["type_coercion_fail_rate"], "values that do not coerce", coerce)
    add("enum_violation_rate", len(enum_bad) / total <= THRESH["enum_violation_rate"], round(len(enum_bad) / total, 4),
        THRESH["enum_violation_rate"], "values outside the enum", enum_bad)
    links_loaded, worst, ex = {}, 1.0, []
    for l in prop["linkTypes"]:
        s = l["source"]
        if s["kind"] != "fk":
            continue
        ft = next((o for o in prop["objectTypes"] if o["name"] == l["from"]), None)
        tt = next((o for o in prop["objectTypes"] if o["name"] == l["to"]), None)
        if not ft or not tt:
            continue
        data = tabs.get(ft["source_table"])
        tk = key_vals.get(tt["name"], set())
        vals = [_get(data, s["from_col"], r) for r in data[0]] if data else []
        vals = [v for v in vals if not is_null(v)]
        ok = sum(v in tk for v in vals)
        links_loaded[l["name"]] = ok
        rate = ok / len(vals) if vals else 1.0
        if rate < THRESH["link_resolution"]:
            ex.append(f"{l['name']}: {round(rate, 3)}")
        worst = min(worst, rate)
    add("link_resolution", worst >= THRESH["link_resolution"], round(worst, 4), THRESH["link_resolution"],
        "share of foreign keys that resolve", ex)
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "stats": {"rows_loaded": stats_rows, "links_loaded": links_loaded}, "validator": "lite", "seconds": 0.0}
