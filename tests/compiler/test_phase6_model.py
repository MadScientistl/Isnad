"""Phase 6 model additions: name, kpis, sim, volatile, geo_types, from_text, output dir independence."""
import copy
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql

from engine.compiler import CompiledOntology, OntologyError, compile_ontology, kpi_sql, write_outputs
from engine.compiler.model import parse_ontology
from engine.db import Principal, connect, get_dsn
from engine.db.principal import scoped
from engine.db.setup import apply_schema

ROOT = Path(__file__).resolve().parents[2]
RAW = yaml.safe_load((ROOT / "ontology" / "supply_chain.yaml").read_text())


def raw(**over):
    d = copy.deepcopy(RAW)
    d.update(over)
    return d


def test_supply_chain_attributes():
    m = compile_ontology(ROOT / "ontology" / "supply_chain.yaml").model
    assert m.name == "Supply chain"
    assert m.sim is not None and m.sim.module == "sim.world"
    props = {(t, p.name): p for t, o in m.object_types.items() for p in o.properties}
    assert props[("Shipment", "position")].volatile and props[("Shipment", "progress")].volatile
    assert props[("Shipment", "position")].type == "point" and props[("Shipment", "progress")].type == "float"
    assert not props[("Shipment", "status")].volatile
    assert set(m.geo_types) == {"Port", "Warehouse", "Shipment"}
    assert set(m.kpis) == {"delay_hours_total", "shipments_delayed", "shipments_in_transit", "ports_closed",
                           "invoices_flagged"}
    assert m.kpis["shipments_delayed"].expr == "count(Shipment, where status = 'delayed')"


def test_defaults_without_new_blocks():
    d = raw()
    for k in ("name", "sim", "kpis"):
        d.pop(k, None)
    m = parse_ontology(d)
    assert m.sim is None and m.kpis == {}
    assert m.name == "example.org"  # fallback: namespace host


def test_volatile_in_enum_and_scalar_forms():
    d = raw()
    d["objectTypes"]["Carrier"]["properties"]["tier"] = {"enum": ["a", "b"], "volatile": True}
    d["objectTypes"]["Carrier"]["properties"]["score"] = {"type": "int", "required": True}
    m = parse_ontology(d)
    pr = {p.name: p for p in m.object_types["Carrier"].properties}
    assert pr["tier"].volatile and pr["tier"].enum == ("a", "b")
    assert pr["score"].type == "int" and pr["score"].required and not pr["score"].volatile
    with pytest.raises(OntologyError):
        d["objectTypes"]["Carrier"]["properties"]["bad"] = {"type": "hstore"}
        parse_ontology(d)


@pytest.mark.parametrize("expr", [
    "SELECT 1", "count(Shipment); DROP TABLE x", "count(Nope)", "count(Shipment.status)", "sum(Shipment)",
    "sum(Shipment.status)", "sum(Shipment.nope)", "avg(Port.name)", "count(Shipment, where status = )",
    "count(Shipment, where nosuch = 'x')", "count(Shipment, where status = $region)",
    "count(Shipment, where 1=1; --)", "median(Shipment.delay_hours)", "max(Shipment.status)",
    "count(Shipment, where status = 'a' or pg_sleep(1))", "x" * 2001])
def test_kpi_rejects(expr):
    d = raw()
    d["kpis"] = {"k": {"description": "d", "expr": expr}}
    with pytest.raises((OntologyError,)):
        parse_ontology(d)


def test_kpi_schema_rejects_raw_sql_key_and_extra():
    for k in ({"k": {"sql": "select 1"}}, {"K": {"expr": "count(Port)"}}, {"k": {"expr": "count(Port)", "x": 1}}):
        with pytest.raises(OntologyError):
            parse_ontology(raw(kpis=k))


def test_sim_and_name_schema():
    for bad in ({"module": "os; import x"}, {"module": "a/b"}, {}, {"module": "a.b", "x": 1}):
        with pytest.raises(OntologyError):
            parse_ontology(raw(sim=bad))
    with pytest.raises(OntologyError):
        parse_ontology(raw(name=""))


def test_kpi_sql_forms():
    d = raw(kpis={
        "a": {"expr": "count(Port)"},
        "b": {"expr": "sum(Shipment.cargo_value, where status in ('delayed', 'in_transit') and delay_hours > 1)"},
        "c": {"expr": "avg(Invoice.amount)"},
        "d": {"expr": "max(Shipment.eta)"},
        "e": {"expr": "count(Shipment, where exists(ships_via, Port: status = 'closed'))"},
        "f": {"expr": "min(Supplier.price_index, where country is not null)"}})
    m = parse_ontology(d)
    s = {k: kpi_sql(m, v) for k, v in m.kpis.items()}
    assert s["a"] == "SELECT count(*) FROM rd_cur_obj_port x"
    assert "IN ('delayed', 'in_transit')" in s["b"] and s["b"].startswith("SELECT coalesce(sum(")
    assert "x.\"amount\"" in s["c"] and "EXISTS" in s["e"]
    assert kpi_sql(m, m.kpis["a"], "rd_eff_obj_port").endswith("rd_eff_obj_port x")
    assert ";" not in "".join(s.values()).replace("; ", "")


def test_kpi_sql_runs():
    name = f"kp_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(get_dsn(), autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"postgres unreachable: {e}")
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        d = raw(kpis={
            "b": {"expr": "sum(Shipment.cargo_value, where status in ('delayed') and delay_hours > 1)"},
            "c": {"expr": "avg(Invoice.amount)"}, "d": {"expr": "max(Shipment.eta)"},
            "e": {"expr": "count(Shipment, where exists(ships_via, Port: status = 'closed'))"},
            "f": {"expr": "count(Port, where region = 'EMEA')"}})
        c = CompiledOntology.from_text(yaml.safe_dump(d, sort_keys=False))
        apply_schema(admin, c.ddl())
        conn = connect(schema=name)
        with scoped(conn, Principal("admin", "adam")):
            for k, v in c.model.kpis.items():
                val = conn.execute(kpi_sql(c.model, v)).fetchone()[0]
                assert val in (0, None) or k == "d"
        conn.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
        admin.close()


def test_from_text_and_bounds():
    text = (ROOT / "ontology" / "supply_chain.yaml").read_text()
    c = CompiledOntology.from_text(text)
    assert c.hash == compile_ontology(ROOT / "ontology" / "supply_chain.yaml").hash
    with pytest.raises(OntologyError):
        CompiledOntology.from_text("a: &a [1]\n" + "".join(f"b{i}: *a\n" for i in range(12)))
    with pytest.raises(OntologyError):
        CompiledOntology.from_text("x" * 300_000)
    with pytest.raises(OntologyError):
        CompiledOntology.from_text("- a\n- b\n")


def test_outputs_to_arbitrary_dir_use_own_namespace(tmp_path):
    d = {"version": 1, "name": "Bikes", "namespace": "https://isnad.local/ws/bikes#",
         "objectTypes": {"Station": {"properties": {"name": "string!", "where": "point"}}}}
    c = CompiledOntology.from_text(yaml.safe_dump(d))
    out = tmp_path / "ws" / "build"
    paths = write_outputs(c, out, sdk_dir=tmp_path / "sdk")
    assert {p.name for p in paths} == {"schema.sql", "ontology.ttl", "shapes.ttl", "models.py", "client.ts"}
    assert "isnad.local/ws/bikes#" in (out / "ontology.ttl").read_text()
    assert "example.org/sc" not in "".join(p.read_text() for p in paths)
    assert (tmp_path / "sdk" / "client.ts").exists()
