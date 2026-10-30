import copy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from engine.compiler import OntologyError, compile_ontology
from engine.compiler.model import parse_ontology, parse_param_type

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / "ontology" / "supply_chain.yaml"
BASE = yaml.safe_load(YAML.read_text())
comp = compile_ontology(YAML)
M = comp.model


def mut(fn):
    d = copy.deepcopy(BASE)
    fn(d)
    return d


def errs(fn) -> str:
    with pytest.raises(OntologyError) as e:
        parse_ontology(mut(fn))
    return str(e.value)


# ---- the shipped ontology
def test_roles_defined():
    assert set(M.roles) == {"planner", "finance_analyst", "regional_ops_lead", "admin", "sim"}
    assert "analyst" not in M.roles
    r = M.roles["regional_ops_lead"]
    assert r.region == "EMEA" and r.types["Port"].where == "region = $region" and r.types["Shipment"].where_ast is not None
    assert M.roles["planner"].types["Invoice"].read is False and not M.roles["planner"].can_read("Invoice")
    assert M.roles["planner"].types["Supplier"].mask == ("country",)
    assert set(M.roles["admin"].types) == set(M.object_types) and M.roles["admin"].can_write("Invoice")
    assert M.roles["admin"].approves == tuple(sorted(M.actions))
    assert set(M.roles["admin"].links) == set(M.link_types)


def test_region_property_on_port_and_warehouse():
    for t in ("Port", "Warehouse"):
        p = {p.name: p for p in M.object_types[t].properties}["region"]
        assert p.enum == ("APAC", "EMEA", "AMER")


def test_demo_permissions():
    assert M.role_can_execute("finance_analyst", "flag_invoice") and M.role_can_execute("admin", "flag_invoice")
    assert not M.role_can_execute("planner", "flag_invoice")
    assert not M.role_can_execute("regional_ops_lead", "flag_invoice")
    for r in ("planner", "regional_ops_lead", "admin"):
        assert M.role_can_execute(r, "close_port")
    assert not M.role_can_execute("finance_analyst", "close_port")
    pay = M.actions["pay_invoice"]
    assert pay.approval_required and pay.approvers == ("admin",) and "sim" in pay.approval_exempt
    assert M.role_can_execute("finance_analyst", "pay_invoice") and M.role_can_approve("admin", "pay_invoice")
    assert not M.role_can_approve("finance_analyst", "pay_invoice")
    assert M.actions["flag_invoice"].webhooks == ("notify_finance",) and M.actions["close_port"].webhooks == ("notify_ops",)
    assert set(M.webhooks) == {"notify_ops", "notify_finance"}
    assert M.actions["close_port"].function == "close_port" and M.actions["flag_invoice"].function is None


def test_every_action_has_logic_and_roles():
    for a in M.actions.values():
        assert a.roles, a.name
        assert bool(a.function) != bool(a.rules), a.name
    assert {r for a in M.actions.values() for r in a.roles} <= set(M.roles)
    sim = set(M.roles["sim"].actions)
    assert {"ship_depart", "ship_position", "ship_arrive", "delay_shipment", "close_port", "reopen_port", "issue_invoice",
            "flag_invoice", "pay_invoice", "create_object", "create_link"} == sim


def test_action_details_parsed():
    f = M.actions["flag_invoice"]
    assert f.bind["inv"].type == "Invoice" and f.bind["inv"].id == "$invoice"
    assert f.criteria[0].message == "Invoice is already paid" and f.criteria[0].ast is not None
    r = f.rules[0]
    assert (r.kind, r.type, r.id, r.set) == ("modify", "Invoice", "$invoice", {"status": "flagged", "anomaly": "$anomaly"})
    assert M.actions["pay_invoice"].rollback == "compensate"
    assert [s.name for s in M.actions["flag_invoice"].param_specs()] == ["invoice", "anomaly"]


def test_param_type_parser():
    ps = parse_param_type("uuid(Invoice)!")
    assert (ps.base, ps.ref, ps.required) == ("uuid", "Invoice", True)
    assert parse_param_type("float").required is False
    assert parse_param_type("string(Port)") is None and parse_param_type("blob") is None and parse_param_type("uuid()") is None


def test_uuid_type_param_roundtrip_and_ts():
    d = mut(lambda d: d["actions"]["flag_invoice"].update(params={"invoice": "uuid(Invoice)!", "anomaly": "string!"}))
    o = parse_ontology(d)
    from engine.compiler.codegen import ts_source
    assert "invoice: string; anomaly: string" in ts_source(o)


