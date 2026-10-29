"""SPARQL attack surface: rejected constructs, hidden IRIs, inferred paths, limits."""
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import as_role  # noqa: E402
from oracle import Oracle  # noqa: E402
from engine.sync import sparql as S  # noqa: E402
from engine.sync.iri import obj_iri  # noqa: E402

NS = "https://example.org/sc#"
PFX = f"PREFIX sc: <{NS}> PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
NONADMIN = ["regional_ops_lead", "planner", "finance_analyst", "restricted"]


def q(world, role, text, **kw):
    with as_role(world.schema, role) as c:
        return S.run_query(world.pipe, c, role == "admin", PFX + text, **kw)


def iris(res):
    return {t["value"] for r in res["rows"] for t in r.values() if t["type"] == "uri"}


def hidden_iris(world, role):
    props = {k: dict(v) for k, v in world.props.items()}
    props[world["p_moved"]]["region"] = "APAC"
    o = Oracle(world.types, props, world.links)
    return {obj_iri(NS, i).value for i in world.types if not o.visible(role, i)}


REJECT = {
    "update": "INSERT DATA { <urn:x> <urn:p> <urn:o> }",
    "delete": "DELETE WHERE { ?s ?p ?o }",
    "clear": "CLEAR ALL",
    "load": "LOAD <http://example.org/x>",
    "construct": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "describe": "DESCRIBE ?s WHERE { ?s a sc:Port }",
    "describe_iri": "DESCRIBE <https://example.org/sc#obj/x>",
    "service": "SELECT ?s WHERE { SERVICE <http://evil.example/sparql> { ?s ?p ?o } }",
    "service_silent": "SELECT ?s WHERE { SERVICE SILENT <http://127.0.0.1:1/x> { ?s ?p ?o } }",
    "from": "SELECT ?s FROM <urn:amarh:g:inferred> WHERE { ?s ?p ?o }",
    "from_named": "SELECT ?s FROM NAMED <urn:amarh:g:meta> WHERE { GRAPH ?g { ?s ?p ?o } }",
    "graph_var": "SELECT ?g ?s WHERE { GRAPH ?g { ?s ?p ?o } }",
    "graph_iri": "SELECT ?s WHERE { GRAPH <urn:amarh:g:meta> { ?s ?p ?o } }",
    "graph_nested": "SELECT ?s WHERE { { SELECT ?s WHERE { GRAPH <urn:amarh:g:asserted> { ?s ?p ?o } } } }",
    "garbage": "SELEKT nothing",
    "empty": "   ",
}
AGG = {
    "count": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
    "count_distinct": "SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s a sc:Port }",
    "sum": "SELECT (SUM(?x) AS ?n) WHERE { ?s ?p ?x }",
    "group": "SELECT ?t WHERE { ?s a ?t } GROUP BY ?t",
    "group_having": "SELECT ?t WHERE { ?s a ?t } GROUP BY ?t HAVING (COUNT(?s) > 1)",
    "subquery_count": "SELECT ?n WHERE { { SELECT (COUNT(*) AS ?n) WHERE { ?s a sc:Port } } }",
    "group_concat": "SELECT (GROUP_CONCAT(?s) AS ?g) WHERE { ?s a sc:Port }",
    "max": "SELECT (MAX(?s) AS ?g) WHERE { ?s a sc:Port }",
    "sample": "SELECT (SAMPLE(?s) AS ?g) WHERE { ?s a sc:Port }",
}


@pytest.mark.parametrize("name", sorted(REJECT))
@pytest.mark.parametrize("role", ["regional_ops_lead", "admin"])
def test_rejected_constructs(world, role, name):
    with pytest.raises(S.SparqlError):
        q(world, role, REJECT[name])


@pytest.mark.parametrize("name", sorted(AGG))
@pytest.mark.parametrize("role", NONADMIN)
def test_aggregates_rejected_non_admin(world, role, name):
    with pytest.raises(S.SparqlError):
        q(world, role, AGG[name])


def test_admin_may_aggregate(world):
    r = q(world, "admin", "SELECT (COUNT(*) AS ?n) WHERE { ?s a sc:Port }")
    assert r["rows"][0]["n"]["value"] == "5"


