"""Proposal model validation, deterministic to_yaml, compile-ability, role template."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engine.compiler import CompiledOntology
from engine.ingest.proposal import Proposal, ProposalError, proposal_diff, to_yaml

H = "a" * 64
ROOT = Path(__file__).resolve().parents[2]


def col(tid, n, name, type_="string", **kw):
    return {"id": f"t{tid}.c{n}", "name": name, "ident": name, "type": type_, "null_rate": 0.0, "distinct": 3,
            "unique": False, "min": None, "max": None, "top": [], "enum_candidate": None, "format": None,
            "geo": None, "time": False, "notes": [], **kw}


def table(n, name, cols):
    return {"id": f"t{n}", "name": name, "ident": name.title(), "rows": 10, "sampled_rows": 10, "columns": cols,
            "key_candidates": [], "is_join_table": False}


def profile(tables):
    return {"version": 1, "profile_hash": H, "source": {"kind": "csv", "name": "demo.zip", "sha256": "0" * 64,
            "bytes": 1}, "tables": tables, "fks": [], "warnings": []}


PROFILE = profile([
    table(1, "customers", [col(1, 1, "id", "int"), col(1, 2, "name"), col(1, 3, "tier"), col(1, 4, "lat", "float"),
                           col(1, 5, "lon", "float")]),
    table(2, "orders", [col(2, 1, "id", "int"), col(2, 2, "customer_id", "int"), col(2, 3, "total", "float"),
                        col(2, 4, "status")]),
    table(3, "products", [col(3, 1, "id", "int"), col(3, 2, "title")]),
    table(4, "order_products", [col(4, 1, "order_id", "int"), col(4, 2, "product_id", "int")]),
    table(5, "regions", [col(5, 1, "id", "int"), col(5, 2, "name")]),
])


def prop(name, type_="string", column=None, **kw):
    return {"name": name, "type": type_, "required": False, "column": column, "transform": "none", "label": name, **kw}


def base():
    return {
        "version": 1, "profile_hash": H,
        "objectTypes": [
            {"name": "Customer", "label": "Customer", "key": ["t1.c1"], "source_table": "t1", "properties": [
                prop("name", "string", "t1.c2", required=True),
                prop("tier", {"enum": ["gold", "silver"]}, "t1.c3", transform="enum_map:" + json.dumps({"g": "gold"})),
                prop("location", "point", None, transform="point(t1.c4,t1.c5)")]},
            {"name": "Sale", "label": "Sale", "key": ["t2.c1"], "source_table": "t2", "properties": [
                prop("total", "float", "t2.c3", transform="float"),
                prop("status", {"enum": ["open", "shipped", "closed"]}, "t2.c4")]},
            {"name": "Product", "label": "Product", "key": ["t3.c1"], "source_table": "t3", "properties": [
                prop("title", "string", "t3.c2")]}],
        "linkTypes": [
            {"name": "placed_by", "from": "Sale", "to": "Customer", "cardinality": "many-to-one",
             "source": {"kind": "fk", "from_col": "t2.c2", "to_type_key": "t1.c1"}},
            {"name": "contains", "from": "Sale", "to": "Product", "cardinality": "many-to-many",
             "source": {"kind": "join_table", "table": "t4", "src_col": "t4.c1", "dst_col": "t4.c2"}}],
        "classes": [], "actions": [], "roles": [], "rationale": "r", "notes": []}


def mk(**over):
    d = base()
    d.update(over)
    return d


# ------------------------------------------------------------------ happy paths
def test_fk_and_join_table_compile():
    p = Proposal.model_validate(base())
    text, mapping = to_yaml(p, PROFILE, slug="demo")
    c = CompiledOntology.from_text(text)
    assert c.model.namespace == "https://isnad.local/ws/demo#"
    assert set(c.model.object_types) == {"Customer", "Sale", "Product"}
    assert set(c.model.link_types) == {"placed_by", "contains"}
    assert c.model.name == "demo.zip"
    assert c.model.geo_types == ("Customer",)
    assert mapping["links"]["contains"]["kind"] == "join_table"
    assert mapping["links"]["placed_by"]["from_col"] == "t2.c2"
    assert mapping["types"]["Sale"]["columns"]["total"]["col"] == "t2.c3"
    assert set(c.model.roles) == {"admin"}
    c.ddl(), c.owl_turtle(), c.shacl_turtle(), c.ts_sdk(), c.pydantic_source()


def test_deterministic():
    p = Proposal.model_validate(base())
    a = to_yaml(p, PROFILE, slug="demo")
    b = to_yaml(Proposal.model_validate(copy.deepcopy(base())), copy.deepcopy(PROFILE), slug="demo")
    assert a == b
    assert json.dumps(a[1], sort_keys=True) == json.dumps(b[1], sort_keys=True)
    # reordering the model's lists must not change the output
    d = base()
    d["objectTypes"].reverse()
    d["linkTypes"].reverse()
    assert to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")[0] == a[0]


def test_default_namespace_from_hash_never_model_chosen():
    d = base()
    d["namespace"] = "https://evil.example/x#"
    text, _ = to_yaml(Proposal.model_validate(d), PROFILE)
    assert yaml.safe_load(text)["namespace"] == "https://isnad.local/ws/w" + H[:10] + "#"


def test_enum_geo_extends_actions_roles():
    d = base()
    d["classes"] = [{"name": "Party", "extends": None}]
    d["objectTypes"][0]["extends"] = "Party"
    d["actions"] = [
        {"name": "rename_customer", "description": "x", "kind": "update_props", "type": "Customer",
         "params": {"props": ["name"]}},
        {"name": "ship_order", "description": "x", "kind": "set_status", "type": "Sale",
         "params": {"prop": "status", "value": "shipped", "from": ["open"]}},
        {"name": "add_item", "description": "x", "kind": "link", "type": "Sale", "params": {"link": "contains"}},
        {"name": "drop_item", "description": "x", "kind": "unlink", "type": "Sale", "params": {"link": "contains"}}]
    d["roles"] = [{"name": "viewer", "description": "v", "access": "read"},
                  {"name": "clerk", "description": "c", "access": "write"},
                  {"name": "admin", "description": "", "access": "admin"}]
    text, _ = to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")
    m = CompiledOntology.from_text(text).model
    assert m.classes["Customer"].extends == "Party"
    assert m.parent_of("Customer") == "Party"
    assert m.actions["ship_order"].rules[0].set == {"status": "shipped"}
    assert m.actions["add_item"].rules[0].kind == "link"
    assert m.actions["drop_item"].rules[0].kind == "unlink"
    assert {r.name for r in m.roles.values()} == {"admin", "viewer", "clerk"}
    # template: no masks, no row filters, viewer has no actions, clerk cannot delete or approve
    for r in m.roles.values():
        assert all(not a.mask and a.where is None for a in r.types.values())
    assert m.roles["viewer"].actions == ()
    assert "delete_object" not in m.roles["clerk"].actions and not m.roles["clerk"].approves
    assert not m.roles["viewer"].can_write("Sale") and m.roles["viewer"].can_read("Sale")
    assert m.roles["admin"].all_actions and m.roles["admin"].all_approves
    assert set(m.roles["clerk"].agent.allowlist) == {"rename_customer", "ship_order", "add_item", "drop_item"}
    assert m.role_can_approve("admin", "ship_order") and not m.role_can_approve("clerk", "ship_order")
    assert not m.role_can_execute("viewer", "create_object") and m.role_can_execute("clerk", "create_object")
    assert not m.role_can_execute("clerk", "delete_object")
    assert not m.functions and not m.webhooks


def test_extends_object_type_target():
    d = base()
    d["objectTypes"][1]["extends"] = "Product"
    text, _ = to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")
    m = CompiledOntology.from_text(text).model
    assert m.parent_of("Sale") == "Product" and "Product" in m.classes


def test_generic_actions_match_supply_chain():
    sc = yaml.safe_load((ROOT / "ontology" / "supply_chain.yaml").read_text())["actions"]
    text, _ = to_yaml(Proposal.model_validate(base()), PROFILE, slug="demo")
    gen = yaml.safe_load(text)["actions"]
    for g in ("create_object", "update_object", "delete_object", "create_link", "end_link"):
        for k in ("description", "params", "function"):
            assert gen[g][k] == sc[g][k], (g, k)


def test_proposal_diff_first_version_and_next():
    d = base()
    d["roles"] = [{"name": "viewer", "description": "v", "access": "read"}]
    text, _ = to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")
    df = proposal_diff(None, text)
    assert "Customer" in df["added"]["objectTypes"] and not df["breaking"]
    assert any(s.startswith("role viewer") for s in df["sensitive"])
    assert any(s.startswith("role admin") for s in df["sensitive"])
    d2 = base()
    d2["objectTypes"].append({"name": "Region", "key": ["t5.c1"], "source_table": "t5",
                              "properties": [prop("name", "string", "t5.c2")]})
    text2, _ = to_yaml(Proposal.model_validate(d2), PROFILE, slug="demo")
    d3 = proposal_diff(text, text2)
    assert d3["added"]["objectTypes"] == ["Region"]


# ------------------------------------------------------------------ validation
def bad(mutate):
    d = base()
    mutate(d)
    with pytest.raises((ValidationError, ProposalError)):
        to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")


@pytest.mark.parametrize("name", ["customer", "Cust omer", "Cust\nomer", "Customer'; DROP TABLE x;--", "C" * 60,
                                  "Café", "", "Select", "Table", "Cust‮omer", "Customer\n"])
def test_hostile_type_names(name):
    bad(lambda d: d["objectTypes"][0].update(name=name))


@pytest.mark.parametrize("name", ["Name", "na me", "id", "valid_during", "select", "n" * 60, "name\n", "a;b", "x​",
                                  "scenario_id", "src"])
def test_hostile_property_names(name):
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(name=name))


def test_extra_fields_refused():
    bad(lambda d: d.update(evil=1))
    bad(lambda d: d["objectTypes"][0].update(evil=1))
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(evil=1))
    bad(lambda d: d["linkTypes"][0].update(evil=1))
    bad(lambda d: d["linkTypes"][0]["source"].update(evil=1))
    bad(lambda d: d["roles"].append({"name": "x", "access": "read", "types": "all"}))


def test_oversize_lists_and_strings():
    bad(lambda d: d.update(rationale="x" * 4001))
    bad(lambda d: d.update(notes=["n"] * 21))
    bad(lambda d: d.update(notes=["n" * 501]))
    bad(lambda d: d.update(roles=[{"name": f"r{i}", "access": "read"} for i in range(9)]))
    bad(lambda d: d["objectTypes"][0].update(properties=[prop(f"p{i}") for i in range(61)]))
    bad(lambda d: d.update(objectTypes=[dict(d["objectTypes"][0], name=f"T{i}") for i in range(51)]))
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(label="x" * 121))
    bad(lambda d: d["objectTypes"][0].update(key=["t1.c1", "t1.c2", "t1.c3"]))


def test_enum_caps_and_chars():
    bad(lambda d: d["objectTypes"][1]["properties"][1].update(type={"enum": [f"v{i}" for i in range(65)]}))
    bad(lambda d: d["objectTypes"][1]["properties"][1].update(type={"enum": ["a" * 65]}))
    bad(lambda d: d["objectTypes"][1]["properties"][1].update(type={"enum": ["a\x00b"]}))
    bad(lambda d: d["objectTypes"][1]["properties"][1].update(type={"enum": ["a", "a"]}))
    bad(lambda d: d["objectTypes"][1]["properties"][1].update(type={"enum": []}))
    bad(lambda d: d["objectTypes"][1]["properties"][1].update(type="hstore"))


def test_unknown_references():
    bad(lambda d: d["objectTypes"][0].update(source_table="t99"))
    bad(lambda d: d["objectTypes"][0].update(key=["t1.c99"]))
    bad(lambda d: d["objectTypes"][0].update(key=["t2.c1"]))          # column of another table
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(column="t2.c3"))
    bad(lambda d: d["linkTypes"][0].update(to="Nope"))
    bad(lambda d: d["linkTypes"][0]["source"].update(from_col="t9.c1"))
    bad(lambda d: d["linkTypes"][1]["source"].update(table="t9"))
    bad(lambda d: d["linkTypes"][0]["source"].update(to_type_key="t1.c2"))   # not a key column
    bad(lambda d: d["objectTypes"][0].update(extends="Ghost"))
    bad(lambda d: d["objectTypes"][0]["properties"][2].update(transform="point(t1.c4,t2.c1)"))
    bad(lambda d: d.update(profile_hash="b" * 64) if False else d.update(profile_hash="zz"))


def test_profile_hash_mismatch():
    d = base()
    d["profile_hash"] = "b" * 64
    with pytest.raises(ProposalError):
        to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")


def test_bad_transforms():
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(transform="os.system('x')"))
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(transform="timestamp:" + "x" * 41))
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(transform="enum_map:not json"))
    bad(lambda d: d["objectTypes"][0]["properties"][0].update(transform="point(t1.c4,t1.c5)"))  # type string
    bad(lambda d: d["objectTypes"][0]["properties"][2].update(transform="none"))               # point w/o transform


def test_role_template_cannot_grant_more():
    bad(lambda d: d["roles"].append({"name": "boss", "access": "admin"}))
    bad(lambda d: d["roles"].append({"name": "admin", "access": "write"}))
    bad(lambda d: d["roles"].append({"name": "sim", "access": "read"}))
    bad(lambda d: d["roles"].append({"name": "Bad Role", "access": "read"}))
    bad(lambda d: d["roles"].extend([{"name": "a", "access": "read"}, {"name": "a", "access": "write"}]))
    d = base()
    d["roles"] = [{"name": "viewer", "description": "x", "access": "read"}]
    y = yaml.safe_load(to_yaml(Proposal.model_validate(d), PROFILE, slug="demo")[0])
    assert set(y["roles"]["viewer"]) == {"description", "types", "links", "actions", "agent"}
    assert y["roles"]["viewer"]["actions"] == []
    assert all(v == {"read": True} for v in y["roles"]["viewer"]["types"].values())
    assert "allowlist" not in y["roles"]["viewer"]["agent"]


def test_action_validation():
    act = {"name": "a1", "description": "", "kind": "set_status", "type": "Sale",
           "params": {"prop": "status", "value": "shipped"}}
    bad(lambda d: d["actions"].append(dict(act, name="create_object")))
    bad(lambda d: d["actions"].append(dict(act, params={"prop": "status", "value": "gone"})))
    bad(lambda d: d["actions"].append(dict(act, params={"prop": "total", "value": "x"})))
    bad(lambda d: d["actions"].append(dict(act, params={"prop": "status", "value": "shipped", "sql": "x"})))
    bad(lambda d: d["actions"].append(dict(act, params={"prop": "status", "value": "shipped", "from": ["x'"]})))
    bad(lambda d: d["actions"].append(dict(act, type="Ghost")))
    bad(lambda d: d["actions"].append({"name": "u", "kind": "update_props", "type": "Sale", "params": {"props": ["zz"]}}))
    bad(lambda d: d["actions"].append({"name": "u", "kind": "update_props", "type": "Sale", "params": {}}))
    bad(lambda d: d["actions"].append({"name": "l", "kind": "link", "type": "Customer", "params": {"link": "contains"}}))
    bad(lambda d: d["actions"].append({"name": "l", "kind": "raw_sql", "type": "Sale", "params": {}}))


def test_yaml_injection_via_strings_is_inert():
    d = base()
    d["rationale"] = "x\nroles:\n  evil: {types: all}"
    d["roles"] = [{"name": "viewer", "description": "a: {actions: all} # x", "access": "read"}]
    d["objectTypes"][0]["label"] = "x: {y} z"
    p = Proposal.model_validate(d)
    text, mapping = to_yaml(p, copy.deepcopy(PROFILE) | {"source": {"kind": "csv", "name": "n\nroles: x", "sha256": "0"}},
                            slug="demo")
    y = yaml.safe_load(text)
    assert set(y["roles"]) == {"admin", "viewer"} and y["roles"]["viewer"]["actions"] == []
    assert "\n" not in y["name"]


def test_slug_namespace_sanitised():
    text, _ = to_yaml(Proposal.model_validate(base()), PROFILE, slug="../../Evil Name!#")
    assert yaml.safe_load(text)["namespace"] == "https://isnad.local/ws/evil_name#"


def test_baseline_shaped_minimum():
    d = {"profile_hash": H, "objectTypes": [{"name": "Region", "key": ["t5.c1"], "source_table": "t5",
                                              "properties": []}]}
    text, _ = to_yaml(Proposal.model_validate(d), PROFILE, slug="x")
    CompiledOntology.from_text(text)