def test_generated_sdk_governance():
    ts = comp.ts_sdk()
    assert 'export type RoleName = "admin" | "finance_analyst" | "planner" | "regional_ops_lead" | "sim";' in ts
    assert "SELECTABLE_ROLES" in ts and '"sim"]' not in ts.split("SELECTABLE_ROLES")[1].split("\n")[0]
    assert '"requiresApproval": true' in ts and "ACTION_META" in ts
    m = comp.pydantic_models()
    assert m.ACTION_META["pay_invoice"]["requiresApproval"] is True and "sim" not in m.SELECTABLE_ROLES


def test_cli_roundtrip_schema_has_policy(tmp_path):
    r = subprocess.run([sys.executable, "-m", "engine.compiler", str(YAML), str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    sql = (tmp_path / "schema.sql").read_text()
    assert sql == comp.ddl() and comp.policy_ddl() in sql and comp.base_ddl() in sql
    assert "FORCE ROW LEVEL SECURITY" in sql and "amarh_visible_port" in sql and "rd_cur_obj_supplier" in sql


def test_policy_ddl_shape():
    sql = comp.policy_ddl()
    assert "CREATE ROLE amarh_app NOLOGIN" in sql and "pg_catalog, %I, public, pg_temp" in sql
    assert "DELETE" not in sql.replace("REVOKE DELETE, TRUNCATE", "")
    assert "TO amarh_app USING" in sql and "TO CURRENT_USER" in sql
    assert sql.count("FORCE ROW LEVEL SECURITY") == len(M.object_types) + len(M.link_types)


# ---- bad inputs: every problem is listed
@pytest.mark.parametrize("fn,frag", [
    (lambda d: d["roles"]["planner"]["types"].update(Ghost={"read": True}), "unknown object type 'Ghost'"),
    (lambda d: d["roles"]["planner"]["types"]["Supplier"].update(mask=["nope"]), "mask references unknown property 'nope'"),
    (lambda d: d["roles"]["planner"]["types"]["Shipment"].update(read=False), "write requires read"),
    (lambda d: d["roles"]["planner"]["types"]["Port"].update(where="bogus = 1"), "unknown property 'bogus'"),
    (lambda d: d["roles"]["planner"]["types"]["Port"].update(where="region = $region"), "role has no region"),
    (lambda d: d["roles"]["planner"]["types"]["Port"].update(where="region ="), "where:"),
    (lambda d: d["roles"]["planner"]["types"]["Port"].update(where="__import__('os')"), "where:"),
    (lambda d: d["roles"]["planner"]["types"]["Port"].update(where="region = 'MARS'"), "not a value of enum"),
    (lambda d: d["roles"]["planner"]["types"]["Invoice"].update(where="status = 'open'"), "where without read access"),
    (lambda d: d["roles"]["planner"]["types"]["Invoice"].update(mask=["ref"]), "mask without read access"),
    (lambda d: d["roles"]["planner"]["links"].append("nope"), "unknown link 'nope'"),
    (lambda d: d["roles"]["planner"]["actions"].append("nope"), "unknown action 'nope'"),
    (lambda d: d["roles"]["planner"].update(approves=["nope"]), "unknown action 'nope'"),
    (lambda d: d["roles"]["planner"].update(region="MARS"), "region"),
    (lambda d: d["roles"]["planner"].update(bogus=1), "bogus"),
    (lambda d: d["roles"]["admin"]["types"] if False else d["roles"].update(Bad={}), "Bad"),
    (lambda d: d["actions"]["flag_invoice"]["roles"].append("ghost"), "unknown role 'ghost'"),
    (lambda d: d["actions"]["flag_invoice"]["roles"].append("planner"), "does not list the action"),
    (lambda d: d["roles"]["planner"]["actions"].append("flag_invoice"), "does not list the role"),
    (lambda d: d["roles"]["finance_analyst"].update(approves=["pay_invoice"]), "does not list it as approver"),
    (lambda d: d["actions"]["pay_invoice"]["approval"].update(approvers=["finance_analyst"]), "does not approve it"),
    (lambda d: d["actions"]["pay_invoice"]["approval"].update(approvers=[]), "at least one approver"),
    (lambda d: d["actions"]["pay_invoice"]["approval"].update(exempt=["ghost"]), "unknown role 'ghost' in approval.exempt"),
    (lambda d: d["actions"]["flag_invoice"].update(webhooks=["nope"]), "unknown webhook 'nope'"),
    (lambda d: d["actions"]["flag_invoice"].update(function="x"), "either rules or function"),
    (lambda d: d["actions"]["flag_invoice"].pop("rules"), "needs rules or function"),
    (lambda d: d["actions"]["flag_invoice"].pop("roles"), "no roles may execute"),
    (lambda d: d["actions"]["flag_invoice"].update(rollback="undo"), "undo"),
    (lambda d: d["actions"]["flag_invoice"]["bind"]["inv"].update(type="Ghost"), "bind 'inv' has unknown type 'Ghost'"),
    (lambda d: d["actions"]["flag_invoice"]["bind"]["inv"].update(id="$nope"), "unknown parameter $nope"),
    (lambda d: d["actions"]["flag_invoice"]["bind"]["inv"].update(id="invoice"), "must be a $reference"),
    (lambda d: d["actions"]["flag_invoice"]["bind"]["inv"].update(id="$anomaly"), "not a uuid parameter"),
    (lambda d: d["actions"]["flag_invoice"]["bind"].update(now={"type": "Invoice", "id": "$invoice"}), "reserved"),
    (lambda d: d["actions"]["flag_invoice"]["params"].update(invoice="uuid(Port)!"), "does not match parameter"),
    (lambda d: d["actions"]["flag_invoice"]["params"].update(invoice="uuid(Ghost)!"), "unknown type 'Ghost'"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "inv.status =="}), "criteria[1]"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "inv.nope == 1"}), "unknown property 'nope'"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "$nope > 1"}), "unknown parameter $nope"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "x.status == 'a'"}), "unknown alias 'x'"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "inv.status == 'bogus'"}), "not a value of enum"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "$new.x is null"}), "$new"),
    (lambda d: d["actions"]["flag_invoice"]["criteria"].append({"expr": "exists(billed_by, Supplier: name = 'x')"}), "criteria[1]"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"].update(type="Ghost"), "unknown type 'Ghost'"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"]["set"].update(nope=1), "unknown property 'nope'"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"]["set"].update(status="bogus"), "not a value of enum"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"]["set"].update(amount="$anomaly"), "cannot be assigned to float"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"]["set"].update(ref=None), "cannot be set to null"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"]["set"].update(anomaly="$ghost"), "is not a parameter"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"].update(id="$anomaly"), "not a uuid parameter"),
    (lambda d: d["actions"]["flag_invoice"]["rules"][0]["modify"].update(id="nope"), "must be a $reference"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"create": {"type": "Event", "set": {"kind": "x"}, "as": "e"}, "modify": {}}), "rules"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"create": {"type": "Event", "set": {"severity": "low"}}}), "required property 'kind'"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"create": {"type": "Event", "as": "e", "set": {"kind": "$ghost"}}}), "is not a parameter"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"link": {"link": "affects", "src": "$new.zz", "dst": "$invoice"}}), "refers to no earlier create"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"link": {"link": "nope", "src": "$invoice", "dst": "$invoice"}}), "unknown link 'nope'"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"delete": {"type": "Invoice"}}), "'id' is a required property"),
    (lambda d: d["actions"]["flag_invoice"]["rules"].append({"explode": {}}), "explode"),
    (lambda d: d["webhooks"]["notify_ops"].update(url_env="lowercase"), "url_env"),
    (lambda d: d["webhooks"]["notify_ops"].update(max_attempts=0), "max_attempts"),
    (lambda d: d["webhooks"]["notify_ops"].pop("url_env"), "url_env"),
])
def test_governance_validation_errors(fn, frag):
    assert frag in errs(fn)


