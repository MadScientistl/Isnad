"""Parsed ontology model + validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .exprs import ExprError, parse_expr, parse_filter, validate_expr, validate_filter, params_used

import yaml
import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "ontology" / "schema" / "ontology.schema.json"
CARDINALITIES = ("many-to-many", "many-to-one", "one-to-many", "one-to-one")


class OntologyError(ValueError):
    """Raised for an invalid ontology; message lists all problems."""


@dataclass(frozen=True)
class Property:
    name: str
    type: str  # string|int|float|bool|timestamp|enum
    required: bool = False
    enum: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObjectType:
    name: str
    properties: tuple[Property, ...] = ()

    @property
    def snake(self) -> str:
        return snake_case(self.name)


@dataclass(frozen=True)
class LinkType:
    name: str
    src: str
    dst: str
    cardinality: str = "many-to-many"
    required: bool = False
    transitive: bool = False
    inverse: str | None = None


@dataclass(frozen=True)
class ClassDef:
    name: str
    extends: str | None = None


@dataclass(frozen=True)
class ParamSpec:
    name: str
    base: str  # string|int|float|bool|timestamp|point|linestring|uuid
    ref: str | None  # object type for uuid(Type)
    required: bool


_PARAM_RE = re.compile(r"^(string|int|float|bool|timestamp|point|linestring|uuid)(?:\(([A-Z][A-Za-z0-9]*)\))?(!?)$")


def parse_param_type(spec: str) -> ParamSpec | None:
    """Parse "uuid(Invoice)!" -> ParamSpec (name left empty); None if malformed."""
    m = _PARAM_RE.match(spec)
    if not m or (m.group(2) and m.group(1) != "uuid"):
        return None
    return ParamSpec("", m.group(1), m.group(2), m.group(3) == "!")


@dataclass(frozen=True)
class BindDef:
    alias: str
    type: str
    id: str  # "$param" reference


@dataclass(frozen=True)
class Criterion:
    expr: str
    message: str
    ast: object = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Rule:
    kind: str  # modify|create|delete|link|unlink
    type: str | None = None
    id: object = None  # "$ref" string or None
    set: dict = field(default_factory=dict, hash=False)
    as_name: str | None = None
    link: str | None = None
    src: object = None
    dst: object = None


@dataclass(frozen=True)
class ActionDef:
    name: str
    params: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    writes: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    approval_required: bool = False
    approvers: tuple[str, ...] = ()
    approval_exempt: tuple[str, ...] = ()  # roles (e.g. sim) that execute directly even when approval is required
    bind: dict[str, BindDef] = field(default_factory=dict, hash=False)
    criteria: tuple[Criterion, ...] = ()
    rules: tuple[Rule, ...] = ()
    function: str | None = None
    webhooks: tuple[str, ...] = ()
    rollback: str = "compensate"  # compensate | irreversible

    def param_specs(self) -> list[ParamSpec]:
        out = []
        for n, t in self.params.items():
            ps = parse_param_type(t)
            if ps is not None:
                out.append(ParamSpec(n, ps.base, ps.ref, ps.required))
        return out

    @property
    def requires_approval(self) -> bool:
        return self.approval_required


@dataclass(frozen=True)
class TypeAccess:
    read: bool = False
    write: bool = False
    where: str | None = None
    mask: tuple[str, ...] = ()
    where_ast: object = field(default=None, compare=False, repr=False)


AGENT_TOOLS = ("query_objects", "traverse", "find_paths", "impact_analysis", "get_history", "as_of", "diff",
               "run_function", "propose_action", "scenario_open", "scenario_apply", "scenario_simulate",
               "scenario_compare")
GENERIC_ACTIONS = ("create_object", "update_object", "delete_object", "create_link", "end_link")


@dataclass(frozen=True)
class AgentDef:
    allowlist: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    instructions: str = ""
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class FunctionDef:
    name: str
    description: str = ""
    params: dict[str, str] = field(default_factory=dict, hash=False)
    roles: tuple[str, ...] = ()

    def param_specs(self) -> list[ParamSpec]:
        out = []
        for n, t in self.params.items():
            ps = parse_param_type(t)
            if ps is not None:
                out.append(ParamSpec(n, ps.base, ps.ref, ps.required))
        return out


@dataclass(frozen=True)
class RoleDef:
    name: str
    description: str | None = None
    types: dict[str, TypeAccess] = field(default_factory=dict, hash=False)  # absent type = no access
    links: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    approves: tuple[str, ...] = ()
    region: str | None = None
    all_actions: bool = False  # `actions: all` shorthand (still limited to actions that list this role)
    all_approves: bool = False
    agent: AgentDef | None = None

    def can_read(self, type_name: str) -> bool:
        a = self.types.get(type_name)
        return bool(a and a.read)

    def can_write(self, type_name: str) -> bool:
        a = self.types.get(type_name)
        return bool(a and a.write)


@dataclass(frozen=True)
class WebhookDef:
    name: str
    url_env: str
    method: str = "POST"
    max_attempts: int = 5
    description: str | None = None


GEO_TYPES = ("point", "linestring")
PARAM_TYPES = ("string", "int", "float", "bool", "timestamp", "point", "linestring", "uuid")


@dataclass
class Ontology:
    version: int
    namespace: str
    classes: dict[str, ClassDef] = field(default_factory=dict)
    object_types: dict[str, ObjectType] = field(default_factory=dict)
    link_types: dict[str, LinkType] = field(default_factory=dict)
    actions: dict[str, ActionDef] = field(default_factory=dict)
    roles: dict[str, RoleDef] = field(default_factory=dict)
    webhooks: dict[str, WebhookDef] = field(default_factory=dict)
    functions: dict[str, FunctionDef] = field(default_factory=dict)

    def agent_allowlist(self, role: str) -> tuple[str, ...]:
        """Actions the role's agent may propose (empty if the role has no agent block)."""
        r = self.roles.get(role)
        return r.agent.allowlist if r and r.agent else ()

    def agent_tools(self, role: str) -> tuple[str, ...]:
        r = self.roles.get(role)
        return r.agent.tools if r and r.agent else ()

    def role_can_run_function(self, role: str, name: str) -> bool:
        f = self.functions.get(name)
        return bool(f and role in f.roles)

    def role_can_execute(self, role: str, action: str) -> bool:
        """Role may execute (or, for approval-gated actions, propose) the action: listed on both sides."""
        r, a = self.roles.get(role), self.actions.get(action)
        return bool(r and a and role in a.roles and action in r.actions)

    def role_can_approve(self, role: str, action: str) -> bool:
        r, a = self.roles.get(role), self.actions.get(action)
        return bool(r and a and role in a.approvers and action in r.approves)

    def parent_of(self, name: str) -> str | None:
        c = self.classes.get(name)
        return c.extends if c else None