def test_comment_and_string_tricks_do_not_hide_bad_keywords(world):
    q(world, "planner", 'SELECT ?s WHERE { ?s ?p ?o FILTER(?o != "a # b") }')  # '#' inside a string is fine
    with pytest.raises(S.SparqlError):  # comments are rejected outright
        q(world, "planner", "SELECT ?s WHERE { ?s ?p ?o } # harmless\n")
    with pytest.raises(S.SparqlError):
        q(world, "planner", "SELECT ?s WHERE { ?s ?p ?o . SERVICE <http://x/> { ?s ?p ?o } }")
    with pytest.raises(S.SparqlError):
        q(world, "planner", "select ?s where { graph ?g { ?s ?p ?o } }")


@pytest.mark.parametrize("role", NONADMIN)
def test_full_scan_has_no_hidden_iris(world, role):
    hidden = hidden_iris(world, role)
    assert hidden or role == 'finance_analyst'  # finance sees every object type
    for text in ("SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
                 "SELECT ?s WHERE { ?s ?p ?o }",
                 "SELECT ?o WHERE { ?s ?p ?o }",
                 "SELECT ?x WHERE { ?x (<urn:none>|!<urn:none>)+ ?y }",
                 "SELECT ?x ?y WHERE { ?x (sc:ships_via|^sc:ships_via|sc:ships_to|^sc:ships_to|sc:operated_by|^sc:operated_by|sc:supplies|^sc:supplies|sc:billed_by|sc:invoices_for)* ?y }",
                 "SELECT ?s ?o WHERE { { ?s sc:supplies+ ?o } UNION { ?s sc:supplied_by+ ?o } }",
                 "SELECT ?s ?o ?h WHERE { ?s a ?t OPTIONAL { ?s sc:ships_via ?o } OPTIONAL { ?o sc:ships_via ?h } }",
                 "SELECT ?s WHERE { ?s a ?t FILTER NOT EXISTS { ?s sc:ships_to ?w } }",
                 "SELECT ?s ?c WHERE { { SELECT ?s WHERE { ?s a sc:Shipment } } ?s sc:operated_by ?c }",
                 "SELECT ?s WHERE { VALUES ?s { %s } ?s ?p ?o }" % " ".join(f"<{h}>" for h in (hidden or {"urn:none"}))):
        res = q(world, role, text)
        assert not (iris(res) & hidden), (role, text)
        assert not res["truncated"]


@pytest.mark.parametrize("role", NONADMIN)
def test_probing_a_hidden_iri_equals_probing_a_missing_one(world, role):
    if role == 'finance_analyst':
        pytest.skip('finance sees everything in this world')
    h = sorted(hidden_iris(world, role))[0]
    missing = obj_iri(NS, uuid.uuid4()).value
    for tmpl in ("ASK { <%s> ?p ?o }", "ASK { ?s ?p <%s> }", "ASK { <%s> a ?t }", "ASK { ?a ?p ?b FILTER(?a = <%s> || ?b = <%s>) }"):
        a = q(world, role, tmpl.replace("%s", h))
        b = q(world, role, tmpl.replace("%s", missing))
        assert a["boolean"] is False and a == b
    a = q(world, role, "SELECT ?p ?o WHERE { <%s> ?p ?o }" % h)
    assert a["rows"] == []


def test_visible_rows_equal_oracle_and_admin_sees_more(world):
    props = {k: dict(v) for k, v in world.props.items()}
    props[world["p_moved"]]["region"] = "APAC"
    for role in NONADMIN:
        o = Oracle(world.types, props, world.links)
        res = q(world, role, "SELECT ?s WHERE { ?s a ?t . FILTER(STRSTARTS(STR(?s), '%sobj/')) }" % NS)
        got = {r["s"]["value"] for r in res["rows"]}
        want = {obj_iri(NS, i).value for i in o.vis_ids(role)}
        assert want <= got <= want  # equality; inferred superclass types give duplicate rows only


def test_inferred_path_through_hidden_node_not_revealed(world):
    A, B, C, D, F, G, H = (world["sup_" + x] for x in "abcdfgh")
    iri = lambda i: obj_iri(NS, i).value  # noqa: E731
    r = q(world, "restricted", "SELECT ?x ?y WHERE { ?x sc:supplies+ ?y }")
    pairs = {(t["x"]["value"], t["y"]["value"]) for t in r["rows"]}
    assert (iri(A), iri(D)) in pairs and (iri(C), iri(D)) in pairs
    assert (iri(A), iri(C)) not in pairs and (iri(F), iri(H)) not in pairs
    # explicit inferred / inverse predicates, ASK
    assert q(world, "restricted", f"ASK {{ <{iri(A)}> sc:supplies <{iri(C)}> }}")["boolean"] is False
    assert q(world, "restricted", f"ASK {{ <{iri(C)}> sc:supplied_by <{iri(A)}> }}")["boolean"] is False
    assert q(world, "restricted", f"ASK {{ <{iri(D)}> sc:supplied_by <{iri(A)}> }}")["boolean"] is True
    assert q(world, "restricted", f"ASK {{ <{iri(H)}> sc:supplied_by <{iri(F)}> }}")["boolean"] is False
    # admin sees all
    assert q(world, "admin", f"ASK {{ <{iri(F)}> sc:supplies <{iri(H)}> }}")["boolean"] is True
    # planner sees all suppliers: hidden-for-restricted inferred edge is visible to planner
    assert q(world, "planner", f"ASK {{ <{iri(A)}> sc:supplies <{iri(C)}> }}")["boolean"] is True


