"""Eval runner: `python -m evals --mode mock` (default) or `--mode live`.

MOCK MODE TESTS THE PLATFORM, NOT THE MODEL. A scripted plan-following model issues fixed tool calls; the cases
check that permissions, masking, hidden objects, allowlists, caps, approval gating, evidence labelling and audit
behave correctly around that behaviour. Passing mock evals says nothing about a real model's judgement.
LIVE MODE runs the same prompts against the real model (needs ANTHROPIC_API_KEY, tiny caps, cheap model via
AMARH_EVAL_MODEL, default claude-haiku-5-5) and applies the same assertions (scripted-only ones are skipped).

Assertion classes (RESEARCH.md section 7): hard guardrail (exit code 1 when any fails), soft guidance (reported),
telemetry (recorded). A JSON report is written to evals/results/.
"""
from __future__ import annotations

import argparse
import sys
import textwrap

from evals import harness

HEADER = {
    "mock": "MOCK MODE: scripted plans. Tests platform enforcement and plumbing, NOT model judgement.",
    "live": "LIVE MODE: real model, tiny caps. Hard guardrails must hold; soft results are model behaviour.",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals")
    ap.add_argument("--mode", choices=["mock", "live"], default="mock")
    ap.add_argument("--case", help="substring of a case id")
    ap.add_argument("--seeds", default="1", help="comma-separated world seeds, e.g. 1,2,3")
    ap.add_argument("--md", action="store_true", help="also write a markdown report next to the JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every assertion")
    a = ap.parse_args(argv)
    try:
        seeds = [int(x) for x in a.seeds.split(",") if x.strip()]
    except ValueError:
        ap.error("--seeds must be comma-separated integers")
    if a.mode == "live" and not harness.have_key():
        print("ANTHROPIC_API_KEY is not set: live evals refused (nothing was run, no network used).", file=sys.stderr)
        return 2
    print(HEADER[a.mode])
    results = harness.run_suite(a.mode, a.case, seeds)
    print(f"\n{'case':38} {'seed':>4} {'role':18} {'status':10} {'hard':>7} {'soft':>7}  tel")
    for r in results:
        def cnt(cls):
            os_ = [o for o in r["outcomes"] if o["class"] == cls and o["status"] != "na"]
            return f"{sum(o['status'] == 'pass' for o in os_)}/{len(os_)}"
        mark = "FAIL" if any(o["class"] == "hard" and o["status"] == "fail" for o in r["outcomes"]) else "ok"
        print(f"{r['id']:38} {r['seed']:>4} {r['role']:18} {r['status']:10} {cnt('hard'):>7} {cnt('soft'):>7}  "
              f"{r['telemetry']['tool_calls']} calls ${r['telemetry']['cost_usd']:.3f} {r['latency_s']:.2f}s  {mark}")
        for o in r["outcomes"]:
            if a.verbose or o["status"] == "fail":
                print(f"    [{o['class']:9}] {o['status']:4} {o['check']}: {textwrap.shorten(o['detail'], 150)}")
    s = harness.suite_summary(results)
    path = harness.write_report(results, a.mode, md=a.md)
    print(f"\nhard guardrails: {s['hard']['pass']} pass, {s['hard']['fail']} fail, {s['hard']['na']} n/a | "
          f"soft guidance: {s['soft']['pass']} pass, {s['soft']['fail']} fail, {s['soft']['na']} n/a | "
          f"telemetry: {s['telemetry']['pass']} recorded")
    print(f"{len(results)} case runs (seeds {seeds}); report: {path}")
    return 1 if s["hard"]["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
