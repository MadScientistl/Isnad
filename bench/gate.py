"""Phase 1 feasibility gate.  python -m bench.gate --seed 1 [--scale 1.0] [--runs 5]

Writes bench/results/report.md + results.json (suffix _scale<x> kept alongside).
Thresholds (fixed, from RESEARCH.md / task): materialization < 60 s, 4-hop p95 < 2 s,
subgraph SHACL < 10 s.  Nothing here is tuned to pass them."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from engine.sync import open_store, G_ASSERTED, G_INFERRED
from . import gen, pgload

ROOT = Path(__file__).resolve().parent
NS = "https://example.org/sc#"
TH = {"materialize_s": 60.0, "hop4_p95_s": 2.0, "shacl_s": 10.0}
STAGE_TIMEOUT = 1800


def run_stage(stage: str, store: Path, args, extra=()):
    cmd = [sys.executable, "-m", "bench.worker", stage, "--store", str(store), "--seed", str(args.seed), *extra]
    t = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=STAGE_TIMEOUT, cwd=ROOT.parent)
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {STAGE_TIMEOUT}s", "wall_s": time.perf_counter() - t}
    wall = time.perf_counter() - t
    if p.returncode != 0:
        return {"error": f"exit {p.returncode}", "stderr_tail": p.stderr[-1500:], "wall_s": wall}
    line = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")][-1]
    r = json.loads(line[7:])
    r["wall_s"] = wall
    return r


def closure_check(seed: int) -> dict:
    """Correctness cross-check on a ~2k-object subset: reasonable vs owlrl vs independent BFS closure."""
    from pyoxigraph import Quad
    from engine.reason import materialize
    from engine.sync.iri import RDF_TYPE, class_iri, link_iri, obj_iri
    ds = gen.generate(seed, 0.02)
    st = open_store()
    qs = []
    for t, rows in ds.objects.items():
        qs += [Quad(obj_iri(NS, i), RDF_TYPE, class_iri(NS, t), G_ASSERTED) for i, _ in rows]
    for l, rows in ds.links.items():
        qs += [Quad(obj_iri(NS, s), link_iri(NS, l), obj_iri(NS, d), G_ASSERTED) for s, d in rows]
    st.bulk_extend(qs)
    # independent expected closure for supplies
    adj: dict = {}
    for s, d in ds.links["supplies"]:
        adj.setdefault(s, set()).add(d)
    exp_sup = set()
    for s in adj:
        seen, stack = set(), list(adj[s])
        while stack:
            x = stack.pop()
            if x in seen: continue
            seen.add(x); stack.extend(adj.get(x, ()))
        exp_sup |= {(s, x) for x in seen}
    asserted = {(s, d) for s, d in ds.links["supplies"]}
    sup_iri, by_iri = NS + "supplies", NS + "supplied_by"
    expected = {(obj_iri(NS, s).value, sup_iri, obj_iri(NS, d).value) for s, d in exp_sup - asserted}
    expected |= {(obj_iri(NS, d).value, by_iri, obj_iri(NS, s).value) for s, d in exp_sup}

    def inferred():
        return {(q.subject.value, q.predicate.value, q.object.value) for q in st.quads_for_pattern(None, None, None, G_INFERRED)}
    exp_fac = {(obj_iri(NS, i).value, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", NS + "Facility") for t in ("Port", "Warehouse") for i, _ in ds.objects[t]}
    expected |= exp_fac  # subclass inference Port/Warehouse -> Facility
    out = {"objects": ds.n_objects, "edges": ds.n_edges, "expected_independent": len(expected)}
    r = materialize(st, NS, "reasonable"); rs = inferred()
    o = materialize(st, NS, "owlrl"); os_all = inferred()
    os_ = {t for t in os_all if not t[1].endswith("owl#sameAs")}
    sameas = len(os_all) - len(os_)
    out.update({
        "reasonable_inferred": len(rs), "reasonable_s": r.seconds, "owlrl_inferred": len(os_), "owlrl_s": o.seconds,
        "owlrl_reflexive_sameAs_ignored": sameas,
        "reasonable_equals_owlrl": rs == os_, "reasonable_only": len(rs - os_), "owlrl_only": len(os_ - rs),
        "reasonable_equals_independent": rs == expected, "reasonable_missing_vs_expected": len(expected - rs),
        "reasonable_extra_vs_expected": len(rs - expected),
        "owlrl_equals_independent": os_ == expected,
        "examples_reasonable_only": [list(x) for x in list(rs - os_)[:3]],
        "examples_owlrl_only": [list(x) for x in list(os_ - rs)[:3]],
    })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    (ROOT / "results").mkdir(exist_ok=True)
    work = ROOT / "work"
    if work.exists(): shutil.rmtree(work)
    work.mkdir()
    res = {"seed": args.seed, "scale": args.scale, "machine": platform.platform(), "python": sys.version.split()[0]}
    try:
        import reasonable, pyoxigraph, pyshacl, owlrl
        res["versions"] = {"reasonable": reasonable.__version__, "pyoxigraph": pyoxigraph.__version__,
                           "pyshacl": pyshacl.__version__, "owlrl": getattr(owlrl, "__version__", "?")}
    except Exception as e:  # noqa: BLE001
        res["versions"] = str(e)

    t = time.perf_counter(); ds = gen.generate(args.seed, args.scale)
    res["dataset"] = {"objects": ds.n_objects, "edges": ds.n_edges, "gen_s": time.perf_counter() - t,
                      "by_type": {k: len(v) for k, v in ds.objects.items()},
                      "by_link": {k: len(v) for k, v in ds.links.items()}, "cycle_networks": ds.cycles}
    print("generated", res["dataset"], flush=True)
    pgload.reset_schema("bench")
    res["pg_bulk_load"] = pgload.load(ds, "bench"); print("pg", res["pg_bulk_load"], flush=True)
    del ds
    store = work / "store_outbox"
    res["oxigraph_load_from_outbox"] = run_stage("outbox_load", store, args); print("outbox_load", res["oxigraph_load_from_outbox"], flush=True)
    res["runs"] = []
    for i in range(args.runs):
        store = work / "store_main"
        if i == 0 and (work / "store_outbox").exists():
            shutil.move(str(work / "store_outbox"), str(store))
        run = {"run": i + 1}
        run["rebuild"] = run_stage("rebuild", store, args); print(i + 1, "rebuild", run["rebuild"], flush=True)
        run["materialize"] = run_stage("materialize", store, args); print(i + 1, "materialize", run["materialize"], flush=True)
        run["queries"] = run_stage("queries", store, args); print(i + 1, "queries done", flush=True)
        run["shacl"] = run_stage("shacl", store, args); print(i + 1, "shacl", run["shacl"].get("clean", run["shacl"]), flush=True)
        res["runs"].append(run)
        (ROOT / "results" / "results.partial.json").write_text(json.dumps(res, indent=1))
    res["correctness_subset"] = closure_check(args.seed); print("xcheck", res["correctness_subset"], flush=True)
    res["edits_100"] = run_stage("edits", work / "store_main", args); print("edits", res["edits_100"], flush=True)
    res["verdict"] = verdict(res)
    tag = "" if args.scale == 1.0 else f"_scale{args.scale}"
    (ROOT / "results" / f"results{tag}.json").write_text(json.dumps(res, indent=1))
    (ROOT / "results" / f"report{tag}.md").write_text(report(res))
    if args.scale == 1.0 or True:  # canonical names always hold the latest run
        (ROOT / "results" / "results.json").write_text(json.dumps(res, indent=1))
        (ROOT / "results" / "report.md").write_text(report(res))
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(res["verdict"], indent=1))


def _vals(res, path):
    out = []
    for r in res["runs"]:
        x = r
        try:
            for k in path: x = x[k]
            out.append(x)
        except (KeyError, TypeError):
            out.append(None)
    return out


def verdict(res):
    def worst(path):
        v = [x for x in _vals(res, path) if x is not None]
        return (max(v) if v else None), (len(v) < len(res["runs"]))
    mat, mf = worst(["materialize", "seconds"])
    hop = {}
    for k in ("fixed_4hop", "plus_transitive", "mixed_4hop_shipment"):
        for m in ("cold", "warm"):
            hop[f"{k}.{m}"] = worst(["queries", k, m, "p95_s"])
    shc = worst(["shacl", "clean", "seconds"]); shb = worst(["shacl", "violated", "seconds"])
    det = all(x for x in _vals(res, ["shacl", "deliberate_violation_detected"]))
    v = {"materialize_worst_s": mat, "materialize_pass": mat is not None and mat < TH["materialize_s"] and not mf}
    v["hop4_worst_p95_s"] = {k: x[0] for k, x in hop.items()}
    v["hop4_pass"] = all(x[0] is not None and x[0] < TH["hop4_p95_s"] and not x[1] for x in hop.values())
    v["shacl_worst_s"] = max([x for x in (shc[0], shb[0]) if x is not None], default=None)
    v["shacl_pass"] = v["shacl_worst_s"] is not None and v["shacl_worst_s"] < TH["shacl_s"] and not shc[1]
    v["violation_detected_all_runs"] = det
    v["stable"] = all("error" not in x for r in res["runs"] for x in r.values() if isinstance(x, dict))
    return v


def f(x, d=2):
    return "n/a" if x is None else (f"{x:.{d}f}" if isinstance(x, (int, float)) else str(x))


def report(res):
    v = res["verdict"]; ds = res["dataset"]; pg = res["pg_bulk_load"]; ox = res["oxigraph_load_from_outbox"]
    L = [f"# Phase 1 feasibility gate report (seed {res['seed']}, scale {res['scale']})", "",
         f"Machine: {res['machine']}, Python {res['python']}; versions: {res['versions']}", "",
         f"Dataset: {ds['objects']} objects, {ds['edges']} edges ({ds['by_type']}; {ds['by_link']}); "
         f"{ds['cycle_networks']} supplier networks contain a cycle; 10 long supplies chains (120 deep at scale 1).", "",
         "## Verdict vs thresholds (worst of all runs; thresholds not tuned)", "",
         "| Check | Threshold | Worst observed | Result |", "|---|---|---|---|",
         f"| reasonable materialization wall time | < 60 s | {f(v['materialize_worst_s'])} s | {'PASS' if v['materialize_pass'] else 'FAIL'} |",
         f"| 4-hop SPARQL p95 (all variants, cold and warm) | < 2 s | {f(max([x for x in v['hop4_worst_p95_s'].values() if x is not None], default=None), 3)} s | {'PASS' if v['hop4_pass'] else 'FAIL'} |",
         f"| subgraph SHACL (~1k nodes) | < 10 s | {f(v['shacl_worst_s'])} s | {'PASS' if v['shacl_pass'] else 'FAIL'} |",
         f"| deliberate SHACL violation detected in every run | yes | {v['violation_detected_all_runs']} | {'PASS' if v['violation_detected_all_runs'] else 'FAIL'} |",
         f"| all stages completed in all {len(res['runs'])} runs | yes | {v['stable']} | {'PASS' if v['stable'] else 'FAIL'} |", "",
         "## 1. Load and rebuild times", "",
         f"- Postgres bulk load (bulk_load_objects/links, COPY): objects {f(pg['objects_s'])} s, links {f(pg['links_s'])} s, total {f(pg['total_s'])} s",
         f"- Oxigraph load by consuming the {ox.get('outbox_rows')} outbox rows (incremental path): {f(ox.get('seconds'))} s, peak RSS {f(ox.get('peak_rss_mb'),0)} MB"]
    rb = [r["rebuild"].get("seconds") for r in res["runs"]]
    L += [f"- Full rebuild from current views (per run): {', '.join(f(x) for x in rb)} s; quads {res['runs'][0]['rebuild'].get('quads')}", "",
          "## 2. Materialization (reasonable, OWL 2 RL, ontology + instance graph)", "",
          "| run | wall s | peak RSS MB | inferred triples | base triples | phases (dump/load/reason/diff+write) |", "|---|---|---|---|---|---|"]
    for r in res["runs"]:
        m = r["materialize"]; p = m.get("phases", {})
        L.append(f"| {r['run']} | {f(m.get('seconds'))} | {f(m.get('peak_rss_mb'),0)} | {m.get('inferred_triples')} | {m.get('base_triples')} | "
                 + "/".join(f(p.get(k)) for k in ("dump", "load", "reason", "diff_write")) + f" {m.get('error','')} {m.get('stderr_tail','')[-300:]} |")
    c = res["correctness_subset"]
    L += ["", f"Only triples whose subject is an instance are kept as inferred (ontology-level closure and owl:Thing noise dropped, count in results.json).", "",
          f"### Correctness cross-check ({c['objects']} objects, {c['edges']} edges)", "",
          f"- reasonable inferred {c['reasonable_inferred']} ({f(c['reasonable_s'],3)} s); owlrl inferred {c['owlrl_inferred']} ({f(c['owlrl_s'],3)} s), ignoring {c['owlrl_reflexive_sameAs_ignored']} reflexive owl:sameAs triples that only owlrl emits",
          f"- reasonable == owlrl: **{c['reasonable_equals_owlrl']}** (reasonable-only {c['reasonable_only']}, owlrl-only {c['owlrl_only']})",
          f"- reasonable == independent BFS transitive closure + inverse: **{c['reasonable_equals_independent']}** (missing {c['reasonable_missing_vs_expected']}, extra {c['reasonable_extra_vs_expected']}); owlrl == independent: {c['owlrl_equals_independent']}"]
    if not c["reasonable_equals_owlrl"]:
        L.append(f"- examples reasonable-only: {c['examples_reasonable_only']}; owlrl-only: {c['examples_owlrl_only']}")
    L += ["", "## 3. SPARQL 4-hop (asserted graph; 40 random start nodes per variant; cold = first execution per node, warm = repeat)", "",
          "Process-cold: the OS page cache is not dropped, so 'cold' = first touch per start node in a freshly opened store.", "",
          "| run | variant | cold median / p95 / max s | warm median / p95 / max s | first query after open s | result size median / max |", "|---|---|---|---|---|---|"]
    for r in res["runs"]:
        q = r["queries"]
        if "error" in q:
            L.append(f"| {r['run']} | ERROR | {q} | | | |"); continue
        for k in ("fixed_4hop", "plus_transitive", "mixed_4hop_shipment", "inferred_lookup"):
            x = q[k]
            L.append(f"| {r['run']} | {k} | {f(x['cold']['median_s'],4)} / {f(x['cold']['p95_s'],4)} / {f(x['cold']['max_s'],4)} | "
                     f"{f(x['warm']['median_s'],4)} / {f(x['warm']['p95_s'],4)} / {f(x['warm']['max_s'],4)} | {f(x['first_query_s'],4)} | {x['result_size_median']} / {x['result_size_max']} |")
    L += ["", "## 4. pySHACL on a changed subgraph (330 random shipments + their ports/carriers, properties hydrated from Postgres)", "",
          "| run | nodes | triples | clean s (conforms) | after deliberate violation s | detected (exactly the victim) |", "|---|---|---|---|---|---|"]
    for r in res["runs"]:
        s = r["shacl"]
        if "error" in s:
            L.append(f"| {r['run']} | ERROR {s} | | | | |"); continue
        L.append(f"| {r['run']} | {s['clean']['nodes']} | {s['clean']['triples']} | {f(s['clean']['seconds'])} ({s['clean']['conforms']}) | {f(s['violated']['seconds'])} | {s['deliberate_violation_detected']} |")
    e = res["edits_100"]
    L += ["", "## 5. 100 edits then resync + re-materialize", ""]
    if "error" in e: L.append(f"ERROR: {e}")
    else:
        L.append(f"- 100 edits in Postgres: {f(e['pg_write_s'])} s; outbox rows {e['outbox_rows']}; resync {f(e['sync_s'],3)} s (added {e['quads_added']}, removed {e['quads_removed']}); "
                 f"full re-materialization {f(e['rematerialize_s'])} s; **total {f(e['resync_plus_rematerialize_s'])} s**; peak RSS {f(e['peak_rss_mb'],0)} MB. Re-materialization is a full recompute (no incremental path used).")
    L += ["", "## 6. Stability", "", f"{len(res['runs'])} consecutive runs (rebuild, materialize, queries, SHACL each in a fresh process). Materialization s: "
          + ", ".join(f(r['materialize'].get('seconds')) for r in res["runs"]) + "; peak RSS MB: " + ", ".join(f(r['materialize'].get('peak_rss_mb'), 0) for r in res["runs"]) + ".", "",
          "## Caveats", "",
          "- Synthetic data; supplier networks are bounded (30 nodes) so transitive closure stays tractable; a real giant connected supply graph would blow up quadratically.",
          "- The supplies 4-hop/`+` queries are cheap by construction (supplier networks of 30 nodes, results <= ~19 rows) and random starts almost never land on the 10 long chains; the mixed Shipment->Port->Shipment->Carrier->Shipment 4-hop (about 57k results) is the heavy one. Long-chain `+` latency was not specifically measured.",
          "- Rebuild runs 2-5 are about 2x run 1 because rebuild first clears the existing asserted+inferred graphs (RocksDB clear_graph is slow: seconds per 1e5 quads). Run 1 hit a store with no inferred graph. Materialization writes only the delta of the inferred graph for the same reason.",
          "- Re-materialization after edits is a full recompute (reasonable's incremental update_graph was not evaluated); at scale 1 it was slower than the first materialization because the delta diff reads the old inferred graph into Python.",
          "- Inferred provenance is graph-level (inferred graph + derivedBy + dataVersion); per-edge rule provenance is not implemented (RDF-star unverified).",
          "- The store holds ids/types/edges only; SHACL property shapes need properties hydrated from Postgres (done in the benchmark).",
          "- Memory/time measured on one laptop with Docker running; page cache not dropped.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
