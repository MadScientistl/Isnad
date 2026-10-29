"""Parsed ontology model + validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class Ontology:
    version: int
    namespace: str
    classes: dict[str, ClassDef] = field(default_factory=dict)
    object_types: dict[str, ObjectType] = field(default_factory=dict)
    link_types: dict[str, LinkType] = field(default_factory=dict)

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
