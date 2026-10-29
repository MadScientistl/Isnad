"""Restricted expression languages (no eval): action criteria and row filters.

Two entry points share one parser and one small AST:

* `parse_expr(text)`   action expressions: `$param`, `$now`, `$actor`, `$new.x`, `alias.prop`, literals,
                       `== != < <= > >= in`, `and or not`, `is [not] null`, `+ - * /`.
* `parse_filter(text)` row filters: bare `prop` identifiers, `= != < <= > >=` against a literal or
                       `$region`, `prop in (..)`, `is [not] null`, `and or not`, parentheses and
                       `exists(<link>, <Type>: <expr>)`.

Nothing here executes text. The AST is evaluated by `evaluate` (criteria) or rendered by
`filter_to_sql` (row filters). `validate_expr` / `validate_filter` return lists of problems.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


class ExprError(ValueError):
    """Syntax or validation error in an expression."""


# ---------------------------------------------------------------- AST
class Node:
    __slots__ = ()


@dataclass(frozen=True)
class Lit(Node):
    value: Any  # str | int | float | bool | None


@dataclass(frozen=True)
class Param(Node):
    name: str  # without '$'; may be dotted ("new.event", "inv.id")


@dataclass(frozen=True)
class Ref(Node):
    alias: str | None  # None for bare filter identifiers
    prop: str


@dataclass(frozen=True)
class Bin(Node):
    op: str  # == != < <= > >= + - * / and or
    left: Node
    right: Node


@dataclass(frozen=True)
class Not(Node):
    arg: Node


@dataclass(frozen=True)
class Neg(Node):
    arg: Node


@dataclass(frozen=True)
class In(Node):
    arg: Node
    items: tuple[Node, ...]


@dataclass(frozen=True)
class IsNull(Node):
    arg: Node
    negated: bool = False


@dataclass(frozen=True)
class Exists(Node):
    link: str
    type: str
    cond: Node


# ---------------------------------------------------------------- lexer
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+\.\d+|\d+)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<param>\$[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|<=|>=|<>|[<>=+\-*/(),:.\[\]])
""", re.X)
_KEYWORDS = {"and", "or", "not", "in", "is", "null", "true", "false", "exists"}


def _lex(text: str) -> list[tuple[str, Any, int]]:
    toks, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ExprError(f"unexpected character {text[pos]!r} at {pos} in {text!r}")
        kind = m.lastgroup
        v = m.group()
        if kind == "num":
            toks.append(("lit", float(v) if "." in v else int(v), pos))
        elif kind == "str":
            q = v[0]
            toks.append(("lit", v[1:-1].replace(q + q, q), pos))
        elif kind == "param":
            toks.append(("param", v[1:], pos))
        elif kind == "ident":
            low = v.lower()
            if low in _KEYWORDS:
                toks.append(("kw", low, pos))
            else:
                toks.append(("ident", v, pos))
        elif kind == "op":
            toks.append(("op", "!=" if v == "<>" else v, pos))
        pos = m.end()
    toks.append(("end", None, len(text)))
    return toks


