import pytest

from engine.compiler import compile_ontology
from engine.compiler.exprs import (Bin, Exists, ExprError, In, IsNull, Lit, Not, Param, Ref, evaluate, filter_to_sql,
                                   params_used, parse_expr, parse_filter, validate_expr, validate_filter)
from pathlib import Path

ONTO = compile_ontology(Path(__file__).resolve().parents[2] / "ontology" / "supply_chain.yaml").model


# ---- action expressions
def test_parse_expr_shapes():
    n = parse_expr("inv.status != 'paid' and $amount >= 10.5")
    assert n == Bin("and", Bin("!=", Ref("inv", "status"), Lit("paid")), Bin(">=", Param("amount"), Lit(10.5)))
    assert parse_expr("a.x is not null") == IsNull(Ref("a", "x"), True)
    assert parse_expr("a.x in ('a', 'b')") == In(Ref("a", "x"), (Lit("a"), Lit("b")))
    assert parse_expr("not a.x == 1") == Not(Bin("==", Ref("a", "x"), Lit(1)))
    assert parse_expr("a.x = 1") == parse_expr("a.x == 1")
    assert parse_expr("-1 + 2 * 3") == Bin("+", Lit(-1), Bin("*", Lit(2), Lit(3)))
    assert parse_expr("$new.event is null") == IsNull(Param("new.event"))
    assert parse_expr("a.x == 'it''s'") == Bin("==", Ref("a", "x"), Lit("it's"))
    assert parse_expr("true or false") == Bin("or", Lit(True), Lit(False))
    assert params_used(parse_expr("$a + $b.c > $now")) == {"a", "b.c", "now"}


@pytest.mark.parametrize("bad", [
    "", "   ", "a.x ==", "== 1", "a.x == 1 2", "x", "a.x in ()", "a.x in 1", "a.x is 3", "a.x @ 1", "(a.x == 1",
    "a.x == 'unterminated", "exists(ships_to, Warehouse: region = 'x')", "$", "a. == 1", "import os", "__import__('os')",
])
def test_parse_expr_rejects(bad):
    with pytest.raises(ExprError):
        parse_expr(bad)


def test_evaluate():
    al = {"inv": {"status": "open", "amount": 5.0, "id": "i1"}, "gone": None}
    ev = lambda t, **k: evaluate(parse_expr(t), aliases=al, **k)
    assert ev("inv.status != 'paid'") is True
    assert ev("inv.status == 'paid'") is False
    assert ev("inv.amount * 2 > $limit", params={"limit": 9}) is True
    assert ev("inv.amount < $missing") is False  # None ordering is False
    assert ev("gone.status is null") is True
    assert ev("inv.status in ('open', 'approved') and not inv.amount > 100") is True
    assert ev("$actor == 'u1'", actor="u1") is True
    assert ev("$new.event is not null", new={"event": 1}) is True
    assert ev("$inv.id == 'i1'") is True
    assert ev("inv.amount / 0") is None
    with pytest.raises(ExprError):
        ev("nope.x == 1")


def test_validate_expr_names():
    al = {"inv": "Invoice"}
    ok = lambda t: validate_expr(parse_expr(t), ONTO, params={"x": "float"}, aliases=al, new_names={"ev"})
    assert ok("inv.status != 'paid' and $x > 0 and $now is not null and $new.ev is not null and $inv.id == $actor") == []
    assert any("unknown alias" in e for e in ok("zzz.status == 'a'"))
    assert any("unknown property 'bogus'" in e for e in ok("inv.bogus == 1"))
    assert any("unknown parameter $y" in e for e in ok("$y == 1"))
    assert any("not a value of enum" in e for e in ok("inv.status == 'nope'"))
    assert any("unknown reference $new.zz" in e for e in ok("$new.zz is null"))
    assert any("unknown property 'q'" in e for e in ok("$inv.q is null"))


# ---- row filters
def test_parse_filter_shapes():
    assert parse_filter("region = $region") == Bin("==", Ref(None, "region"), Param("region"))
    assert parse_filter("status in ('open','closed') or region is null").op == "or"
    f = parse_filter("exists(ships_to, Warehouse: region = $region and name != 'x')")
    assert isinstance(f, Exists) and f.link == "ships_to" and f.type == "Warehouse"
    assert parse_filter("not (region = 'EMEA')") == Not(Bin("==", Ref(None, "region"), Lit("EMEA")))


