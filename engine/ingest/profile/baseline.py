"""Heuristic baseline proposal (no LLM): a plain dict in the Proposal shape (engine/ingest/proposal.py validates it)."""
from __future__ import annotations

import json

from ..names import clean_text, looks_injected, safe_link, safe_prop
from . import textsim

TYPE_MAP = {"int": "int", "float": "float", "bool": "bool", "timestamp": "timestamp", "string": "string"}


def _col(profile: dict, cid: str) -> dict:
    t = profile["tables"][int(cid[1:].split(".")[0]) - 1]
    return t["columns"][int(cid.split(".c")[1]) - 1]


def baseline_proposal(profile: dict) -> dict:
    tables = {t["id"]: t for t in profile["tables"]}
    fks = [f for f in profile["fks"] if f["declared"] or f["confidence"] >= 0.6]
    notes: list[str] = []
    incoming: dict[str, int] = {}
    fk_cols = {f["from"] for f in fks}   # reference columns stay plain strings/ints (an enum of ids is not a vocabulary)
    for f in fks:
        incoming[f["to"]] = incoming.get(f["to"], 0) + 1

    def key_of(t: dict) -> list[str] | None:
        if not t["key_candidates"]:
            return None
        return sorted(t["key_candidates"], key=lambda kc: -sum(incoming.get(c, 0) for c in kc))[0]

    obj_tables = {}
    for t in profile["tables"]:
        if t["is_join_table"] and len([f for f in fks if f["from"].startswith(t["id"] + ".")]) == 2:
            continue
        k = key_of(t)
        if k is None:
            notes.append(f"table {t['id']} ({clean_text(t['name'], 40)}) has no key candidate and was skipped")
            continue
        obj_tables[t["id"]] = (t, k)

    object_types = []
    for tid, (t, key) in obj_tables.items():
        by_col_geo = {c["id"]: c for c in t["columns"]}
        props, taken = [], set()
        keyset = set(key)
        for c in t["columns"]:
            ptype, transform = _prop_type(c, enum_ok=c["id"] not in fk_cols)
            if isinstance(ptype, dict):
                mapping = {}
                used: set[str] = set()
                for v in ptype["enum"]:
                    mapping[v] = safe_prop(v, used)
                transform = "enum_map:" + json.dumps(mapping, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                ptype = {"enum": sorted(mapping.values())}
            props.append({"name": safe_prop(c["ident"], taken), "type": ptype, "required": c["id"] in keyset and c["null_rate"] == 0.0,
                          "column": c["id"], "transform": transform, "label": _label(c["name"], c["id"])})
        for c in t["columns"]:
            if c["geo"] and c["geo"]["role"] == "lat" and c["geo"]["pair"]:
                props.append({"name": safe_prop("location", taken), "type": "point", "required": False, "column": None,
                              "transform": f"point({c['id']},{c['geo']['pair']})", "label": "location"})
                break
        object_types.append({"name": t["ident"], "label": _label(t["name"], t["id"]), "extends": None, "key": key,
                             "source_table": tid, "properties": props})

    # A link and a property share one RDF predicate (namespace + name): link names must never equal any property name.
    prop_names = {p["name"] for ot in object_types for p in ot["properties"]}
    link_types, taken_links = [], set()

    def link_name(base: str, owner: str = "") -> str:
        if owner and safe_link(base) in taken_links:   # same stem on several tables: qualify with the owning type
            base = f"{owner.lower()}_{base}"
        n = safe_link(base, taken_links)
        if n in prop_names or base in prop_names:
            taken_links.discard(n)
            n = safe_link(f"{safe_link(base)[:34]}_link", taken_links)
            while n in prop_names:
                taken_links.discard(n)
                n = safe_link(f"{n[:34]}_rel", taken_links)
        return n

    for f in fks:
        ft, tt = f["from"].split(".")[0], f["to"].split(".")[0]
        if ft not in obj_tables or tt not in obj_tables:
            continue
        fcol = _col(profile, f["from"])
        stem = "_".join(textsim.stem(fcol["name"]))
        base = stem or f"{obj_tables[ft][0]['ident']}_to_{obj_tables[tt][0]['ident']}"
        link_types.append({"name": link_name(base, obj_tables[ft][0]["ident"]), "from": obj_tables[ft][0]["ident"],
                           "to": obj_tables[tt][0]["ident"], "cardinality": f["cardinality"], "inverse": None,
                           "transitive": False, "required": False,
                           "source": {"kind": "fk", "from_col": f["from"], "to_type_key": f["to"]}})
    for t in profile["tables"]:
        if t["id"] in obj_tables:
            continue
        jf = [f for f in fks if f["from"].startswith(t["id"] + ".")]
        if t["is_join_table"] and len(jf) == 2:
            a, b = jf
            ta, tb = a["to"].split(".")[0], b["to"].split(".")[0]
            if ta in obj_tables and tb in obj_tables:
                base = f"{textsim.stem(_col(profile, a['from'])['name']) and '_'.join(textsim.stem(_col(profile, a['from'])['name'])) or obj_tables[ta][0]['ident']}_{'_'.join(textsim.stem(_col(profile, b['from'])['name'])) or obj_tables[tb][0]['ident']}"
                link_types.append({"name": link_name(base), "from": obj_tables[ta][0]["ident"],
                                   "to": obj_tables[tb][0]["ident"], "cardinality": "many-to-many", "inverse": None,
                                   "transitive": False, "required": False,
                                   "source": {"kind": "join_table", "table": t["id"], "src_col": a["from"], "dst_col": b["from"]}})
                extra = [c for c in t["columns"] if c["id"] not in (a["from"], b["from"])]
                if extra:
                    notes.append(f"join table {t['id']} has {len(extra)} payload columns that are not imported")
            else:
                notes.append(f"join table {t['id']} was skipped (an endpoint has no key)")
    low = [f for f in profile["fks"] if not f["declared"] and f["confidence"] < 0.6]
    if low:
        notes.append(f"{len(low)} low-confidence foreign key candidates were not used")
    return {
        "version": 1, "profile_hash": profile["profile_hash"],
        "namespace": f"https://isnad.local/ns/{profile['profile_hash'][:12]}#",
        "objectTypes": object_types, "linkTypes": link_types, "classes": [], "actions": [],
        "roles": [{"name": "admin", "description": "Full access", "access": "admin"},
                  {"name": "viewer", "description": "Read-only access to all imported data", "access": "read"}],
        "rationale": "Heuristic baseline from the data profile (types, keys, declared and inferred foreign keys); no model was used.",
        "notes": notes,
    }


def _label(name: str, fallback: str) -> str:
    """Display label from the source name; instruction-like text is replaced by the column / table id."""
    lab = clean_text(name, 64)
    return fallback if looks_injected(lab) else lab


def _prop_type(c: dict, enum_ok: bool = True):
    t = c["type"]
    if t == "geojson":
        gt = next((n.split("=", 1)[1] for n in c["notes"] if n.startswith("geojson_type=")), "")
        if gt == "Point":
            return "point", "geojson"
        if gt == "LineString":
            return "linestring", "geojson"
        return "string", "none"
    if (enum_ok and c["enum_candidate"] and t == "string" and len(c["enum_candidate"]) >= 2
            and not any(looks_injected(v) for v in c["enum_candidate"])):
        return {"enum": list(c["enum_candidate"])}, "none"
    if t == "timestamp":
        return "timestamp", f"timestamp:{c['format'] or 'iso8601'}"
    if t == "string":
        return "string", "none"
    return TYPE_MAP[t], ("none" if t == "string" else t)
