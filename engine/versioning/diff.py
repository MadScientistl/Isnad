"""Structural diff between two ontology YAML texts, with breaking / additive classification."""
from __future__ import annotations

import yaml

from engine.compiler.model import OntologyError, parse_ontology

KINDS = ("objectTypes", "properties", "linkTypes", "actions", "roles", "functions", "webhooks")
MAX_YAML_CHARS = 200_000
MAX_YAML_EVENTS = 100_000   # parser events (~ nodes): rejects absurdly large documents before building them
MAX_YAML_ALIASES = 10       # anchors/aliases beyond this are refused (alias expansion bombs)


def _check_yaml_bounds(yaml_text: str) -> None:
    if not isinstance(yaml_text, str):
        raise OntologyError("ontology YAML must be text")
    if len(yaml_text) > MAX_YAML_CHARS:
        raise OntologyError(f"ontology YAML too large (max {MAX_YAML_CHARS} characters)")
    events = aliases = 0
    try:
        for ev in yaml.parse(yaml_text, Loader=yaml.SafeLoader):
            events += 1
            if isinstance(ev, yaml.AliasEvent):
                aliases += 1
            if events > MAX_YAML_EVENTS:
                raise OntologyError("ontology YAML has too many nodes")
            if aliases > MAX_YAML_ALIASES:
                raise OntologyError(f"ontology YAML uses too many aliases (max {MAX_YAML_ALIASES})")
    except yaml.YAMLError as e:
        raise OntologyError(f"YAML parse error: {e}") from e


