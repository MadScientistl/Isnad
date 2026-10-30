"""Datasets are deterministic, references are valid and consistent, cassettes are in sync, and the runner works offline."""
import csv
import json
from pathlib import Path

import pytest
import yaml

from engine.compiler import CompiledOntology
from evals.ontogen import make_cassettes, make_datasets, run

ROOT = Path(__file__).resolve().parents[3] / "evals" / "ontogen"
SCHEMA = Path(__file__).resolve().parents[3] / "ontology" / "schema" / "ontology.schema.json"
csv.field_size_limit(10_000_000)


def test_datasets_regenerate_to_identical_bytes():
    for ds, files in make_datasets.build_all().items():
        again = make_datasets.DATASETS[ds]()
        assert files == again
        for fn, data in files.items():
            assert (ROOT / "datasets" / ds / fn).read_bytes() == data, f"{ds}/{fn} differs: run python -m evals.ontogen.make_datasets"


def test_dataset_sizes_and_shape():
    for ds in make_datasets.DATASETS:
        files = list((ROOT / "datasets" / ds).glob("*.csv"))
        assert files and sum(f.stat().st_size for f in files) < 1_500_000
    n = lambda ds, f: sum(1 for _ in open(ROOT / "datasets" / ds / f, newline="", encoding="utf-8")) - 1  # noqa: E731
    assert 2000 <= n("messy_supply", "shipments.csv") <= 5000 and 2000 <= n("bikeshare", "trips.csv") <= 5000


def test_messy_supply_is_messy():
    ships = list(csv.DictReader(open(ROOT / "datasets/messy_supply/shipments.csv", newline="")))
    ports = {r["port_code"] for r in csv.DictReader(open(ROOT / "datasets/messy_supply/ports.csv", newline=""))}
    assert any(r["origin_port"] not in ports for r in ships)                              # orphan foreign keys
    assert {"In Transit", "in_transit"} <= {r["status"] for r in ships}                  # enum variants
    assert {"N/A", "NULL", "-", ""} & {r["Cargo Val"] for r in ships}                      # null tokens
    assert any("/" in r["ETA"] for r in ships) and any("-" in r["ETA"] for r in ships)     # mixed date formats


def test_injection_dataset_is_hostile():
    text = (ROOT / "datasets/injection/notes.csv").read_text(encoding="utf-8")
    head = text.splitlines()[0].lower()
    assert "ignore previous instructions" in head and "system:" in head
    assert "webhook" in text.lower() and "<tool_result>" in text and "\x07" in text and "A" * 50_000 in text
    assert (ROOT / "datasets/injection/widgets.csv").exists()


@pytest.mark.parametrize("ds", ["messy_supply", "bikeshare"])
def test_references_valid_and_consistent_with_ground_truth_and_csvs(ds):
    text = (ROOT / "references" / f"{ds}.yaml").read_text()
    import jsonschema
    jsonschema.validate(yaml.safe_load(text), json.loads(SCHEMA.read_text()))
    co = CompiledOntology.from_text(text)
    gt = json.loads((ROOT / "references" / f"{ds}.ground_truth.json").read_text())
    ref_types = co.model.object_types
    assert set(gt["types"]) == set(ref_types) and set(gt["links"]) == set(co.model.link_types)
    for t, d in gt["types"].items():
        header = next(csv.reader(open(ROOT / "datasets" / ds / f"{d['table']}.csv", newline="", encoding="utf-8")))
        assert set(d["key"]) <= set(header)
        for p, v in d["properties"].items():
            assert v["column"] in header and p in {x.name for x in ref_types[t].properties}
        if d.get("geo"):
            assert {d["geo"]["lat"], d["geo"]["lon"]} <= set(header)
    for ref in gt["enums"]:
        t, p = ref.split(".")
        assert p in {x.name for x in ref_types[t].properties}
    for l, d in gt["links"].items():
        header = next(csv.reader(open(ROOT / "datasets" / ds / f"{gt['types'][d['from']]['table']}.csv", newline="", encoding="utf-8")))
        assert d["from_col"] in header


def test_cassettes_are_in_sync_with_the_builder():
    for fn, txt in make_cassettes.render().items():
        assert (ROOT / "cassettes" / fn).read_text() == txt, "run python -m evals.ontogen.make_cassettes"


def test_mock_suite_hard_checks_all_pass_offline():
    res = run.run_suite("mock", None, "lite")
    s = run.summarise(res)
    assert s["hard_fail"] == 0 and s["hard_pass"] >= 12 and len(res) == 4
    by = {r["cassette"]: r for r in res}
    assert by["bikeshare_repair"]["repair_rounds"] == 1 and by["bikeshare_good"]["repair_rounds"] == 0
    assert by["injection_obedient"]["rejected_submits"] and all(r["status"] == "completed" for r in res)
    assert by["messy_supply_good"]["metrics"]["aggregate"] == 1.0


def test_baseline_suite_runs_without_a_model():
    res = run.run_suite("baseline", None, "lite")
    assert all(r["cost_usd"] == 0.0 for r in res if r["status"] == "ok")
    assert all(h["status"] == "pass" for r in res for h in r["hard"] if h["class"] == "hard")


def test_live_mode_refuses_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert run.main(["--mode", "live"]) == 2
    assert "refused" in capsys.readouterr().err


def test_obedient_injection_is_rejected_by_the_pipeline_not_trusted():
    res = {r["cassette"]: r for r in run.run_suite("mock", "injection", "lite")}
    r = res["injection_obedient"]
    assert "admin" in json.dumps(r["rejected_submits"]) or "unknown top-level" in json.dumps(r["rejected_submits"])
    assert r["proposal_types"] == ["Widget", "Note"]
