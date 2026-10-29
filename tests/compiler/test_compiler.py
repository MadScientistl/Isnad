import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml
from rdflib import Graph, Literal, Namespace, RDF
from rdflib.compare import isomorphic
from pyshacl import validate

from engine.compiler import OntologyError, compile_ontology
from engine.compiler.model import parse_ontology

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / "ontology" / "supply_chain.yaml"
GOLD = Path(__file__).parent / "golden"
SC = Namespace("https://example.org/sc#")
DSN = os.environ.get("AMARH_DSN", "postgresql://amarh:amarh@localhost:55441/amarh")


@pytest.fixture(scope="module")
def comp():
    return compile_ontology(YAML)


def _golden(name, actual, update=os.environ.get("UPDATE_GOLDEN")):
    p = GOLD / name
    if update or not p.exists():
        GOLD.mkdir(exist_ok=True)
        p.write_text(actual)
    return p.read_text()


def test_golden_text(comp):
    assert comp.ddl() == _golden("schema.sql", comp.ddl())
    assert comp.pydantic_source() == _golden("models.py", comp.pydantic_source())
    assert comp.ts_sdk() == _golden("client.ts", comp.ts_sdk())


def test_golden_rdf(comp):
    for name, text in (("ontology.ttl", comp.owl_turtle()), ("shapes.ttl", comp.shacl_turtle())):
        g = Graph().parse(data=text, format="turtle")
        exp = Graph().parse(data=_golden(name, text), format="turtle")
        assert isomorphic(g, exp)


def test_ddl_contract(comp):
    ddl = comp.ddl()
    assert "EXCLUDE USING gist (scenario_id WITH =, id WITH =, valid_during WITH &&, recorded_during WITH &&)" in ddl
    assert "WITHOUT OVERLAPS" not in ddl
    for t in ("obj_supplier", "obj_port", "link_ships_via", "link_operated_by", "cur_obj_port", "cur_link_supplies",
              "graph_outbox", "ontology_registry"):
        assert t in ddl


def test_owl(comp):
    g = Graph().parse(data=comp.owl_turtle(), format="turtle")
    from rdflib import OWL, RDFS
    assert (SC.Port, RDFS.subClassOf, SC.Facility) in g
    assert (SC.Shipment, RDF.type, OWL.Class) in g
    assert (SC.supplies, RDF.type, OWL.TransitiveProperty) in g
    assert (SC.supplies, OWL.inverseOf, SC.supplied_by) in g
    assert (SC.ships_via, RDFS.domain, SC.Shipment) in g and (SC.ships_via, RDFS.range, SC.Port) in g


def _ship(g, i, carriers, ref="R1"):
    s = SC[f"obj/{i}"]
    g.add((s, RDF.type, SC.Shipment))
    g.add((s, SC.ref, Literal(ref)))
    for c in carriers:
        g.add((s, SC.operated_by, SC[f"obj/{c}"]))
    return s


def _conforms(comp, g):
    sh = Graph().parse(data=comp.shacl_turtle(), format="turtle")
    return validate(g, shacl_graph=sh, inference="none")[0]


def test_shacl_valid_and_violations(comp):
    g = Graph()
    _ship(g, "s1", ["c1"])
    assert _conforms(comp, g)
    # missing required link
    g = Graph(); _ship(g, "s1", [])
    assert not _conforms(comp, g)
    # many-to-one: two carriers
    g = Graph(); _ship(g, "s1", ["c1", "c2"])
    assert not _conforms(comp, g)
    # missing required prop
    g = Graph(); s = _ship(g, "s1", ["c1"]); g.remove((s, SC.ref, None))
    assert not _conforms(comp, g)
    # enum violation
    g = Graph()
    p = SC["obj/p1"]
    g.add((p, RDF.type, SC.Port)); g.add((p, SC.name, Literal("X"))); g.add((p, SC.status, Literal("open")))
    assert _conforms(comp, g)
    g.set((p, SC.status, Literal("sideways")))
    assert not _conforms(comp, g)


