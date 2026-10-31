"""Real evaluation datasets through the real validator; performance report for the largest."""
import time

import pytest

from pathlib import Path

DATASETS = Path(__file__).resolve().parents[3] / "evals" / "ontogen" / "datasets"
from engine.ingest.validate import validate

pytestmark = pytest.mark.usefixtures("no_scratch_left")


def test_bikeshare_cassette_good(env):
    with env.open(DATASETS / "bikeshare") as src:
        prof = env.profile(src)
        rep = validate(prof, env.cassette("bikeshare_good", prof), src)
    assert rep.ok, [c for c in rep.checks if not c["ok"]]
    assert rep.stats["rows_loaded"]["Trip"] == 3000 or sum(rep.stats["rows_loaded"].values()) > 3000


def test_bikeshare_baseline_good(env):
    with env.open(DATASETS / "bikeshare") as src:
        prof = env.profile(src)
        rep = validate(prof, env.baseline(prof), src)
    assert rep.ok, [c for c in rep.checks if not c["ok"]]


def test_messy_supply_cassette_good_merges_denormalised_carrier(env):
    with env.open(DATASETS / "messy_supply") as src:
        prof = env.profile(src)
        rep = validate(prof, env.cassette("messy_supply_good", prof), src)
    assert rep.ok, [c for c in rep.checks if not c["ok"]]
    assert rep.stats["rows_merged"]["Carrier"] > 0 and rep.stats["rows_loaded"]["Carrier"] < 20
    assert check_ok(rep, "key_violations")
    assert rep.stats["dropped_links"]            # the dataset has dangling references; counted, not hidden


def test_messy_supply_baseline_has_no_name_collision(env):
    """The baseline used to emit link `origin_port` next to property `origin_port` (one RDF predicate, thousands of SHACL
    violations); link names are now kept distinct from property names."""
    with env.open(DATASETS / "messy_supply") as src:
        prof = env.profile(src)
        rep = validate(prof, env.baseline(prof), src)
    assert rep.check("name_collisions")["ok"] and rep.check("shacl_conforms")["ok"] and rep.ok


def test_injection_dataset_is_data_only(env):
    with env.open(DATASETS / "injection") as src:
        prof = env.profile(src)
        rep = validate(prof, env.baseline(prof), src)
        again = validate(prof, env.baseline(prof), src)
    assert rep.ok == again.ok and [c["name"] for c in rep.checks] == [c["name"] for c in again.checks]
    assert rep.stats["rows_loaded"] == again.stats["rows_loaded"]
    assert not rep.check("internal_error")


def check_ok(rep, name):
    return rep.check(name)["ok"]


def test_performance_report_largest_dataset(env, capsys):
    best = None
    for ds in ("bikeshare", "messy_supply"):
        with env.open(DATASETS / ds) as src:
            prof = env.profile(src)
            t0 = time.perf_counter()
            rep = validate(prof, env.cassette(f"{ds}_good", prof), src)
            dt = time.perf_counter() - t0
            rows = sum(rep.stats["rows_loaded"].values())
            best = max(best or (0, "", 0), (rows, ds, dt))
            assert dt < 60
    with capsys.disabled():
        print(f"\n[validation perf] largest dataset {best[1]}: {best[0]} rows validated in {best[2]:.1f}s")