# ---------------------------------------------------------------- parser
class _Parser:
    def __init__(self, text: str, filter_mode: bool):
        self.text, self.toks, self.i, self.filter = text, _lex(text), 0, filter_mode

    def err(self, msg: str):
        raise ExprError(f"{msg} at {self.toks[self.i][2]} in {self.text!r}")

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def is_op(self, *ops) -> bool:
        t = self.peek()
        return t[0] == "op" and t[1] in ops

    def is_kw(self, *kws) -> bool:
        t = self.peek()
        return t[0] == "kw" and t[1] in kws

    def expect_op(self, op):
        if not self.is_op(op):
            self.err(f"expected {op!r}")
        self.next()

    def parse(self) -> Node:
        n = self.or_()
        if self.peek()[0] != "end":
            self.err("unexpected trailing input")
        return n

    def or_(self):
        n = self.and_()
        while self.is_kw("or"):
            self.next()
            n = Bin("or", n, self.and_())
        return n

    def and_(self):
        n = self.not_()
        while self.is_kw("and"):
            self.next()
            n = Bin("and", n, self.not_())
        return n

    def not_(self):
        if self.is_kw("not"):
            self.next()
            return Not(self.not_())
        return self.cmp()

    def cmp(self):
        n = self.add()
        if self.is_op("==", "=", "!=", "<", "<=", ">", ">="):
            op = self.next()[1]
            n = Bin("==" if op == "=" else op, n, self.add())
        elif self.is_kw("in"):
            self.next()
            n = In(n, self.list_())
        elif self.is_kw("is"):
            self.next()
            neg = False
            if self.is_kw("not"):
                self.next()
                neg = True
            if not self.is_kw("null"):
                self.err("expected 'null' after 'is'")
            self.next()
            n = IsNull(n, neg)
        elif self.is_kw("not") and self.toks[self.i + 1][:2] == ("kw", "in"):
            self.next()
            self.next()
            n = Not(In(n, self.list_()))
        return n

    def list_(self):
        close = ")" if self.is_op("(") else "]" if self.is_op("[") else None
        if close is None:
            self.err("expected '(' or '[' after 'in'")
        self.next()
        items = []
        if not self.is_op(close):
            items.append(self.add())
            while self.is_op(","):
                self.next()
                items.append(self.add())
        self.expect_op(close)
        if not items:
            self.err("empty 'in' list")
        return tuple(items)

    def add(self):
        n = self.mul()
        while self.is_op("+", "-"):
            n = Bin(self.next()[1], n, self.mul())
        return n

    def mul(self):
        n = self.unary()
        while self.is_op("*", "/"):
            n = Bin(self.next()[1], n, self.unary())
        return n

    def unary(self):
        if self.is_op("-"):
            self.next()
            a = self.unary()
            return Lit(-a.value) if isinstance(a, Lit) and isinstance(a.value, (int, float)) and not isinstance(a.value, bool) else Neg(a)
        return self.primary()

    def primary(self):
        k, v, _ = self.peek()
        if k == "lit":
            self.next()
            return Lit(v)
        if k == "param":
            self.next()
            return Param(v)
        if k == "kw":
            if v in ("true", "false"):
                self.next()
                return Lit(v == "true")
            if v == "null":
                self.next()
                return Lit(None)
            if v == "exists":
                if not self.filter:
                    self.err("exists(...) is only allowed in row filters")
                return self.exists()
            self.err(f"unexpected keyword {v!r}")
        if k == "ident":
            self.next()
            if self.is_op("."):
                if self.filter:
                    self.err("qualified names are not allowed in row filters")
                self.next()
                t = self.next()
                if t[0] != "ident":
                    self.err("expected property name after '.'")
                return Ref(v, t[1])
            if not self.filter:
                self.err(f"bare identifier {v!r}: use alias.prop")
            return Ref(None, v)
        if k == "op" and v == "(":
            self.next()
            n = self.or_()
            self.expect_op(")")
            return n
        self.err("unexpected token")

    def exists(self):
        self.next()  # exists
        self.expect_op("(")
        t = self.next()
        if t[0] != "ident":
            self.err("exists: expected link name")
        self.expect_op(",")
        ty = self.next()
        if ty[0] != "ident":
            self.err("exists: expected type name")
        self.expect_op(":")
        cond = self.or_()
        self.expect_op(")")
        return Exists(t[1], ty[1], cond)


def parse_expr(text: str) -> Node:
    """Parse an action/criteria expression. Raises ExprError."""
    if not isinstance(text, str) or not text.strip():
        raise ExprError("empty expression")
    return _Parser(text, False).parse()


def parse_filter(text: str) -> Node:
    """Parse a row-filter expression. Raises ExprError (structure is checked by validate_filter)."""
    if not isinstance(text, str) or not text.strip():
        raise ExprError("empty filter")
    return _Parser(text, True).parse()


def walk(node: Node):
    yield node
    if isinstance(node, Bin):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, (Not, Neg)):
        yield from walk(node.arg)
    elif isinstance(node, In):
        yield from walk(node.arg)
        for i in node.items:
            yield from walk(i)
    elif isinstance(node, IsNull):
        yield from walk(node.arg)
    elif isinstance(node, Exists):
        yield from walk(node.cond)