def test_shacl_one_to_many_inverse():
    raw = {"version": 1, "namespace": "https://e.org/x#",
           "objectTypes": {"A": {"properties": {}}, "B": {"properties": {}}},
           "linkTypes": {"owns": {"from": "A", "to": "B", "cardinality": "one-to-many"}}}
    sh_ttl = parse_and_shacl(raw)
    X = Namespace("https://e.org/x#")
    g = Graph()
    for a in ("a1", "a2"):
        g.add((X[a], RDF.type, X.A)); g.add((X[a], X.owns, X.b1))
    g.add((X.b1, RDF.type, X.B))
    assert not validate(g, shacl_graph=Graph().parse(data=sh_ttl, format="turtle"))[0]
    g.remove((X.a2, X.owns, X.b1))
    assert validate(g, shacl_graph=Graph().parse(data=sh_ttl, format="turtle"))[0]


def parse_and_shacl(raw):
    from engine.compiler.rdf import shacl_turtle
    return shacl_turtle(parse_ontology(raw))


def test_pydantic(comp):
    m = comp.pydantic_models()
    p = m.Port(id=uuid.uuid4(), name="Rotterdam", status="open")
    assert p.lat is None
    with pytest.raises(Exception):
        m.Port(id=uuid.uuid4(), name="x", status="bad")
    with pytest.raises(Exception):
        m.Port(id=uuid.uuid4())


def test_cli(tmp_path):
    r = subprocess.run([sys.executable, "-m", "engine.compiler", str(YAML), str(tmp_path)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for f in ("schema.sql", "ontology.ttl", "shapes.ttl", "models.py", "client.ts"):
        assert (tmp_path / f).stat().st_size > 0


BASE = yaml.safe_load(YAML.read_text())


def mutated(fn):
    import copy
    d = copy.deepcopy(BASE)
    fn(d)
    return d


@pytest.mark.parametrize("fn,msg", [
    (lambda d: d["linkTypes"]["ships_via"].update(to="Nope"), "unknown type 'Nope'"),
    (lambda d: d["linkTypes"]["ships_via"].update(cardinality="some-to-some"), "some-to-some"),
    (lambda d: d["classes"]["Port"].update(extends="Ghost"), "unknown class 'Ghost'"),
    (lambda d: d["objectTypes"]["Port"]["properties"].update(x="blob"), "x"),
    (lambda d: d["linkTypes"]["ships_via"].update(transitive=True), "transitive requires"),
    (lambda d: d["linkTypes"]["ships_via"].update(bogus=1), "bogus"),
    (lambda d: d["objectTypes"]["Port"]["properties"].update(s={"enum": []}), "s"),
])
def test_validation_errors(fn, msg):
    with pytest.raises(OntologyError) as e:
        parse_ontology(mutated(fn))
    assert msg in str(e.value)


def test_class_cycle():
    d = mutated(lambda d: d["classes"].update(Facility={"extends": "Port"}))
    with pytest.raises(OntologyError, match="cycle"):
        parse_ontology(d)


def test_ddl_executes(comp):
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DSN, connect_timeout=2, autocommit=True)
    except Exception as e:
        pytest.skip(f"postgres unreachable: {e}")
    schema = "t_" + uuid.uuid4().hex[:8]
    with conn:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
            conn.execute(f"CREATE SCHEMA {schema}")
            conn.execute(f"SET search_path = {schema}, public")
            conn.execute(comp.ddl())
            conn.execute(comp.ddl())  # idempotent
            pid = uuid.uuid4()
            ins = ("INSERT INTO obj_port (id, name, status, valid_during, recorded_during, changed_by) "
                   "VALUES (%s,'P','open',tstzrange(now()-interval '1 day','infinity'),tstzrange(now()-interval '1 day','infinity'),'t')")
            conn.execute(ins, (pid,))
            assert conn.execute("SELECT count(*) FROM cur_obj_port").fetchone()[0] == 1
            with pytest.raises(psycopg.errors.ExclusionViolation):
                conn.execute(ins, (pid,))
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO obj_port (id,name,status,valid_during,recorded_during,changed_by) "
                             "VALUES (gen_random_uuid(),'P','bad','[2020-01-01,)','[2020-01-01,)','t')")
        finally:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