@pytest.mark.parametrize("bad", ["", "a.b = 1", "region = ", "exists(ships_to Warehouse: x = 1)", "exists(ships_to, Warehouse x = 1)",
                                 "region in 'a'", "name like 'x'", "region = $region;", "1 = 1 --", "region = 'a' ) ("])
def test_parse_filter_syntax_errors(bad):
    with pytest.raises(ExprError):
        parse_filter(bad)


@pytest.mark.parametrize("text,typ,frag", [
    ("nope = 'x'", "Port", "unknown property 'nope'"),
    ("region = 'MARS'", "Port", "not a value of enum"),
    ("region = 5", "Port", "enum"),
    ("name = null", "Port", "is null"),
    ("name = $now", "Port", "only $region"),
    ("name = other", "Port", "literals or $region"),
    ("region + 1 = 2", "Port", "property name"),
    ("location = 'x'", "Port", "cannot be compared"),
    ("lat > 'x'", "Port", "float"),
    ("lat = $region", "Port", "string/enum"),
    ("region = 'EMEA'", "Nope", "unknown object type"),
    ("exists(nope, Port: region = 'EMEA')", "Shipment", "unknown link 'nope'"),
    ("exists(ships_to, Port: region = 'EMEA')", "Shipment", "points to Warehouse"),
    ("exists(ships_to, Ghost: region = 'EMEA')", "Shipment", "unknown type 'Ghost'"),
    ("exists(ships_to, Warehouse: bogus = 1)", "Shipment", "unknown property 'bogus'"),
    ("exists(ships_to, Warehouse: region = 'EMEA')", "Port", "starts at Shipment"),
    ("'EMEA' = 'EMEA'", "Port", "property name"),
])
def test_validate_filter_rejects(text, typ, frag):
    errs = validate_filter(parse_filter(text), ONTO, typ)
    assert errs and any(frag in e for e in errs), errs


def test_filter_to_sql():
    sql = filter_to_sql(parse_filter("region = $region and status != 'closed'"), ONTO, "Port", "t")
    assert sql == """((t."region" = NULLIF(current_setting('amarh.region', true), '')) AND (t."status" <> 'closed'))"""
    assert filter_to_sql(parse_filter("lat >= 3.5 or lat is null"), ONTO, "Port", "t") == '((t."lat" >= 3.5) OR (t."lat" IS NULL))'
    assert filter_to_sql(parse_filter("status in ('open','closed')"), ONTO, "Port", "p") == """(p."status" IN ('open', 'closed'))"""
    assert filter_to_sql(parse_filter("not region = 'EMEA'"), ONTO, "Port", "t") == """(NOT (t."region" = 'EMEA'))"""
    # operand order is normalised: literal on the left flips the operator
    assert filter_to_sql(parse_filter("5 < lat"), ONTO, "Port", "t") == '(t."lat" > 5)'
    ex = filter_to_sql(parse_filter("exists(ships_to, Warehouse: region = $region)"), ONTO, "Shipment", "t")
    assert "FROM link_ships_to xl1 JOIN obj_warehouse xo1 ON xo1.id = xl1.dst WHERE xl1.src = t.id" in ex
    assert 'xo1."region" = NULLIF(' in ex and "valid_during @> now()" in ex


def test_filter_to_sql_quotes_literals_and_validates():
    sql = filter_to_sql(parse_filter("name = 'x''; DROP TABLE y; --'"), ONTO, "Port", "t")
    assert sql == """(t."name" = 'x''; DROP TABLE y; --')"""
    with pytest.raises(ExprError):
        filter_to_sql(parse_filter("nope = 1"), ONTO, "Port", "t")


def test_nested_exists_gets_unique_aliases():
    # Facility is a class: a link into it from Shipment accepts Port or Warehouse targets
    sql = filter_to_sql(parse_filter("exists(ships_from, Port: region = 'EMEA') or exists(ships_to, Warehouse: region = 'EMEA')"),
                        ONTO, "Shipment", "t")
    assert "xl1" in sql and "xl2" in sql