def snake_case(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.lower()


def _parse_prop(name: str, spec) -> Property:
    if isinstance(spec, str):
        req = spec.endswith("!")
        return Property(name, spec.rstrip("!"), req)
    return Property(name, "enum", bool(spec.get("required", False)), tuple(spec["enum"]))


_REF_RE = re.compile(r"^\$[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BASE_TO_PROP = {"string": ("string", "enum"), "int": ("int", "float"), "float": ("float",), "bool": ("bool",),
                 "timestamp": ("timestamp", "string"), "point": ("point",), "linestring": ("linestring",)}


def _type_compatible(onto: "Ontology", declared: str, expected: str) -> bool:
    """uuid(declared) used where an object of `expected` (type or class) is needed."""
    def chain(n):
        out, seen = [], set()
        while n and n not in seen:
            seen.add(n)
            out.append(n)
            n = onto.parent_of(n)
        return out
    return expected in chain(declared) or declared in chain(expected)


def _parse_actions(raw: dict, onto: "Ontology", errors: list[str]) -> None:
    have_roles = bool(raw.get("roles"))
    for n, a in (raw.get("actions") or {}).items():
        a = a or {}
        where = f"action '{n}'"
        params = dict(a.get("params") or {})
        pspecs: dict[str, ParamSpec] = {}
        for pn, pt in params.items():
            ps = parse_param_type(pt)
            if ps is None:
                errors.append(f"{where}: param '{pn}' has unknown type '{pt}'")
                continue
            if ps.ref and ps.ref not in onto.object_types and ps.ref not in onto.classes:
                errors.append(f"{where}: param '{pn}' references unknown type '{ps.ref}'")
            pspecs[pn] = ParamSpec(pn, ps.base, ps.ref, ps.required)
        ptypes = {pn: pt for pn, pt in params.items()}

        ap = a.get("approval") or {}
        approval_required = bool(ap.get("required", False))
        approvers = tuple(ap.get("approvers") or ())
        exempt = tuple(ap.get("exempt") or ())
        if approval_required and not approvers:
            errors.append(f"{where}: approval.required needs at least one approver")
        # approvers without `required` = who may approve PROPOSALS (e.g. agent proposals) of an action that
        # roles may also execute directly

        bind: dict[str, BindDef] = {}
        for al, b in (a.get("bind") or {}).items():
            if al in ("new", "now", "actor"):
                errors.append(f"{where}: bind alias '{al}' is reserved")
            t, i = b["type"], b["id"]
            if t not in onto.object_types:
                errors.append(f"{where}: bind '{al}' has unknown type '{t}'")
            if not _REF_RE.match(str(i)):
                errors.append(f"{where}: bind '{al}' id must be a $reference, got {i!r}")
            else:
                pn = i[1:]
                if pn.split(".")[0] not in pspecs and not pn.startswith(("new.", "now")):
                    errors.append(f"{where}: bind '{al}' references unknown parameter {i}")
                else:
                    ps = pspecs.get(pn)
                    if ps is not None:
                        if ps.base != "uuid":
                            errors.append(f"{where}: bind '{al}' id {i} is not a uuid parameter")
                        elif ps.ref and t in onto.object_types and not _type_compatible(onto, ps.ref, t):
                            errors.append(f"{where}: bind '{al}' type {t} does not match parameter {i} uuid({ps.ref})")
            bind[al] = BindDef(al, t, i)
        aliases = {al: b.type for al, b in bind.items() if b.type in onto.object_types}

        criteria = []
        for k, c in enumerate(a.get("criteria") or ()):
            try:
                ast = parse_expr(c["expr"])
            except ExprError as e:
                errors.append(f"{where}: criteria[{k}]: {e}")
                ast = None
            else:
                for m in validate_expr(ast, onto, params=ptypes, aliases=aliases):
                    errors.append(f"{where}: criteria[{k}]: {m}")
                if any(x == "new" or x.startswith("new.") for x in params_used(ast)):
                    errors.append(f"{where}: criteria[{k}]: $new is not available in criteria")
            criteria.append(Criterion(c["expr"], c.get("message") or f"criteria failed: {c['expr']}", ast))

        rules, new_names = [], set()
        for k, r in enumerate(a.get("rules") or ()):
            (kind, body), = r.items()
            rw = f"{where}: rules[{k}] ({kind})"
            body = body or {}
            rule = Rule(kind, body.get("type"), body.get("id"), dict(body.get("set") or {}), body.get("as"),
                        body.get("link"), body.get("src"), body.get("dst"))
            _check_rule(onto, rule, rw, pspecs, aliases, new_names, errors)
            if kind == "create" and rule.as_name:
                new_names.add(rule.as_name)
            rules.append(rule)

        func = a.get("function")
        if func and rules:
            errors.append(f"{where}: give either rules or function, not both")
        if have_roles and not func and not rules:
            errors.append(f"{where}: needs rules or function")
        rb = a.get("rollback", "compensate")
        onto.actions[n] = ActionDef(
            n, params, a.get("description"), tuple(a.get("writes") or ()), tuple(a.get("roles") or ()),
            approval_required, approvers, exempt, bind, tuple(criteria), tuple(rules), func,
            tuple(a.get("webhooks") or ()), rb)
        if have_roles and not a.get("roles"):
            errors.append(f"{where}: no roles may execute it (add roles: [...])")


def _check_rule(onto, rule: Rule, rw: str, pspecs, aliases, new_names, errors) -> None:
    def ref_ok(v, what, expect_type=None):
        if not (isinstance(v, str) and _REF_RE.match(v)):
            errors.append(f"{rw}: {what} must be a $reference, got {v!r}")
            return
        name = v[1:]
        head, _, rest = name.partition(".")
        if head == "new":
            if rest not in new_names:
                errors.append(f"{rw}: {what} {v} refers to no earlier create rule with as: {rest or '?'}")
        elif rest:
            if head not in aliases:
                errors.append(f"{rw}: {what} {v}: unknown alias '{head}'")
        elif name in ("now", "actor"):
            pass
        elif name not in pspecs:
            errors.append(f"{rw}: {what} {v} is not a parameter")
        else:
            ps = pspecs[name]
            if ps.base != "uuid":
                errors.append(f"{rw}: {what} {v} is not a uuid parameter")
            elif ps.ref and expect_type and not _type_compatible(onto, ps.ref, expect_type):
                errors.append(f"{rw}: {what} {v} is uuid({ps.ref}), expected {expect_type}")

    need = {"modify": ("type", "id", "set"), "create": ("type", "set"), "delete": ("type", "id"),
            "link": ("link", "src", "dst"), "unlink": ("link", "src", "dst")}[rule.kind]
    for f in need:
        v = {"set": rule.set or None}.get(f, getattr(rule, f))
        if v is None and not (f == "set" and rule.kind == "create"):
            errors.append(f"{rw}: missing '{f}'")
    if rule.kind in ("link", "unlink"):
        ln = onto.link_types.get(rule.link)
        if rule.link and ln is None:
            errors.append(f"{rw}: unknown link '{rule.link}'")
        for f, tt in (("src", ln.src if ln else None), ("dst", ln.dst if ln else None)):
            ref_ok(getattr(rule, f), f, None)
        return
    t = onto.object_types.get(rule.type)
    if rule.type and t is None:
        errors.append(f"{rw}: unknown type '{rule.type}'")
        return
    if rule.kind in ("modify", "delete"):
        ref_ok(rule.id, "id", rule.type)
    if rule.kind == "create":
        if rule.id is not None:
            ref_ok(rule.id, "id")
        if rule.as_name and (not _NAME_RE.match(rule.as_name) or rule.as_name in new_names):
            errors.append(f"{rw}: 'as' name {rule.as_name!r} is invalid or duplicated")
    if t is None or rule.kind not in ("modify", "create"):
        return
    props = {p.name: p for p in t.properties}
    for pn, v in rule.set.items():
        p = props.get(pn)
        if p is None:
            errors.append(f"{rw}: unknown property '{pn}' on {rule.type}")
            continue
        if isinstance(v, str) and v.startswith("$"):
            if not _REF_RE.match(v):
                errors.append(f"{rw}: bad reference {v!r} for '{pn}'")
                continue
            name = v[1:]
            head, _, rest = name.partition(".")
            if name == "now":
                if p.type not in ("timestamp", "string"):
                    errors.append(f"{rw}: $now cannot be assigned to {p.type} property '{pn}'")
            elif name == "actor":
                if p.type not in ("string", "enum"):
                    errors.append(f"{rw}: $actor cannot be assigned to {p.type} property '{pn}'")
            elif head == "new":
                if rest not in new_names:
                    errors.append(f"{rw}: {v} refers to no earlier create rule")
            elif rest:
                if head not in aliases:
                    errors.append(f"{rw}: {v}: unknown alias '{head}'")
                elif rest != "id" and rest not in {x.name for x in onto.object_types[aliases[head]].properties}:
                    errors.append(f"{rw}: {v}: unknown property '{rest}'")
            elif name not in pspecs:
                errors.append(f"{rw}: {v} is not a parameter")
            else:
                ps = pspecs[name]
                if p.type == "enum":
                    ok = ps.base == "string"
                else:
                    ok = p.type in _BASE_TO_PROP.get(ps.base, ())
                if not ok:
                    errors.append(f"{rw}: parameter {v} ({ps.base}) cannot be assigned to {p.type} property '{pn}'")
        elif v is None:
            if p.required:
                errors.append(f"{rw}: required property '{pn}' cannot be set to null")
        else:
            from .exprs import Lit, _lit_compat
            bad = _lit_compat(p, Lit(v))
            if bad:
                errors.append(f"{rw}: {bad}")
    if rule.kind == "create":
        for p in t.properties:
            if p.required and p.name not in rule.set:
                errors.append(f"{rw}: required property '{p.name}' of {rule.type} not set")


def _expand(v, universe):
    if v == "all":
        return tuple(sorted(universe))
    return tuple(v or ())


def _parse_roles(raw: dict, onto: "Ontology", errors: list[str]) -> None:
    for rn, r in (raw.get("roles") or {}).items():
        r = r or {}
        rw = f"role '{rn}'"
        types: dict[str, TypeAccess] = {}
        traw = r.get("types") or {}
        if traw == "all":
            traw = {t: {"read": True, "write": True} for t in onto.object_types}
        for tn, spec in traw.items():
            spec = spec or {}
            if tn not in onto.object_types:
                errors.append(f"{rw}: types: unknown object type '{tn}'")
                continue
            read, write = bool(spec.get("read", False)), bool(spec.get("write", False))
            if write and not read:
                errors.append(f"{rw}: {tn}: write requires read")
            props = {p.name for p in onto.object_types[tn].properties}
            mask = tuple(spec.get("mask") or ())
            for m in mask:
                if m not in props:
                    errors.append(f"{rw}: {tn}: mask references unknown property '{m}'")
            if mask and not read:
                errors.append(f"{rw}: {tn}: mask without read access")
            where, ast = spec.get("where"), None
            if where is not None:
                try:
                    ast = parse_filter(where)
                except ExprError as e:
                    errors.append(f"{rw}: {tn}: where: {e}")
                else:
                    for m in validate_filter(ast, onto, tn):
                        errors.append(f"{rw}: {tn}: where: {m}")
                    if "region" in params_used(ast) and not r.get("region"):
                        errors.append(f"{rw}: {tn}: where uses $region but the role has no region")
                    if not read:
                        errors.append(f"{rw}: {tn}: where without read access")
            types[tn] = TypeAccess(read, write, where, mask, ast)
        links = _expand(r.get("links"), onto.link_types)
        for l in links:
            if l not in onto.link_types:
                errors.append(f"{rw}: links: unknown link '{l}'")
        acts = _expand(r.get("actions"), onto.actions)
        for a in acts:
            if a not in onto.actions:
                errors.append(f"{rw}: actions: unknown action '{a}'")
        appr = _expand(r.get("approves"), onto.actions)
        for a in appr:
            if a not in onto.actions:
                errors.append(f"{rw}: approves: unknown action '{a}'")
        agent = None
        if r.get("agent") is not None:
            ag = r["agent"]
            allow = tuple(ag.get("allowlist") or ())
            tools = tuple(ag.get("tools") or ())
            for t in tools:
                if t not in AGENT_TOOLS:
                    errors.append(f"{rw}: agent.tools: unknown tool '{t}' (valid: {', '.join(AGENT_TOOLS)})")
            for a in allow:
                if a in GENERIC_ACTIONS:
                    errors.append(f"{rw}: agent.allowlist: generic action '{a}' is not allowed")
                elif a not in onto.actions:
                    errors.append(f"{rw}: agent.allowlist: unknown action '{a}'")
                elif a not in acts:
                    errors.append(f"{rw}: agent.allowlist: action '{a}' is not in the role's actions")
            if allow and "propose_action" not in tools:
                errors.append(f"{rw}: agent.allowlist is set but 'propose_action' is not in agent.tools")
            agent = AgentDef(allow, tools, ag.get("instructions") or "", ag.get("max_tool_calls"),
                             ag.get("max_cost_usd"))
        onto.roles[rn] = RoleDef(rn, r.get("description"), types, links, acts, appr, r.get("region"),
                                 r.get("actions") == "all", r.get("approves") == "all", agent)


def _parse_functions(raw: dict, onto: "Ontology", errors: list[str]) -> None:
    from engine.functions import FUNCTIONS  # registry of Python implementations
    for n, f in (raw.get("functions") or {}).items():
        f = f or {}
        where = f"function '{n}'"
        if n not in FUNCTIONS:
            errors.append(f"{where}: no implementation registered in engine.functions.FUNCTIONS")
        params = dict(f.get("params") or {})
        for pn, pt in params.items():
            ps = parse_param_type(pt)
            if ps is None:
                errors.append(f"{where}: param '{pn}' has unknown type '{pt}'")
            elif ps.ref and ps.ref not in onto.object_types and ps.ref not in onto.classes:
                errors.append(f"{where}: param '{pn}' references unknown type '{ps.ref}'")
        roles = tuple(f.get("roles") or ())
        for r in roles:
            if r not in onto.roles:
                errors.append(f"{where}: unknown role '{r}'")
        onto.functions[n] = FunctionDef(n, f.get("description") or "", params, roles)


def _parse_webhooks(raw: dict, onto: "Ontology", errors: list[str]) -> None:
    for n, w in (raw.get("webhooks") or {}).items():
        onto.webhooks[n] = WebhookDef(n, w["url_env"], w.get("method", "POST"), int(w.get("max_attempts", 5)),
                                      w.get("description"))


def _cross_check(onto: "Ontology", errors: list[str]) -> None:
    for a in onto.actions.values():
        for r in a.roles:
            if r not in onto.roles:
                errors.append(f"action '{a.name}': unknown role '{r}'")
            elif a.name not in onto.roles[r].actions:
                errors.append(f"action '{a.name}' lists role '{r}' but role '{r}' does not list the action")
        for r in a.approvers:
            if r not in onto.roles:
                errors.append(f"action '{a.name}': unknown approver role '{r}'")
            elif a.name not in onto.roles[r].approves:
                errors.append(f"action '{a.name}' lists approver '{r}' but role '{r}' does not approve it")
        for r in a.approval_exempt:
            if r not in onto.roles:
                errors.append(f"action '{a.name}': unknown role '{r}' in approval.exempt")
        for w in a.webhooks:
            if w not in onto.webhooks:
                errors.append(f"action '{a.name}': unknown webhook '{w}'")
    for r in onto.roles.values():
        for a in r.actions:
            act = onto.actions.get(a)
            if act is not None and r.name not in act.roles and not r.all_actions:
                errors.append(f"role '{r.name}' lists action '{a}' but the action does not list the role")
        for a in r.approves:
            act = onto.actions.get(a)
            if act is not None and r.name not in act.approvers and not r.all_approves:
                errors.append(f"role '{r.name}' approves '{a}' but the action does not list it as approver")


def parse_ontology(raw: dict) -> Ontology:
    schema = json.loads(SCHEMA_PATH.read_text())
    v = jsonschema.Draft202012Validator(schema)
    errs = sorted(v.iter_errors(raw), key=lambda e: list(e.path))
    if errs:
        msgs = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errs]
        raise OntologyError("invalid ontology schema:\n  " + "\n  ".join(msgs))

    onto = Ontology(raw["version"], raw["namespace"])
    for n, c in (raw.get("classes") or {}).items():
        onto.classes[n] = ClassDef(n, (c or {}).get("extends"))
    for n, o in raw["objectTypes"].items():
        props = tuple(_parse_prop(p, s) for p, s in ((o or {}).get("properties") or {}).items())
        onto.object_types[n] = ObjectType(n, props)
    for n, l in (raw.get("linkTypes") or {}).items():
        onto.link_types[n] = LinkType(
            n, l["from"], l["to"], l.get("cardinality", "many-to-many"),
            bool(l.get("required", False)), bool(l.get("transitive", False)), l.get("inverse"),
        )

    errors: list[str] = []
    _parse_actions(raw, onto, errors)
    _parse_roles(raw, onto, errors)
    _parse_webhooks(raw, onto, errors)
    _parse_functions(raw, onto, errors)
    _cross_check(onto, errors)
    for c in onto.classes.values():
        if c.extends and c.extends not in onto.classes:
            errors.append(f"class '{c.name}' extends unknown class '{c.extends}'")
    for c in onto.classes:  # cycles
        seen, cur = set(), c
        while cur:
            if cur in seen:
                errors.append(f"class hierarchy cycle involving '{c}'")
                break
            seen.add(cur)
            cur = onto.parent_of(cur)
    for l in onto.link_types.values():
        for end, t in (("from", l.src), ("to", l.dst)):
            if t not in onto.object_types and t not in onto.classes:
                errors.append(f"link '{l.name}': '{end}' references unknown type '{t}'")
        if l.transitive and not (l.src == l.dst):
            errors.append(f"link '{l.name}': transitive requires from == to")
        if l.inverse:
            if l.inverse in onto.link_types:
                errors.append(f"link '{l.name}': inverse name '{l.inverse}' collides with a link type")
            if l.inverse == l.name:
                errors.append(f"link '{l.name}': inverse cannot equal itself")
    inv_names = [l.inverse for l in onto.link_types.values() if l.inverse]
    for i in set(inv_names):
        if inv_names.count(i) > 1:
            errors.append(f"inverse name '{i}' used by multiple links")
    if errors:
        raise OntologyError("invalid ontology:\n  " + "\n  ".join(errors))
    return onto


def load_ontology(path) -> tuple[Ontology, str]:
    text = Path(path).read_text()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise OntologyError(f"YAML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise OntologyError("ontology root must be a mapping")
    return parse_ontology(raw), text
