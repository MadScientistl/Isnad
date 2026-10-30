"""Phase 5 compiler output: scenario-aware eff_* views and policies; the main fast path stays untouched."""
import re
from pathlib import Path

import pytest

from engine.compiler import compile_ontology
from engine.compiler.exprs import current_pred, main_current_pred

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def ddl():
    return compile_ontology(ROOT / "ontology" / "supply_chain.yaml").ddl()


def view(ddl, name):
    m = re.search(rf"CREATE OR REPLACE VIEW {name} WITH \(security_invoker = true\) AS\s+(.*?);\n", ddl, re.S)
    assert m, name
    return m.group(1)


def test_main_views_are_unchanged(ddl):
    cur = "SELECT * FROM obj_port WHERE (upper_inf(recorded_during) OR upper(recorded_during) = 'infinity') AND valid_during @> now() AND scenario_id = 0"
    assert view(ddl, "cur_obj_port") == cur
    assert view(ddl, "cur_link_ships_via").endswith("valid_during @> now() AND scenario_id = 0")
    for v in ("cur_obj_port", "rd_cur_obj_port", "cur_link_ships_via"):
        body = view(ddl, v)
        assert "isnad_" not in body and "current_setting" not in body and "scenario_override" not in body


def test_eff_views_exist_for_every_type_and_link(ddl):
    for t in ("port", "shipment", "supplier", "invoice"):
        assert "scenario_override" in view(ddl, f"eff_obj_{t}")
        assert view(ddl, f"rd_eff_obj_{t}").endswith(f"FROM eff_obj_{t}")
    for l in ("ships_via", "ships_to", "supplies"):
        body = view(ddl, f"eff_link_{l}")
        assert "key_b = a.dst" in body and "isnad_scn_base()" in body
    assert "rd_eff_link_ships_via" not in ddl


def test_helpers_and_eff_pred(ddl):
    assert "CREATE OR REPLACE FUNCTION isnad_scn()" in ddl and "-- BEGIN scenario_core" in ddl
    assert current_pred("t") == main_current_pred("t")                      # legacy call unchanged
    p = current_pred("t", "obj", "Port")
    assert p.startswith("CASE WHEN coalesce(current_setting('isnad.scenario', true), '') IN ('', '0') THEN (")
    assert "isnad_eff_row('obj', 'Port'" in p
    assert "isnad_eff_row('link', 'ships_to'" in current_pred("l", "link", "ships_to")


def test_policies_and_indexes(ddl):
    assert ddl.count("scenario_id = isnad_scn() AND isnad_scenario_visible(scenario_id)") >= 12
    assert "isnad_scenario_writable(scenario_id)" in ddl
    assert "ON obj_port (scenario_id, id)" in ddl and "ON link_ships_via (scenario_id, src)" in ddl
    assert "GRANT SELECT ON" in ddl and "rd_eff_obj_port" in ddl.split("GRANT SELECT ON")[1]
    # exists() filters inside visibility helpers use the scenario-aware predicate
    helper = re.search(r"CREATE OR REPLACE FUNCTION isnad_visible_shipment.*?\$fn\$;", ddl, re.S).group(0)
    assert "isnad_eff_row('link', 'ships_to'" in helper