def test_all_problems_listed_at_once():
    def bad(d):
        d["roles"]["planner"]["types"]["Port"].update(where="bogus = 1")
        d["roles"]["planner"]["links"].append("nope")
        d["actions"]["flag_invoice"]["criteria"].append({"expr": "inv.zz == 1"})
        d["actions"]["flag_invoice"]["webhooks"] = ["ghost"]
    msg = errs(bad)
    for frag in ("unknown property 'bogus'", "unknown link 'nope'", "unknown property 'zz'", "unknown webhook 'ghost'"):
        assert frag in msg


def test_new_reference_chain_valid():
    def good(d):
        d["actions"]["flag_invoice"]["rules"] += [
            {"create": {"as": "ev", "type": "Event", "set": {"kind": "invoice_flagged", "started": "$now", "note": "$anomaly"}}},
            {"link": {"link": "affects", "src": "$new.ev", "dst": "$invoice"}}]
    # affects points to Port; $invoice is a plain uuid so this is accepted structurally
    parse_ontology(mut(good))


def test_ontology_without_roles_still_parses():
    d = {"version": 1, "namespace": "https://e.org/x#", "objectTypes": {"A": {"properties": {"n": "string"}}},
         "actions": {"a": {"params": {"x": "string"}}}}
    o = parse_ontology(d)
    assert o.roles == {} and "a" in o.actions
    assert "ENABLE ROW LEVEL SECURITY" in compile_for(d).policy_ddl()


def compile_for(d):
    from engine.compiler import CompiledOntology
    return CompiledOntology(parse_ontology(d), "x")


def test_role_all_shorthand_respects_action_side():
    d = mut(lambda d: d["actions"]["close_port"].update(roles=["planner", "regional_ops_lead", "sim"]))
    o = parse_ontology(d)
    assert not o.role_can_execute("admin", "close_port")  # action does not list admin even though role has `all`
