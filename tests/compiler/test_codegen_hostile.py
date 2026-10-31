"""L2: hostile enum values must not break out of any generated artefact (TS string literals, Python repr,
Turtle literals, SQL literals)."""
import json
import re

import yaml
from rdflib import Graph, Literal

from engine.compiler import CompiledOntology

HOSTILE = [
    'a"b',
    "back\\slash",
    '"; } export const pwn = 1; //',
    "line\nbreak",
    "quote'single",
    "</script><svg onload=1>",
    "u sep",
]


def _compile(values):
    doc = {"version": 1, "namespace": "https://x.test/ns#",
           "objectTypes": {"Thing": {"properties": {"status": {"enum": list(values)}, "label": "string"}}}}
    return CompiledOntology.from_text(yaml.safe_dump(doc))


def test_ts_enum_literals_are_json_strings():
    ts = _compile(HOSTILE).ts_sdk()
    line = next(l for l in ts.splitlines() if l.strip().startswith(("status?:", "status:")))
    body = line.split(":", 1)[1].strip().rstrip(";")
    dec = json.JSONDecoder()
    i, got = 0, []
    while i < len(body):
        v, j = dec.raw_decode(body, i)
        got.append(v)
        i = j
        m = re.match(r"\s*\|\s*", body[i:])
        if not m:
            break
        i += m.end()
    assert got == HOSTILE and i == len(body)


def test_python_models_load_and_enforce_enum():
    comp = _compile(HOSTILE)
    ns = {}
    exec(compile(comp.pydantic_source(), "<m>", "exec"), ns)
    M = ns["Thing"]
    for v in HOSTILE:
        assert M(id="00000000-0000-0000-0000-000000000001", status=v).status == v


def test_turtle_and_sql_literals_roundtrip():
    comp = _compile(HOSTILE)
    Graph().parse(data=comp.owl_turtle(), format="turtle")
    g = Graph()
    g.parse(data=comp.shacl_turtle(), format="turtle")
    lits = {str(o) for o in g.objects() if isinstance(o, Literal)}
    assert set(HOSTILE) <= lits
    ddl = comp.ddl()
    assert "quote''single" in ddl and "'a\"b'" in ddl