def parse(yaml_text: str):
    """Parse + validate with the compiler model. Raises OntologyError with a clear message."""
    _check_yaml_bounds(yaml_text)
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise OntologyError(f"YAML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise OntologyError("ontology root must be a mapping")
    return parse_ontology(raw)


def _tup(x):
    return sorted(x or ())


def _norm(m) -> dict:
    out = {k: {} for k in KINDS}
    for o in m.object_types.values():
        out["objectTypes"][o.name] = {}
        for p in o.properties:
            out["properties"][f"{o.name}.{p.name}"] = {"type": p.type, "required": bool(p.required),
                                                       "enum": _tup(p.enum)}
    for l in m.link_types.values():
        out["linkTypes"][l.name] = {"from": l.src, "to": l.dst, "cardinality": l.cardinality,
                                    "required": bool(l.required), "transitive": bool(l.transitive),
                                    "inverse": l.inverse}
    for a in m.actions.values():
        out["actions"][a.name] = {"params": dict(a.params), "roles": _tup(a.roles),
                                  "approval_required": bool(a.approval_required),
                                  "approvers": _tup(a.approvers), "writes": _tup(a.writes),
                                  "function": a.function, "webhooks": _tup(a.webhooks),
                                  "rollback": a.rollback}
    for r in m.roles.values():
        agent = None
        if r.agent is not None:
            agent = {"allowlist": _tup(r.agent.allowlist), "tools": _tup(r.agent.tools)}
        out["roles"][r.name] = {
            "types": {t: {"read": bool(x.read), "write": bool(x.write), "where": x.where, "mask": _tup(x.mask)}
                      for t, x in r.types.items()},
            "links": _tup(r.links), "actions": _tup(r.actions), "approves": _tup(r.approves),
            "all_actions": bool(r.all_actions), "all_approves": bool(r.all_approves),
            "region": r.region, "agent": agent}
    for f in m.functions.values():
        out["functions"][f.name] = {"params": dict(f.params), "roles": _tup(f.roles)}
    for w in m.webhooks.values():
        out["webhooks"][w.name] = {"url_env": w.url_env, "method": w.method, "max_attempts": w.max_attempts}
    return out


def _lost(old, new):
    return sorted(set(old or ()) - set(new or ()))


def _breaking_change(kind: str, name: str, o: dict, n: dict) -> list[str]:
    b: list[str] = []
    if kind == "properties":
        if o["type"] != n["type"]:
            b.append(f"property {name}: type changed {o['type']} -> {n['type']}")
        if n["required"] and not o["required"]:
            b.append(f"property {name}: made required (no default)")
        for v in _lost(o["enum"], n["enum"]):
            b.append(f"property {name}: enum value {v!r} removed")
    elif kind == "linkTypes":
        for k in ("from", "to", "cardinality", "transitive", "inverse", "required"):
            if o[k] != n[k]:
                if k == "required" and not n[k]:
                    continue
                b.append(f"link {name}: {k} changed {o[k]!r} -> {n[k]!r}")
    elif kind == "actions":
        for p in _lost(o["params"], n["params"]):
            b.append(f"action {name}: param {p!r} removed")
        for p in set(o["params"]) & set(n["params"]):
            if o["params"][p] != n["params"][p]:
                b.append(f"action {name}: param {p!r} changed {o['params'][p]!r} -> {n['params'][p]!r}")
        for r in _lost(o["roles"], n["roles"]):
            b.append(f"action {name}: role {r!r} no longer allowed")
        if n["approval_required"] and not o["approval_required"]:
            b.append(f"action {name}: approval now required")
        for a in _lost(o["approvers"], n["approvers"]):
            b.append(f"action {name}: approver {a!r} removed")
        for key in ("function", "rollback"):
            if o[key] != n[key]:
                b.append(f"action {name}: {key} changed {o[key]!r} -> {n[key]!r}")
        for w in _lost(o["writes"], n["writes"]):
            b.append(f"action {name}: write target {w!r} removed")
    elif kind == "roles":
        for t, ox in o["types"].items():
            nx = n["types"].get(t)
            if nx is None:
                b.append(f"role {name}: access to type {t} removed")
                continue
            for k in ("read", "write"):
                if ox[k] and not nx[k]:
                    b.append(f"role {name}: {k} on {t} revoked")
            if nx["where"] != ox["where"]:
                b.append(f"role {name}: row filter on {t} changed")
            for m in sorted(set(nx["mask"]) - set(ox["mask"])):
                b.append(f"role {name}: property {t}.{m} now masked")
        for key in ("links", "actions", "approves"):
            for x in _lost(o[key], n[key]):
                b.append(f"role {name}: {key} entry {x!r} removed")
        for key in ("all_actions", "all_approves"):
            if o[key] and not n[key]:
                b.append(f"role {name}: {key} dropped")
        if o["region"] != n["region"]:
            b.append(f"role {name}: region changed {o['region']!r} -> {n['region']!r}")
        if o["agent"] and not n["agent"]:
            b.append(f"role {name}: agent removed")
        elif o["agent"] and n["agent"]:
            for key in ("allowlist", "tools"):
                for x in _lost(o["agent"][key], n["agent"][key]):
                    b.append(f"role {name}: agent {key} entry {x!r} removed")
    elif kind == "functions":
        for p in _lost(o["params"], n["params"]):
            b.append(f"function {name}: param {p!r} removed")
        for p in set(o["params"]) & set(n["params"]):
            if o["params"][p] != n["params"][p]:
                b.append(f"function {name}: param {p!r} changed")
        for r in _lost(o["roles"], n["roles"]):
            b.append(f"function {name}: role {r!r} removed")
    elif kind == "webhooks":
        for k in ("url_env", "method"):
            if o[k] != n[k]:
                b.append(f"webhook {name}: {k} changed {o[k]!r} -> {n[k]!r}")
    return b


def _sensitive_change(kind: str, name: str, o: dict, n: dict, old: dict) -> list[str]:
    """Changes of EXISTING roles/actions that WIDEN what somebody may see or do (review L5): they are additive
    (not `breaking`) but need a human's explicit comment on approval."""
    out: list[str] = []
    if kind == "roles":
        # roles declared `all` expand to the new types/links/actions of the same diff: only entries that existed
        # BEFORE are a widening of what the role could already reach
        for t, nx in n["types"].items():
            ox = o["types"].get(t)
            if ox is None:
                if t in old["objectTypes"]:
                    out.append(f"role {name}: gains access to existing type {t}")
                continue
            for k in ("read", "write"):
                if nx[k] and not ox[k]:
                    out.append(f"role {name}: {k} on {t} granted")
            if ox["where"] != nx["where"]:
                out.append(f"role {name}: row filter on {t} changed")
            for m in sorted(set(ox["mask"]) - set(nx["mask"])):
                out.append(f"role {name}: property {t}.{m} no longer masked")
        for key, known in (("links", old["linkTypes"]), ("actions", old["actions"]), ("approves", old["actions"])):
            for x in sorted((set(n[key]) - set(o[key])) & set(known)):
                out.append(f"role {name}: {key} entry {x!r} added")
        for key in ("all_actions", "all_approves"):
            if n[key] and not o[key]:
                out.append(f"role {name}: {key} granted")
        if o["region"] != n["region"]:
            out.append(f"role {name}: region changed {o['region']!r} -> {n['region']!r}")
        if n["agent"] and not o["agent"]:
            out.append(f"role {name}: agent enabled")
        elif n["agent"] and o["agent"]:
            for key in ("allowlist", "tools"):
                for x in sorted(set(n["agent"][key]) - set(o["agent"][key])):
                    out.append(f"role {name}: agent {key} entry {x!r} added")
    elif kind == "actions":
        for r in sorted(set(n["roles"]) - set(o["roles"])):
            out.append(f"action {name}: role {r!r} may now execute it")
        for a in sorted(set(n["approvers"]) - set(o["approvers"])):
            out.append(f"action {name}: approver {a!r} added")
        if o["approval_required"] and not n["approval_required"]:
            out.append(f"action {name}: approval no longer required")
    return out


def diff(old_yaml: str, new_yaml: str) -> dict:
    """{added:{kind:[names]}, removed:{kind:[names]}, changed:{kind:{name:{field:[old,new]}}}, breaking:[str],
    sensitive:[str]} (`sensitive` = permission widening of existing roles / actions).

    Raises OntologyError when either document is invalid. Kinds: objectTypes, properties (Type.prop),
    linkTypes, actions, roles, functions, webhooks.
    """
    o, n = _norm(parse(old_yaml)), _norm(parse(new_yaml))
    res = {"added": {}, "removed": {}, "changed": {}, "breaking": [], "sensitive": []}
    for kind in KINDS:
        ok, nk = o[kind], n[kind]
        add, rem = sorted(set(nk) - set(ok)), sorted(set(ok) - set(nk))
        if add:
            res["added"][kind] = add
        if rem:
            res["removed"][kind] = rem
            res["breaking"] += [f"{kind[:-1] if kind.endswith('s') else kind} {x} removed" for x in rem]
        for name in sorted(set(ok) & set(nk)):
            if ok[name] != nk[name]:
                fields = {f: [ok[name].get(f), nk[name].get(f)] for f in nk[name] if ok[name].get(f) != nk[name].get(f)}
                res["changed"].setdefault(kind, {})[name] = fields
                res["breaking"] += _breaking_change(kind, name, ok[name], nk[name])
                res["sensitive"] += _sensitive_change(kind, name, ok[name], nk[name], o)
    return res


def is_empty(d: dict) -> bool:
    return not (d["added"] or d["removed"] or d["changed"])
