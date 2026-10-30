"""Output-time masking for responses that carry diff / before / after / dry_run / params values.

Role-relative; stored values stay raw. Shared by the REST API and the agent tools so both mask identically.
`om` is the compiled ontology model (`compile_ontology(path).model`).
"""
from __future__ import annotations

import json


def mask_snapshot(om, p, type_: str, snap):
    """Diff snapshots come from the engine's unmasked read: null the columns this role may not see."""
    if p.role == "admin" or not isinstance(snap, dict):
        return snap
    a = om.roles[p.role].types.get(type_)
    mask = set(a.mask) if a else set()
    return {k: (None if k in mask else v) for k, v in snap.items()} if mask else snap

def mask_diff(om, p, diff):
    if not isinstance(diff, list):
        return diff
    return [{**d, "before": mask_snapshot(om, p, d.get("type"), d.get("before")),
             "after": mask_snapshot(om, p, d.get("type"), d.get("after"))}
            if isinstance(d, dict) and d.get("kind", "object") == "object" else d for d in diff]

def mask_summary(om, p, snap):
    """before/after summaries are keyed "Type:uuid"."""
    if p.role == "admin" or not isinstance(snap, dict):
        return snap
    return {k: mask_snapshot(om, p, k.partition(":")[0], v) for k, v in snap.items()}

def mask_params(om, p, name, params):
    """Generic object actions carry property values in params (type + props)."""
    if p.role == "admin" or not isinstance(params, dict):
        return params
    a = om.actions.get(name) if name else None
    if a is not None:  # declarative actions: params that flow straight into a masked column
        hide = set()
        for r in a.rules:
            acc = om.roles[p.role].types.get(r.type) if r.type else None
            masked = set(acc.mask) if acc else set()
            for col, val in (r.set or {}).items():
                if col in masked and isinstance(val, str) and val.startswith("$"):
                    hide.add(val[1:])
        if hide:
            params = {k: (None if k in hide else v) for k, v in params.items()}
    if "props" not in params:
        return params
    props, as_str = params["props"], False
    if isinstance(props, str):
        try:
            props, as_str = json.loads(props), True
        except ValueError:
            return {**params, "props": None}
    if not isinstance(props, dict):
        return params
    props = mask_snapshot(om, p, params.get("type"), props)
    return {**params, "props": json.dumps(props) if as_str else props}

def mask_out(om, p, x):
    """THE masking function for every response that can carry diff / before / after / dry_run /
    params values. Applied recursively; role-relative, at output time (stored values stay raw)."""
    if p.role == "admin":
        return x
    if isinstance(x, list):
        return [mask_out(om, p, i) for i in x]
    if not isinstance(x, dict):
        return x
    out = dict(x)
    for k, v in x.items():
        if k == "diff":
            out[k] = mask_diff(om, p, v)
        elif k == "dry_run" and isinstance(v, dict):
            # scenario dry runs may also carry the scenario-side diff; impact holds aggregate KPIs only
            out[k] = {**v, **{dk: mask_diff(om, p, v.get(dk)) for dk in ("diff", "scenario_diff", "main_diff")
                              if dk in v}}
        elif k in ("before", "after") and isinstance(v, dict):
            if all(isinstance(i, dict) for i in v.values()) and all(":" in str(i) for i in v):
                out[k] = mask_summary(om, p, v)
        elif k == "params":
            out[k] = mask_params(om, p, x.get("name") or x.get("action"), v)
        elif isinstance(v, (dict, list)):
            out[k] = mask_out(om, p, v)
    return out