def params_used(node: Node) -> set[str]:
    return {n.name for n in walk(node) if isinstance(n, Param)}


# ---------------------------------------------------------------- evaluation (criteria)
def evaluate(node: Node, *, params: Mapping[str, Any] | None = None, aliases: Mapping[str, Mapping[str, Any] | None] | None = None,
             now: Any = None, actor: Any = None, new: Mapping[str, Any] | None = None) -> Any:
    """Evaluate an action expression. `aliases[alias]` is the bound object's property mapping (incl. `id`).

    SQL-lite null semantics: ordering comparisons with None are False; `==`/`!=` use plain equality;
    arithmetic with None gives None.
    """
    params, aliases, new = params or {}, aliases or {}, new or {}

    def ev(n):
        if isinstance(n, Lit):
            return n.value
        if isinstance(n, Param):
            head, _, rest = n.name.partition(".")
            if n.name == "now":
                return now
            if n.name == "actor":
                return actor
            if head == "new" and rest:
                return new.get(rest)
            if rest and head in aliases:
                obj = aliases[head]
                return None if obj is None else obj.get(rest)
            if rest:
                raise ExprError(f"unknown reference ${n.name}")
            if n.name not in params:
                return None
            return params[n.name]
        if isinstance(n, Ref):
            if n.alias not in aliases:
                raise ExprError(f"unknown alias {n.alias!r}")
            obj = aliases[n.alias]
            return None if obj is None else obj.get(n.prop)
        if isinstance(n, Not):
            return not ev(n.arg)
        if isinstance(n, Neg):
            v = ev(n.arg)
            return None if v is None else -v
        if isinstance(n, IsNull):
            return (ev(n.arg) is None) != n.negated
        if isinstance(n, In):
            v = ev(n.arg)
            return any(v == ev(i) for i in n.items)
        if isinstance(n, Bin):
            if n.op == "and":
                return bool(ev(n.left)) and bool(ev(n.right))
            if n.op == "or":
                return bool(ev(n.left)) or bool(ev(n.right))
            a, b = ev(n.left), ev(n.right)
            if n.op == "==":
                return a == b
            if n.op == "!=":
                return a != b
            if a is None or b is None:
                return False if n.op in ("<", "<=", ">", ">=") else None
            try:
                if n.op == "<":
                    return a < b
                if n.op == "<=":
                    return a <= b
                if n.op == ">":
                    return a > b
                if n.op == ">=":
                    return a >= b
                if n.op == "+":
                    return a + b
                if n.op == "-":
                    return a - b
                if n.op == "*":
                    return a * b
                if n.op == "/":
                    return None if b == 0 else a / b
            except TypeError as e:
                raise ExprError(f"type error evaluating {n.op!r}: {e}") from e
        raise ExprError(f"cannot evaluate {n!r}")  # Exists is filter-only

    return ev(node)


# ---------------------------------------------------------------- validation
_NUMERIC = ("int", "float")


def _prop_map(onto, type_name):
    t = onto.object_types.get(type_name)
    if t is None:
        return None
    m = {p.name: p for p in t.properties}
    return m


def _lit_compat(prop, lit: Lit) -> str | None:
    v = lit.value
    if v is None:
        return "compare with 'is null' / 'is not null' instead of null"
    if prop.type == "enum":
        if not isinstance(v, str):
            return f"property '{prop.name}' is an enum, got {v!r}"
        if v not in prop.enum:
            return f"'{v}' is not a value of enum '{prop.name}' ({', '.join(prop.enum)})"
    elif prop.type in _NUMERIC:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"property '{prop.name}' is {prop.type}, got {v!r}"
    elif prop.type == "bool":
        if not isinstance(v, bool):
            return f"property '{prop.name}' is bool, got {v!r}"
    elif prop.type in ("string", "timestamp"):
        if not isinstance(v, str):
            return f"property '{prop.name}' is {prop.type}, got {v!r}"
    else:
        return f"property '{prop.name}' of type {prop.type} cannot be compared"
    return None