def test_two_hop_pattern_through_hidden_port_not_revealed(world):
    """s1 and s4 share the (hidden-to-lead) port p_apac: the join must not reveal it."""
    r = q(world, "regional_ops_lead", "SELECT ?a ?b WHERE { ?a sc:ships_via/^sc:ships_via ?b FILTER(?a != ?b) }")
    pairs = {(t["a"]["value"], t["b"]["value"]) for t in r["rows"]}
    s1, s4 = (obj_iri(NS, world[x]).value for x in ("s1", "s4"))
    assert (s1, s4) not in pairs and (s4, s1) not in pairs


def test_class_hierarchy_only_visible_instances(world):
    r = q(world, "regional_ops_lead", "SELECT ?x WHERE { ?x a sc:Facility }")
    names = iris(r)
    assert obj_iri(NS, world["p_emea"]).value in names and obj_iri(NS, world["w_emea"]).value in names
    assert obj_iri(NS, world["p_apac"]).value not in names and obj_iri(NS, world["w_apac"]).value not in names


def test_max_rows_and_truncated(world):
    r = q(world, "planner", "SELECT ?s ?p ?o WHERE { ?s ?p ?o }", max_rows=3)
    assert len(r["rows"]) == 3 and r["truncated"]


def test_timeout(world):
    class SlowStore:
        def query(self, *a, **k):
            time.sleep(1.0)

    class P:
        store = SlowStore()
        cfg = world.pipe.cfg
        _lock = world.pipe._lock

    t0 = time.monotonic()
    with pytest.raises(S.SparqlError) as e:
        S.run_query(P(), None, True, "SELECT ?s WHERE { ?s ?p ?o }", timeout_s=0.2)
    assert e.value.code == "timeout" and time.monotonic() - t0 < 0.9


def test_query_api_has_no_write_side_effects(world):
    before = len(list(world.pipe.store.quads_for_pattern(None, None, None, None)))
    for bad in (REJECT["update"], REJECT["delete"], REJECT["clear"]):
        with pytest.raises(S.SparqlError):
            q(world, "admin", bad)
    assert len(list(world.pipe.store.quads_for_pattern(None, None, None, None))) == before


def test_meta_graph_not_exposed(world):
    r = q(world, "admin", "SELECT ?s ?p ?o WHERE { ?s ?p ?o FILTER(STRSTARTS(STR(?s), 'urn:amarh')) }")
    assert r["rows"] == []


P7 = """SELECT * WHERE { # '''
 SERVICE <http://127.0.0.1:18777/x> { ?s ?p ?o }
 # '''
}"""


@pytest.mark.parametrize("is_admin", [False, True])
def test_p7_comment_quote_then_service_rejected(is_admin):
    with pytest.raises(S.SparqlError) as ei:
        S.validate(P7, is_admin=is_admin)
    assert "127.0.0.1" not in str(ei.value)


@pytest.mark.parametrize("bad", [
    "SELECT * WHERE { SERVICE SILENT <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { SERVICE ?v { ?s ?p ?o } }",
    "SELECT * FROM <http://x/> WHERE { ?s ?p ?o }",
    "SELECT * FROM NAMED <http://x/> WHERE { ?s ?p ?o }",
    "SELECT * WHERE { GRAPH <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { # c\n ?s ?p ?o }",
])
def test_sparql_forbidden_forms(bad):
    for adm in (False, True):
        with pytest.raises(S.SparqlError):
            S.validate(bad, is_admin=adm)


def test_lexer_blanks_strings_and_iris_left_to_right():
    assert "SERVICE" not in S._strip('SELECT * WHERE { ?s ?p "SERVICE # x" . ?s ?q <http://a/b#SERVICE> }')
    with pytest.raises(S.SparqlError):
        S._strip("SELECT * WHERE { ?s ?p ?o } #")
