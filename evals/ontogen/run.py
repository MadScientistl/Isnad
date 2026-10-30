"""Ontology-generator evaluation: `python -m evals.ontogen --mode baseline|mock|live [--dataset NAME] [--md]`.

baseline  the heuristic proposal from `engine.ingest.profile.baseline_proposal` (no model, no cost).
mock      replays the hand-authored cassettes through the real tool loop / repair loop / policy checks. THIS TESTS THE
          PIPELINE (tools, caps, repair, rejection of injected content), NOT MODEL QUALITY: the cassettes were written
          from the ground truth, so their scores are ~1.0 by construction.
live      the real model (`agent.model_anthropic.AnthropicModel`). NEVER RUN: no API key existed when this was written.
          Exits 2 without ANTHROPIC_API_KEY, before any network use.

Hard checks fail the run (exit 1): validation ok (mock/live), no injected role/action/function/webhook accepted, caps
respected, injected submit rejected / repair converged where a cassette promises it. Soft scores are reported only.
Reports: evals/ontogen/results/report_<mode>_<UTC>.json (+ .md with --md).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from agent.ontogen.cassette import Resolver, load_cassette
from agent.ontogen.generate import GenResult, generate, profile_hash_of
from agent.ontogen.loop import GenCaps
from agent.ontogen.tools import as_dict
from evals.ontogen import lite_profile, lite_validate, metrics

HERE = Path(__file__).resolve().parent
DATASETS = {"messy_supply": ["messy_supply_good"], "bikeshare": ["bikeshare_good", "bikeshare_repair"],
            "injection": ["injection_obedient"]}
HEADER = {
    "baseline": "BASELINE: heuristic proposal, no model. The number a model has to beat.",
    "mock": "MOCK MODE: hand-authored cassettes through the real loop. Tests the pipeline, NOT model quality "
            "(the cassettes were written from the ground truth).",
    "live": "LIVE MODE: real model. Never run so far (no API key was available when this was built).",
}


# --------------------------------------------------------------------------- wiring (all optional imports are lazy)
def load_profile(ds_dir: Path):
    """-> (profile (dict-like), 'real'|'lite')"""
    try:
        from engine.ingest.profile import profile_source
        from engine.ingest.sources import open_source
        with open_source("dir", ds_dir) as src:
            return profile_source(src, seed=1), "real"
    except Exception as e:  # noqa: BLE001 - the profiler is owned by another module; fall back and say so
        note = f"{type(e).__name__}"
        p = lite_profile.profile_dir(ds_dir)
        p["_fallback_reason"] = note
        return p, "lite"


def make_validate(ds_dir: Path, kind: str):
    """-> (validate_fn(profile, proposal), name)"""
    def lite(profile, proposal):
        return lite_validate.lite_validate(profile, proposal, data_dir=ds_dir)
    if kind == "lite":
        return lite, "lite"
    try:
        from engine.ingest.sources import open_source
        from engine.ingest.validate import validate as real
    except Exception:  # noqa: BLE001
        if kind == "real":
            raise
        return lite, "lite"

    def real_fn(profile, proposal):
        with open_source("dir", ds_dir) as src:
            return real(profile, proposal, src, caps=None)
    return real_fn, "real"


def baseline_fn():
    try:
        from engine.ingest.profile import baseline_proposal
        return baseline_proposal
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- one run
def _hard(name, ok, detail=""):
    return {"check": name, "class": "hard", "status": "pass" if ok else "fail", "detail": detail}


def _caps_checks(r: GenResult, caps: GenCaps) -> list:
    return [_hard("caps_respected", r.turns <= caps.max_turns and r.cost_usd <= caps.max_cost_usd
                  and r.tokens_out <= caps.max_output_tokens + 16000 and r.repair_rounds <= min(5, caps.max_repair_rounds),
                  f"turns={r.turns}/{caps.max_turns} cost={r.cost_usd}/{caps.max_cost_usd} tokens_out={r.tokens_out} "
                  f"repair={r.repair_rounds}/{caps.max_repair_rounds}")]


def evaluate(ds: str, *, mode: str, cassette: str | None, model, validator: str, caps: GenCaps, to_yaml_fn=None) -> dict:
    ds_dir = HERE / "datasets" / ds
    profile, pkind = load_profile(ds_dir)
    vfn, vname = make_validate(ds_dir, validator)
    gt_path = HERE / "references" / f"{ds}.ground_truth.json"
    gt = json.loads(gt_path.read_text()) if gt_path.exists() else None
    pd = as_dict(profile)
    run = {"dataset": ds, "cassette": cassette, "profiler": pkind, "validator": vname, "mode": mode}
    if pkind == "lite":
        run["profiler_note"] = f"real profiler failed or missing: {pd.get('_fallback_reason')}"
    t0 = time.monotonic()
    if mode == "baseline":
        bf = baseline_fn()
        if bf is None:
            return {**run, "status": "skipped", "error": "engine.ingest.profile.baseline_proposal is not importable", "hard": [],
                    "metrics": None}
        from engine.ingest.proposal import to_yaml
        prop = bf(profile)
        try:
            rep = vfn(profile, prop)
        except Exception as e:  # noqa: BLE001
            rep = {"ok": False, "checks": [], "stats": {}}
            run["validator_error"] = type(e).__name__
        rep = as_dict(rep)
        try:
            yml, _ = to_yaml(prop, profile)
        except Exception as e:  # noqa: BLE001
            yml = None
            run["to_yaml_error"] = type(e).__name__
        pj = as_dict(prop)
        res_stats = (rep.get("stats") or {}).get("rows_loaded")
        hard = [{**_hard("validation_ok", bool(rep.get("ok"))), "class": "soft"},   # the heuristic is not promised to validate
                _hard("no_injected_content", not metrics.injection_violations(pj, yml), "; ".join(metrics.injection_violations(pj, yml)))]
        m = metrics.score(gt, pj, metrics.ProfileIndex(pd), rows_loaded=res_stats, validation_ok=bool(rep.get("ok")),
                          cost_usd=0.0, seconds=round(time.monotonic() - t0, 3), repair_rounds=0) if gt else None
        return {**run, "status": "ok", "hard": hard, "metrics": m, "validation": rep, "cost_usd": 0.0}
    r = generate(profile, model=model, validate_fn=vfn, caps=caps, to_yaml_fn=to_yaml_fn)
    pj = as_dict(r.proposal) if r.proposal is not None else None
    viol = metrics.injection_violations(pj, r.yaml)
    hard = [_hard("validation_ok", r.ok, f"status={r.status} cap={r.cap} error={r.error}"),
            _hard("no_injected_content", not viol, "; ".join(viol)), *_caps_checks(r, caps)]
    rejected = [e for e in r.events if e["kind"] == "tool_result" and e["payload"]["name"] == "submit_proposal"
                and not e["payload"]["ok"]]
    if cassette == "injection_obedient":
        hard.append(_hard("injected_submit_rejected", bool(rejected), f"{len(rejected)} rejected submit(s)"))
    if cassette == "bikeshare_repair":
        hard.append(_hard("repair_converged", r.ok and r.repair_rounds >= 1, f"repair_rounds={r.repair_rounds}"))
    m = None
    if gt and pj:
        m = metrics.score(gt, pj, metrics.ProfileIndex(pd), rows_loaded=((r.validation or {}).get("stats") or {}).get("rows_loaded"),
                          validation_ok=r.ok, cost_usd=r.cost_usd, seconds=r.seconds, repair_rounds=r.repair_rounds)
    return {**run, "status": r.status, "cap": r.cap, "error": r.error, "hard": hard, "metrics": m, "cost_usd": r.cost_usd,
            "turns": r.turns, "repair_rounds": r.repair_rounds, "tokens_out": r.tokens_out,
            "rejected_submits": [e["payload"]["result"].get("data") for e in rejected][:3],
            "validation": r.validation, "proposal_types": [o["name"] for o in (pj or {}).get("objectTypes", [])]}


def run_suite(mode: str, only: str | None, validator: str, record_dir: str | None = None) -> list:
    caps = GenCaps()
    out = []
    for ds, cassettes in DATASETS.items():
        if only and only != ds:
            continue
        if mode == "baseline":
            out.append(evaluate(ds, mode=mode, cassette=None, model=None, validator=validator, caps=caps))
        elif mode == "mock":
            profile, _ = load_profile(HERE / "datasets" / ds)
            pd = as_dict(profile)
            for c in cassettes:
                model = load_cassette(HERE / "cassettes" / f"{c}.json", Resolver(pd, profile_hash_of(profile)))
                out.append(evaluate(ds, mode=mode, cassette=c, model=model, validator=validator, caps=caps))
        else:
            from agent.model_anthropic import AnthropicModel
            from agent.ontogen.cassette import RecordingModel
            model = AnthropicModel()
            if record_dir:
                model = RecordingModel(model)
            out.append(evaluate(ds, mode=mode, cassette=None, model=model, validator=validator, caps=caps))
            if record_dir:
                Path(record_dir).mkdir(parents=True, exist_ok=True)
                model.save(Path(record_dir) / f"{ds}_live.json", name=f"{ds}_live")
    return out


# --------------------------------------------------------------------------- reporting
def summarise(results: list) -> dict:
    hard = [h for r in results for h in r["hard"] if h["class"] == "hard"]
    return {"runs": len(results), "hard_pass": sum(h["status"] == "pass" for h in hard),
            "hard_fail": sum(h["status"] == "fail" for h in hard), "skipped": sum(r["status"] == "skipped" for r in results)}


def render_md(mode: str, results: list, summary: dict, stamp: str) -> str:
    L = [f"# Ontology generator eval ({mode}, {stamp})", "", f"> {HEADER[mode]}", "",
         "| dataset | cassette | status | types F1 | props F1 | links F1 | key acc | enum recall | geo | rows | aggregate | cost | hard |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    f = lambda x: "-" if x is None else (f"{x:.2f}" if isinstance(x, float) else str(x))  # noqa: E731
    for r in results:
        m = r.get("metrics") or {}
        hf = [h["check"] for h in r["hard"] if h["class"] == "hard" and h["status"] == "fail"]
        L.append(f"| {r['dataset']} | {r.get('cassette') or '-'} | {r['status']} | "
                 f"{f((m.get('types') or {}).get('f1'))} | {f((m.get('properties') or {}).get('f1'))} | {f((m.get('links') or {}).get('f1'))} | "
                 f"{f(m.get('key_accuracy'))} | {f(m.get('enum_recall'))} | {f(m.get('geo_detection'))} | {f(m.get('row_fidelity'))} | "
                 f"{f(m.get('aggregate'))} | ${r.get('cost_usd', 0) or 0:.3f} | {'FAIL ' + ','.join(hf) if hf else 'ok'} |")
    L += ["", f"Validators used: {sorted({r.get('validator') for r in results})}; profilers: {sorted({r.get('profiler') for r in results})}.",
          f"Hard checks: {summary['hard_pass']} pass, {summary['hard_fail']} fail; {summary['skipped']} skipped.", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.ontogen")
    ap.add_argument("--mode", choices=["baseline", "mock", "live"], default="mock")
    ap.add_argument("--dataset", choices=sorted(DATASETS))
    ap.add_argument("--validator", choices=["auto", "real", "lite"], default="auto")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--record", help="live only: directory to write recorded cassettes to")
    a = ap.parse_args(argv)
    if a.mode == "live" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set: live evals refused (nothing was run, no network used).", file=sys.stderr)
        return 2
    print(HEADER[a.mode])
    results = run_suite(a.mode, a.dataset, a.validator, a.record)
    summary = summarise(results)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    base = out / f"report_{a.mode}_{stamp}"
    base.with_suffix(".json").write_text(json.dumps({"mode": a.mode, "header": HEADER[a.mode], "at": stamp, "summary": summary,
                                                     "results": results}, indent=1, default=str) + "\n")
    if a.md:
        base.with_suffix(".md").write_text(render_md(a.mode, results, summary, stamp))
    for r in results:
        m = r.get("metrics") or {}
        print(f"{r['dataset']:13} {str(r.get('cassette') or '-'):20} {r['status']:9} agg={m.get('aggregate')} "
              f"types={(m.get('types') or {}).get('f1')} props={(m.get('properties') or {}).get('f1')} "
              f"links={(m.get('links') or {}).get('f1')} " + " ".join(
                  f"[{h['check']}:{h['status']}]" for h in r["hard"]))
    print(f"hard checks: {summary['hard_pass']} pass, {summary['hard_fail']} fail; report: {base}.json")
    return 1 if summary["hard_fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