def subtype_names(onto, target: str) -> list[str]:
    """Concrete object types that may sit at `target` (an object type or a class)."""
    out = []
    for name in sorted(onto.object_types):
        cur, seen = name, set()
        while cur and cur not in seen:
            if cur == target:
                out.append(name)
                break
            seen.add(cur)
            cur = onto.parent_of(cur)
    return out


def validate_filter(node: Node, onto, type_name: str) -> list[str]:
    """Structural + name validation of a row filter against `type_name`. Returns problems."""
    errs: list[str] = []
    props = _prop_map(onto, type_name)
    if props is None:
        return [f"unknown object type '{type_name}'"]

    def operand(n, props, tname):
        if isinstance(n, Ref):
            if n.alias is not None:
                errs.append("qualified names are not allowed in row filters")
            elif n.prop not in props:
                errs.append(f"unknown property '{n.prop}' on {tname}")
            else:
                return props[n.prop]
        else:
            errs.append(f"expected a property name, got {type(n).__name__}")
        return None

    def value(n):
        if isinstance(n, Lit):
            return n
        if isinstance(n, Param):
            if n.name != "region":
                errs.append(f"only $region is allowed in row filters, got ${n.name}")
            return n
        errs.append("filter values must be literals or $region")
        return None

    def check(n, props, tname):
        if isinstance(n, Bin) and n.op in ("and", "or"):
            check(n.left, props, tname)
            check(n.right, props, tname)
        elif isinstance(n, Not):
            check(n.arg, props, tname)
        elif isinstance(n, Bin) and n.op in ("==", "!=", "<", "<=", ">", ">="):
            l, r, op = n.left, n.right, n.op
            if not isinstance(l, Ref) and isinstance(r, Ref):
                l, r = r, l
            p = operand(l, props, tname)
            v = value(r)
            if p is None or v is None:
                return
            if isinstance(v, Lit):
                bad = _lit_compat(p, v)
                if bad:
                    errs.append(bad)
            elif p.type not in ("string", "enum"):
                errs.append(f"$region can only be compared with string/enum properties, '{p.name}' is {p.type}")
            if op not in ("==", "!=") and p.type in ("bool", "enum", "point", "linestring"):
                errs.append(f"operator {op} is not valid for {p.type} property '{p.name}'")
        elif isinstance(n, In):
            p = operand(n.arg, props, tname)
            for it in n.items:
                v = value(it)
                if p is not None and isinstance(v, Lit):
                    bad = _lit_compat(p, v)
                    if bad:
                        errs.append(bad)
        elif isinstance(n, IsNull):
            operand(n.arg, props, tname)
        elif isinstance(n, Exists):
            ln = onto.link_types.get(n.link)
            if ln is None:
                errs.append(f"exists: unknown link '{n.link}'")
                return
            if tname not in subtype_names(onto, ln.src):
                errs.append(f"exists: link '{n.link}' starts at {ln.src}, not {tname}")
            if n.type not in onto.object_types:
                errs.append(f"exists: unknown type '{n.type}'")
                return
            if n.type not in subtype_names(onto, ln.dst):
                errs.append(f"exists: link '{n.link}' points to {ln.dst}, not {n.type}")
            tprops = _prop_map(onto, n.type)
            check(n.cond, tprops, n.type)
        else:
            errs.append(f"unsupported filter construct {type(n).__name__}"
                        + (f" ({n.op})" if isinstance(n, Bin) else ""))

    check(node, props, type_name)
    return errs


