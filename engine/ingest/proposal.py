"""Ontology proposal (what the generator model emits) and its deterministic YAML producer (Phase 6).

``Proposal.model_validate(json)`` is the only way untrusted model output enters the system: extra fields are
refused, list sizes and string lengths are bounded, every identifier must already be in canonical form
(``names.safe_*(x) == x``), enum values are capped and control-char free, and ids must resolve against the
profile (``to_yaml`` re-checks that). ``to_yaml(proposal, profile)`` is the ONLY producer of ontology YAML: it
emits declarative ``rules:`` and the five generic actions only (no ``functions:``, no custom handlers), expands
roles from a fixed template (no masks, no row filters), derives the namespace on the server and returns the YAML
text plus the import mapping. The YAML is built as plain data and dumped with ``yaml.safe_dump`` so no string
from the data can alter its structure.

Role template (documented, fixed):
  * ``admin`` is always present: all types read+write, all links, all actions, approves all.
  * ``read``: read on every type, every link, no actions.
  * ``write``: read+write on every type, every link, the generic actions create_object, update_object,
    create_link, end_link (NOT delete_object) and every declared action; no approvals.
  * Declared actions carry ``approval: {approvers: [admin]}`` (agent proposals are approved by an admin).
  * Every role gets a read-only agent block (query/traverse/path/history tools); a write role's agent may only
    propose the declared actions. Nothing else is generated for roles.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

PROPOSAL_VERSION = 1
SCALAR_TYPES = ("string", "int", "float", "bool", "timestamp", "point", "linestring")
GENERIC = ("create_object", "update_object", "delete_object", "create_link", "end_link")
CARDINALITIES = ("many-to-many", "many-to-one", "one-to-many", "one-to-one")
READ_TOOLS = ("query_objects", "traverse", "find_paths", "impact_analysis", "get_history", "as_of", "diff")
MAX_TYPES, MAX_PROPS, MAX_LINKS, MAX_CLASSES, MAX_ACTIONS, MAX_ROLES = 50, 60, 100, 50, 100, 8
MAX_ENUM_VALUES, MAX_ENUM_LEN = 64, 64

_PROP_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_TYPE_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,40}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_COL_RE = re.compile(r"^t[0-9]{1,4}\.c[0-9]{1,4}$")
_TABLE_RE = re.compile(r"^t[0-9]{1,4}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_TRANSFORM_RE = re.compile(
    r"^(none|int|float|bool|geojson|timestamp:[^\x00-\x1f\x7f]{1,40}|enum_map:.{1,4000}|"
    r"point\(t[0-9]{1,4}\.c[0-9]{1,4},t[0-9]{1,4}\.c[0-9]{1,4}\))$", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ProposalError(ValueError):
    """The proposal does not resolve against the profile or does not produce a valid ontology."""


def _canon(kind: str, v: str) -> str:
    """Identifier must be canonical: matches the regex and is a fixed point of names.safe_*."""
    rx = {"prop": _PROP_RE, "type": _TYPE_RE, "role": _ROLE_RE}[kind]
    if not isinstance(v, str) or not rx.fullmatch(v):
        raise ValueError(f"invalid {kind} name {v[:50]!r}")
    if kind in ("prop", "type"):
        try:
            from engine.ingest import names
        except ImportError:  # names.py is created by another module owner; the regex above is the floor
            return v
        fn = names.safe_prop if kind == "prop" else names.safe_type
        if fn(v) != v:
            raise ValueError(f"{kind} name {v!r} is reserved or not canonical (expected {fn(v)!r})")
    return v


def _text(v: str, n: int, *, allow_empty=True) -> str:
    if _CTRL.search(v):
        raise ValueError("control characters are not allowed")
    if len(v) > n:
        raise ValueError(f"too long (max {n})")
    if not allow_empty and not v.strip():
        raise ValueError("must not be empty")
    return v


Label = Annotated[str, StringConstraints(max_length=120)]
ColId = Annotated[str, StringConstraints(pattern=_COL_RE.pattern)]
TableId = Annotated[str, StringConstraints(pattern=_TABLE_RE.pattern)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, validate_assignment=False)


class EnumType(_Strict):
    enum: list[str] = Field(min_length=1, max_length=MAX_ENUM_VALUES)

    @field_validator("enum")
    @classmethod
    def _vals(cls, v):
        seen = set()
        for x in v:
            _text(x, MAX_ENUM_LEN, allow_empty=False)
            if x in seen:
                raise ValueError(f"duplicate enum value {x!r}")
            seen.add(x)
        return v


class PropertyP(_Strict):
    name: str
    type: str | EnumType
    required: bool = False
    column: ColId | None = None
    transform: str = "none"
    label: Label = ""

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _canon("prop", v)

    @field_validator("type")
    @classmethod
    def _t(cls, v):
        if isinstance(v, str) and v not in SCALAR_TYPES:
            raise ValueError(f"unknown type {v[:30]!r}")
        return v

    @field_validator("transform")
    @classmethod
    def _tr(cls, v):
        if not _TRANSFORM_RE.fullmatch(v) or (not v.startswith("enum_map:") and _CTRL.search(v)):
            raise ValueError(f"invalid transform {v[:40]!r}")
        if v.startswith("enum_map:"):
            try:
                m = json.loads(v[9:])
            except ValueError as e:
                raise ValueError("enum_map must be JSON") from e
            if not isinstance(m, dict) or len(m) > 500 or not all(
                    isinstance(a, str) and isinstance(b, str) and len(a) <= 80 and len(b) <= MAX_ENUM_LEN
                    and not _CTRL.search(a + b) for a, b in m.items()):
                raise ValueError("enum_map must be a JSON object of short strings")
        return v

    @field_validator("label")
    @classmethod
    def _l(cls, v):
        return _text(v, 120)


class ObjectTypeP(_Strict):
    name: str
    label: Label = ""
    extends: str | None = None
    key: list[ColId] = Field(min_length=1, max_length=2)
    source_table: TableId
    properties: list[PropertyP] = Field(max_length=MAX_PROPS)

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _canon("type", v)

    @field_validator("extends")
    @classmethod
    def _e(cls, v):
        return None if v is None else _canon("type", v)

    @field_validator("label")
    @classmethod
    def _l(cls, v):
        return _text(v, 120)


class FkSource(_Strict):
    kind: Literal["fk"]
    from_col: ColId
    to_type_key: ColId


class JoinSource(_Strict):
    kind: Literal["join_table"]
    table: TableId
    src_col: ColId
    dst_col: ColId


class LinkTypeP(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    from_: str = Field(alias="from")
    to: str
    cardinality: Literal["many-to-many", "many-to-one", "one-to-many", "one-to-one"] = "many-to-many"
    inverse: str | None = None
    transitive: bool = False
    required: bool = False
    source: Annotated[FkSource | JoinSource, Field(discriminator="kind")]

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _canon("prop", v)

    @field_validator("inverse")
    @classmethod
    def _i(cls, v):
        return None if v is None else _canon("prop", v)

    @field_validator("from_", "to")
    @classmethod
    def _t(cls, v):
        return _canon("type", v)


class ClassP(_Strict):
    name: str
    extends: str | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return _canon("type", v)

    @field_validator("extends")
    @classmethod
    def _e(cls, v):
        return None if v is None else _canon("type", v)


class ActionP(_Strict):
    name: str
    description: str = ""
    kind: Literal["update_props", "set_status", "link", "unlink"]
    type: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        _canon("prop", v)
        if v in GENERIC:
            raise ValueError(f"action name {v!r} is reserved")
        return v

    @field_validator("type")
    @classmethod
    def _t(cls, v):
        return _canon("type", v)

    @field_validator("description")
    @classmethod
    def _d(cls, v):
        return _text(v, 300)

    @model_validator(mode="after")
    def _params(self):
        p, k = self.params, self.kind
        allowed = {"update_props": {"props"}, "set_status": {"prop", "value", "from"}, "link": {"link"},
                   "unlink": {"link"}}[k]
        extra = set(p) - allowed
        if extra:
            raise ValueError(f"action {self.name}: unknown params {sorted(extra)[:3]} for kind {k}")
        if k == "update_props":
            props = p.get("props")
            if not isinstance(props, list) or not 1 <= len(props) <= 30 or not all(isinstance(x, str) for x in props):
                raise ValueError("update_props needs params.props: 1..30 property names")
            for x in props:
                _canon("prop", x)
            if len(set(props)) != len(props):
                raise ValueError("duplicate property in params.props")
        elif k == "set_status":
            for f in ("prop", "value"):
                if not isinstance(p.get(f), str):
                    raise ValueError(f"set_status needs params.{f}")
            _canon("prop", p["prop"])
            _text(p["value"], MAX_ENUM_LEN, allow_empty=False)
            fr = p.get("from")
            if fr is not None:
                if not isinstance(fr, list) or len(fr) > MAX_ENUM_VALUES or not all(isinstance(x, str) for x in fr):
                    raise ValueError("params.from must be a list of enum values")
                for x in fr:
                    _text(x, MAX_ENUM_LEN, allow_empty=False)
        else:
            if not isinstance(p.get("link"), str):
                raise ValueError(f"{k} needs params.link")
            _canon("prop", p["link"])
        return self


class RoleP(_Strict):
    name: str
    description: str = ""
    access: Literal["read", "write", "admin"]

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        _canon("role", v)
        if v == "sim":
            raise ValueError("role name 'sim' is reserved")
        return v

    @field_validator("description")
    @classmethod
    def _d(cls, v):
        return _text(v, 300)

    @model_validator(mode="after")
    def _a(self):
        if (self.access == "admin") != (self.name == "admin"):
            raise ValueError("access 'admin' is only for the role named 'admin' (and that role is always admin)")
        return self


class Proposal(_Strict):
    version: Literal[1] = 1
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(default="", max_length=200)  # informational: the server derives the real one
    objectTypes: list[ObjectTypeP] = Field(min_length=1, max_length=MAX_TYPES)
    linkTypes: list[LinkTypeP] = Field(default_factory=list, max_length=MAX_LINKS)
    classes: list[ClassP] = Field(default_factory=list, max_length=MAX_CLASSES)
    actions: list[ActionP] = Field(default_factory=list, max_length=MAX_ACTIONS)
    roles: list[RoleP] = Field(default_factory=list, max_length=MAX_ROLES)
    rationale: str = Field(default="", max_length=4000)
    notes: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _unique(self):
        for what, items in (("object type", [o.name for o in self.objectTypes]),
                            ("link type", [l.name for l in self.linkTypes]),
                            ("class", [c.name for c in self.classes]),
                            ("action", [a.name for a in self.actions]),
                            ("role", [r.name for r in self.roles])):
            if len(set(items)) != len(items):
                raise ValueError(f"duplicate {what} name")
        for o in self.objectTypes:
            names = [p.name for p in o.properties]
            if len(set(names)) != len(names):
                raise ValueError(f"duplicate property name in {o.name}")
        return self


# ------------------------------------------------------------------ profile access
def _pd(profile) -> dict:
    if isinstance(profile, dict):
        return profile
    for attr in ("model_dump", "to_dict", "dict"):
        f = getattr(profile, attr, None)
        if callable(f):
            return f()
    raise ProposalError("unsupported profile object")


def _index(profile) -> tuple[dict, dict]:
    p = _pd(profile)
    tables = {t["id"]: t for t in p.get("tables", [])}
    cols = {c["id"]: (t["id"], c) for t in tables.values() for c in t.get("columns", [])}
    return tables, cols


def map_profile_type(ptype: str) -> str:
    """Profile column type -> ontology property type."""
    return {"int": "int", "float": "float", "bool": "bool", "timestamp": "timestamp", "string": "string",
            "geojson": "point"}.get(ptype, "string")


def derive_namespace(slug: str) -> str:
    s = _SLUG_RE.sub("_", slug.lower()).strip("_")[:40] or "ws"
    if not s[0].isalpha():
        s = "w" + s
    return f"https://isnad.local/ws/{s}#"


def _default_slug(profile: dict, proposal: Proposal) -> str:
    return "w" + proposal.profile_hash[:10]


# ------------------------------------------------------------------ generic actions (copied from supply_chain.yaml)
_GENERIC_DECL = {
    "create_object": {"description": "Create an object of any type (generic, used by REST).",
                      "params": {"type": "string!", "id": "uuid", "props": "string!"}},
    "update_object": {"description": "Replace properties of an object.",
                      "params": {"type": "string!", "id": "uuid!", "props": "string!"}},
    "delete_object": {"description": "End an object's validity.",
                      "params": {"type": "string!", "id": "uuid!"}},
    "create_link": {"description": "Create a link between two objects.",
                    "params": {"link": "string!", "src": "uuid!", "dst": "uuid!"}},
    "end_link": {"description": "End a link between two objects.",
                 "params": {"link": "string!", "src": "uuid!", "dst": "uuid!"}},
}
_WRITE_GENERIC = ("create_object", "update_object", "create_link", "end_link")


def _ptype(p: PropertyP) -> str:
    return "enum" if isinstance(p.type, EnumType) else p.type


def _param_type(p: PropertyP) -> str:
    return {"enum": "string"}.get(_ptype(p), _ptype(p))


# ------------------------------------------------------------------ resolution
def _resolve(proposal: Proposal, profile) -> dict:
    """Check every reference; returns lookup tables. Raises ProposalError listing all problems."""
    tables, cols = _index(profile)
    prof = _pd(profile)
    errs: list[str] = []
    if prof.get("profile_hash") and prof["profile_hash"] != proposal.profile_hash:
        errs.append("profile_hash does not match the profile")
    types = {o.name: o for o in proposal.objectTypes}
    classes = {c.name: c for c in proposal.classes}
    used_tables: dict[str, str] = {}
    for o in proposal.objectTypes:
        w = f"type {o.name}"
        if o.source_table not in tables:
            errs.append(f"{w}: unknown source table {o.source_table}")
            continue
        used_tables.setdefault(o.source_table, o.name)
        for k in o.key:
            if k not in cols or cols[k][0] != o.source_table:
                errs.append(f"{w}: key column {k} is not in {o.source_table}")
        for p in o.properties:
            pw = f"{w}.{p.name}"
            if p.column is not None and (p.column not in cols or cols[p.column][0] != o.source_table):
                errs.append(f"{pw}: column {p.column} is not in {o.source_table}")
            tr = p.transform
            pt = _ptype(p)
            if tr.startswith("point("):
                refs = re.findall(r"t[0-9]+\.c[0-9]+", tr)
                for r in refs:
                    if r not in cols or cols[r][0] != o.source_table:
                        errs.append(f"{pw}: transform column {r} is not in {o.source_table}")
                if pt != "point":
                    errs.append(f"{pw}: point(...) transform needs type point")
            elif tr == "geojson":
                if pt != "point":
                    errs.append(f"{pw}: geojson transform needs type point")
                if p.column is None:
                    errs.append(f"{pw}: geojson transform needs a column")
            elif pt == "point" and tr == "none" and p.column is None:
                errs.append(f"{pw}: point property needs a point(lat,lon) or geojson transform")
            elif pt in ("point", "linestring") and not tr.startswith(("point(", "geojson")) and p.column is not None:
                errs.append(f"{pw}: {pt} property needs a geo transform")
            if tr.startswith("enum_map:") and pt != "enum":
                errs.append(f"{pw}: enum_map transform needs an enum type")
            if p.required and p.column is None and not tr.startswith("point("):
                errs.append(f"{pw}: required property without a column")
        if o.extends is not None and o.extends == o.name:
            errs.append(f"{w}: extends itself")
    for l in proposal.linkTypes:
        w = f"link {l.name}"
        for end, t in (("from", l.from_), ("to", l.to)):
            if t not in types and t not in classes:
                errs.append(f"{w}: '{end}' references unknown type {t}")
        s = l.source
        ft, tt = types.get(l.from_), types.get(l.to)
        if s.kind == "fk":
            if ft is None or tt is None:
                errs.append(f"{w}: fk source needs object types on both ends")
                continue
            if s.from_col not in cols or cols[s.from_col][0] != ft.source_table:
                errs.append(f"{w}: from_col {s.from_col} is not in {ft.name}'s table {ft.source_table}")
            if s.to_type_key not in cols or s.to_type_key not in tt.key:
                errs.append(f"{w}: to_type_key {s.to_type_key} is not a key column of {tt.name}")
        else:
            if s.table not in tables:
                errs.append(f"{w}: unknown join table {s.table}")
                continue
            for c in (s.src_col, s.dst_col):
                if c not in cols or cols[c][0] != s.table:
                    errs.append(f"{w}: column {c} is not in join table {s.table}")
            if ft is None or tt is None:
                errs.append(f"{w}: join_table source needs object types on both ends")
            elif len(ft.key) != 1 or len(tt.key) != 1:
                errs.append(f"{w}: join_table links need single-column keys on both types")
    for a in proposal.actions:
        w = f"action {a.name}"
        t = types.get(a.type)
        if t is None:
            errs.append(f"{w}: unknown type {a.type}")
            continue
        props = {p.name: p for p in t.properties}
        if a.kind == "update_props":
            for x in a.params["props"]:
                if x not in props:
                    errs.append(f"{w}: unknown property {x} on {t.name}")
                elif _ptype(props[x]) == "linestring":
                    errs.append(f"{w}: property {x} cannot be a parameter")
        elif a.kind == "set_status":
            p = props.get(a.params["prop"])
            if p is None or not isinstance(p.type, EnumType):
                errs.append(f"{w}: {a.params['prop']} is not an enum property of {t.name}")
            else:
                vals = set(p.type.enum)
                for v in [a.params["value"], *(a.params.get("from") or [])]:
                    if v not in vals:
                        errs.append(f"{w}: {v!r} is not a value of {t.name}.{p.name}")
        else:
            ln = next((l for l in proposal.linkTypes if l.name == a.params["link"]), None)
            if ln is None:
                errs.append(f"{w}: unknown link {a.params['link']}")
            elif a.type != ln.from_:
                errs.append(f"{w}: type must be the link's source type {ln.from_}")
    for c in proposal.classes:
        if c.extends is not None and c.extends not in classes and c.extends not in types:
            errs.append(f"class {c.name}: extends unknown {c.extends}")
    for o in proposal.objectTypes:
        if o.extends is not None and o.extends not in classes and o.extends not in types:
            errs.append(f"type {o.name}: extends unknown {o.extends}")
    if errs:
        raise ProposalError("proposal does not match the profile:\n  " + "\n  ".join(errs[:60]))
    return {"tables": tables, "cols": cols}


# ------------------------------------------------------------------ YAML
def _prop_yaml(p: PropertyP):
    if isinstance(p.type, EnumType):
        d: dict[str, Any] = {"enum": list(p.type.enum)}
        if p.required:
            d["required"] = True
        return d
    return p.type + ("!" if p.required else "")


def _display_name(profile: dict) -> str:
    n = str((profile.get("source") or {}).get("name") or "")
    n = _CTRL.sub(" ", n).strip()[:80]
    return n or "Imported ontology"


def to_yaml(proposal: Proposal, profile, *, slug: str | None = None) -> tuple[str, dict]:
    """Deterministic ``(yaml_text, mapping)``. Raises ProposalError when the proposal does not resolve against the
    profile or the resulting ontology does not compile. ``slug`` (server side) selects the namespace."""
    if not isinstance(proposal, Proposal):
        proposal = Proposal.model_validate(proposal)
    ix = _resolve(proposal, profile)
    prof = _pd(profile)
    ns = derive_namespace(slug) if slug else derive_namespace(_default_slug(prof, proposal))
    types = {o.name: o for o in proposal.objectTypes}

    out: dict[str, Any] = {"version": 1, "name": _display_name(prof), "namespace": ns}

    cls: dict[str, dict] = {}
    for c in sorted(proposal.classes, key=lambda c: c.name):
        cls[c.name] = {"extends": c.extends} if c.extends else {}
    for o in sorted(proposal.objectTypes, key=lambda o: o.name):
        if o.extends:
            cur = cls.get(o.name, {}).get("extends")
            if cur and cur != o.extends:
                raise ProposalError(f"type {o.name}: extends {o.extends} conflicts with class declaration {cur}")
            cls[o.name] = {"extends": o.extends}
            if o.extends in types and o.extends not in cls:
                cls[o.extends] = {}
    if cls:
        out["classes"] = dict(sorted(cls.items()))

    out["objectTypes"] = {o.name: {"properties": {p.name: _prop_yaml(p) for p in o.properties}}
                          for o in sorted(proposal.objectTypes, key=lambda o: o.name)}

    links = {}
    for l in sorted(proposal.linkTypes, key=lambda l: l.name):
        d: dict[str, Any] = {"from": l.from_, "to": l.to, "cardinality": l.cardinality}
        if l.required:
            d["required"] = True
        if l.transitive:
            d["transitive"] = True
        if l.inverse:
            d["inverse"] = l.inverse
        links[l.name] = d
    if links:
        out["linkTypes"] = links

    # roles (template-expanded)
    rroles = {r.name: r for r in proposal.roles if r.name != "admin"}
    write_roles = sorted(n for n, r in rroles.items() if r.access == "write")
    declared = sorted(a.name for a in proposal.actions)
    exec_roles = ["admin", *write_roles]

    roles: dict[str, Any] = {}
    all_types_rw = {t: {"read": True, "write": True} for t in sorted(types)}
    admin_decl = next((r for r in proposal.roles if r.name == "admin"), None)
    admin_agent: dict[str, Any] = {"tools": [*READ_TOOLS]}
    if declared:
        admin_agent = {"allowlist": declared, "tools": [*READ_TOOLS, "propose_action"],
                       "instructions": "Administrator assistant. Prefer read-only analysis; propose only when "
                                       "evidence is clear. You cannot approve your own proposals."}
    roles["admin"] = {"description": (admin_decl.description if admin_decl and admin_decl.description
                                      else "Administrator. Full access, approves every approval-gated action."),
                      "types": "all", "links": "all", "actions": "all", "approves": "all", "agent": admin_agent}
    for n in sorted(rroles):
        r = rroles[n]
        d = {"description": r.description or f"{r.access.capitalize()} access to all data."}
        if r.access == "write":
            d["types"] = {t: {"read": True, "write": True} for t in sorted(types)}
            d["links"] = sorted(proposal_l.name for proposal_l in proposal.linkTypes)
            d["actions"] = sorted([*_WRITE_GENERIC, *declared])
            ag: dict[str, Any] = {"tools": [*READ_TOOLS]}
            if declared:
                ag = {"allowlist": declared, "tools": [*READ_TOOLS, "propose_action"]}
            d["agent"] = ag
        else:
            d["types"] = {t: {"read": True} for t in sorted(types)}
            d["links"] = sorted(proposal_l.name for proposal_l in proposal.linkTypes)
            d["actions"] = []
            d["agent"] = {"tools": [*READ_TOOLS]}
        roles[n] = d
    out["roles"] = roles

    # actions: generic first, then declared
    actions: dict[str, Any] = {}
    for g in GENERIC:
        a = {"description": _GENERIC_DECL[g]["description"], "params": dict(_GENERIC_DECL[g]["params"]),
             "roles": ["admin"] if g == "delete_object" else list(exec_roles), "function": g}
        actions[g] = a
    for a in sorted(proposal.actions, key=lambda a: a.name):
        actions[a.name] = _declared_action(a, types, proposal, exec_roles)
    out["actions"] = actions

    text = yaml.safe_dump(out, sort_keys=False, default_flow_style=False, allow_unicode=False, width=100)
    try:
        from engine.compiler import CompiledOntology, OntologyError
        CompiledOntology.from_text(text)
    except Exception as e:  # noqa: BLE001 - OntologyError or anything the compiler raises
        raise ProposalError(f"generated ontology does not compile: {e}") from e
    return text, _mapping(proposal, prof, ix)


def _declared_action(a: ActionP, types: dict[str, ObjectTypeP], proposal: Proposal, exec_roles: list[str]) -> dict:
    t = types[a.type]
    props = {p.name: p for p in t.properties}
    d: dict[str, Any] = {"description": a.description or f"{a.kind} on {a.type}"}
    base = {"approval": {"approvers": ["admin"]}, "roles": list(exec_roles)}
    if a.kind == "update_props":
        params = {"id": f"uuid({a.type})!"}
        sets = {}
        for x in a.params["props"]:
            params[x] = _param_type(props[x]) + "!"
            sets[x] = f"${x}"
        if "id" in props:  # property named id cannot exist (reserved) but keep the guard explicit
            raise ProposalError("property named 'id' is not allowed")
        d["params"] = params
        d.update(base)
        d["bind"] = {"obj": {"type": a.type, "id": "$id"}}
        d["rules"] = [{"modify": {"type": a.type, "id": "$id", "set": sets}}]
    elif a.kind == "set_status":
        d["params"] = {"id": f"uuid({a.type})!"}
        d.update(base)
        d["bind"] = {"obj": {"type": a.type, "id": "$id"}}
        frm = a.params.get("from")
        if frm:
            lst = ", ".join("'" + v.replace("'", "") + "'" for v in frm) if all("'" not in v for v in frm) else None
            if lst is None:
                raise ProposalError(f"action {a.name}: quotes are not allowed in from-values")
            d["criteria"] = [{"expr": f"obj.{a.params['prop']} in ({lst})",
                              "message": f"{a.type} is not in a state that allows {a.name}"}]
        d["rules"] = [{"modify": {"type": a.type, "id": "$id", "set": {a.params["prop"]: a.params["value"]}}}]
    else:
        ln = next(l for l in proposal.linkTypes if l.name == a.params["link"])
        d["params"] = {"src": f"uuid({ln.from_})!", "dst": f"uuid({ln.to})!"}
        d.update(base)
        d["rules"] = [{a.kind: {"link": ln.name, "src": "$src", "dst": "$dst"}}]
    return d


def _mapping(proposal: Proposal, prof: dict, ix: dict) -> dict:
    cols, tables = ix["cols"], ix["tables"]
    types: dict[str, Any] = {}
    for o in sorted(proposal.objectTypes, key=lambda o: o.name):
        types[o.name] = {
            "label": o.label, "table": o.source_table, "table_name": tables[o.source_table].get("name", ""),
            "key_cols": list(o.key),
            "columns": {p.name: {"col": p.column, "transform": p.transform, "type": _ptype(p),
                                 "required": p.required, "label": p.label}
                        for p in sorted(o.properties, key=lambda p: p.name)}}
    links: dict[str, Any] = {}
    tmap = {o.name: o for o in proposal.objectTypes}
    for l in sorted(proposal.linkTypes, key=lambda l: l.name):
        s = l.source
        if s.kind == "fk":
            links[l.name] = {"kind": "fk", "from_type": l.from_, "to_type": l.to, "from_col": s.from_col,
                             "to_type_key": s.to_type_key, "from_key_cols": list(tmap[l.from_].key),
                             "table": tmap[l.from_].source_table}
        else:
            links[l.name] = {"kind": "join_table", "from_type": l.from_, "to_type": l.to, "table": s.table,
                             "src_col": s.src_col, "dst_col": s.dst_col,
                             "src_key": tmap[l.from_].key[0], "dst_key": tmap[l.to].key[0]}
    return {"version": 1, "profile_hash": proposal.profile_hash, "source": prof.get("source"),
            "types": types, "links": links}


# ------------------------------------------------------------------ diff
EMPTY_YAML = "version: 1\nnamespace: https://isnad.local/ws/empty#\nobjectTypes: {}\n"


def proposal_diff(old_yaml: str | None, new_yaml: str) -> dict:
    """Thin wrapper over ``engine.versioning.diff.diff``. ``old_yaml=None`` means a first version: everything is
    added, and each new role is listed in ``sensitive`` (it defines who may see and do what)."""
    from engine.versioning.diff import diff, parse
    if old_yaml is not None:
        return diff(old_yaml, new_yaml)
    m = parse(new_yaml)
    stub = f"version: 1\nnamespace: {m.namespace}\nobjectTypes: {{}}\n"
    d = diff(stub, new_yaml)
    for rn in d["added"].get("roles", []):
        r = m.roles[rn]
        d["sensitive"].append(f"role {rn}: new role ({'admin' if r.all_actions else 'write' if any(a.write for a in r.types.values()) else 'read'} access)")
    return d


def proposal_hash(proposal: Proposal) -> str:
    return hashlib.sha256(json.dumps(proposal.model_dump(by_alias=True, mode="json"), sort_keys=True).encode()).hexdigest()


__all__ = ["Proposal", "ProposalError", "to_yaml", "proposal_diff", "map_profile_type", "derive_namespace",
           "ObjectTypeP", "PropertyP", "LinkTypeP", "ClassP", "ActionP", "RoleP", "EnumType", "FkSource",
           "JoinSource", "proposal_hash"]