def validate_expr(node: Node, onto, *, params: Mapping[str, str] | None = None, aliases: Mapping[str, str] | None = None,
                  new_names: set[str] | None = None) -> list[str]:
    """Validate an action expression: params, aliases (alias -> object type), `$new.x` names, props."""
    errs: list[str] = []
    params, aliases, new_names = params or {}, aliases or {}, new_names or set()
    for n in walk(node):
        if isinstance(n, Param):
            head, _, rest = n.name.partition(".")
            if n.name in ("now", "actor"):
                continue
            if head == "new":
                if rest not in new_names:
                    errs.append(f"unknown reference ${n.name} (no create rule with as: {rest or '?'})")
            elif rest:
                t = aliases.get(head)
                if t is None:
                    errs.append(f"unknown reference ${n.name}")
                elif rest != "id" and rest not in (_prop_map(onto, t) or {}):
                    errs.append(f"unknown property '{rest}' on {t} in ${n.name}")
            elif n.name not in params:
                errs.append(f"unknown parameter ${n.name}")
        elif isinstance(n, Ref):
            t = aliases.get(n.alias)
            if t is None:
                errs.append(f"unknown alias '{n.alias}' (declare it under bind:)")
            elif n.prop != "id" and n.prop not in (_prop_map(onto, t) or {}):
                errs.append(f"unknown property '{n.prop}' on {t} ({n.alias}.{n.prop})")
        elif isinstance(n, Exists):
            errs.append("exists(...) is not allowed in action expressions")
        elif isinstance(n, Bin) and n.op in ("==", "!="):
            for a, b in ((n.left, n.right), (n.right, n.left)):
                if isinstance(a, Ref) and isinstance(b, Lit) and b.value is not None and a.alias in aliases and a.prop != "id":
                    p = (_prop_map(onto, aliases[a.alias]) or {}).get(a.prop)
                    if p is not None:
                        bad = _lit_compat(p, b)
                        if bad:
                            errs.append(bad)
    return errs


# ---------------------------------------------------------------- SQL rendering (row filters)
REGION_SQL = "NULLIF(current_setting('amarh.region', true), '')"


def _sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def current_pred(alias: str) -> str:
    return (f"(upper_inf({alias}.recorded_during) OR upper({alias}.recorded_during) = 'infinity') "
            f"AND {alias}.valid_during @> now() AND {alias}.scenario_id = 0")


def _qi(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def filter_to_sql(node: Node, onto, type_name: str, alias: str, *, region_sql: str = REGION_SQL) -> str:
    """Render a validated row filter as a SQL boolean expression over table alias `alias`.

    `exists(link, Type: expr)` becomes a correlated subquery over the base link/object tables restricted
    to current versions. Identifiers come from the validated ontology (never from request data) and
    literals are quoted. Raises ExprError if the filter does not validate.
    """
    errs = validate_filter(node, onto, type_name)
    if errs:
        raise ExprError("; ".join(errs))
    counter = [0]

    def lit_or_param(n):
        return region_sql if isinstance(n, Param) else _sql_lit(n.value)

    def render(n, tbl_alias):
        if isinstance(n, Bin) and n.op in ("and", "or"):
            return f"({render(n.left, tbl_alias)} {n.op.upper()} {render(n.right, tbl_alias)})"
        if isinstance(n, Not):
            return f"(NOT {render(n.arg, tbl_alias)})"
        if isinstance(n, Bin):
            l, r, op = n.left, n.right, n.op
            if not isinstance(l, Ref):
                l, r = r, l
                op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
            sop = {"==": "=", "!=": "<>"}.get(op, op)
            return f"({tbl_alias}.{_qi(l.prop)} {sop} {lit_or_param(r)})"
        if isinstance(n, In):
            return f"({tbl_alias}.{_qi(n.arg.prop)} IN ({', '.join(lit_or_param(i) for i in n.items)}))"
        if isinstance(n, IsNull):
            return f"({tbl_alias}.{_qi(n.arg.prop)} IS {'NOT ' if n.negated else ''}NULL)"
        if isinstance(n, Exists):
            counter[0] += 1
            k = counter[0]
            la, oa = f"xl{k}", f"xo{k}"
            return (f"EXISTS (SELECT 1 FROM link_{n.link} {la} JOIN obj_{_snake(n.type)} {oa} ON {oa}.id = {la}.dst "
                    f"WHERE {la}.src = {tbl_alias}.id AND {current_pred(la)} AND {current_pred(oa)} "
                    f"AND {render(n.cond, oa)})")
        raise ExprError(f"cannot render {n!r}")

    return render(node, alias)


def _snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.lower()


__all__ = ["ExprError", "Node", "Lit", "Param", "Ref", "Bin", "Not", "Neg", "In", "IsNull", "Exists",
           "parse_expr", "parse_filter", "walk", "params_used", "evaluate", "validate_expr", "validate_filter",
           "filter_to_sql", "current_pred", "subtype_names", "REGION_SQL"]
