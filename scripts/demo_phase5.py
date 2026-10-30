"""Phase 5 demo: an agent tests a disruption response inside a what-if scenario, then proposes it.

Run: PYTHONPATH=. uv run python scripts/demo_phase5.py

Headless, mock model (no network, no key). Needs `docker compose up -d postgres`; uses a throw-away schema.
Story: Rotterdam is closed. The planner's agent forks a scenario, optionally runs a short what-if simulation
there with an extra port closure injected (only when `sim.whatif` is available), applies the reroute inside the
scenario, compares it with main and proposes the reroute with the KPI impact. Main is shown unchanged; the
scenario is then discarded (it could instead be merged through POST /scenarios/{id}/merge).
The scripted model proves the platform around the agent, not model judgement.
"""
from __future__ import annotations

import json
import sys

from engine import scenarios
from evals import harness
from evals.world import build_world


def say(t: str = "") -> None:
    print(t)


def main() -> int:
    w = build_world(1)
    try:
        before = w.fingerprint()
        say("Rotterdam is closed; 5 shipments sail via it. Asking the planner's agent to test a response.")
        run = harness.run_agent(w, "planner", "Rotterdam is closed. Test rerouting in a what-if, then propose.",
                                "mock", "scenario_reroute", {"port": "Rotterdam"})
        say(f"\nAgent run {run.result.status}: {run.result.tool_calls} tool calls, tools used: "
            + ", ".join(sorted({c['name'] for c, _r, _d in run.calls()})))
        for line in run.summary().splitlines():
            say("  " + line)
        sid = run.scenario_ids[0]
        say(f"\nScenario {sid} was created by the run; executed there: "
            f"{[e['name'] for e in run.executions if e['scenario_id'] == sid and e['status'] == 'executed']}")
        say(f"Executed on main by the agent: {[e['name'] for e in run.executions if not e['scenario_id'] and e['status'] == 'executed'] or 'nothing'}")
        say(f"Main fingerprint unchanged: {before == w.fingerprint()}")
        p = run.proposals[0]
        imp = ((p["dry_run"] or {}).get("impact") or {}).get("delta", {}).get("kpis", {})
        say(f"Proposal {str(p['id'])[:8]} ({p['action']}) pending; evidence scenario {p['scenario_id']}; "
            f"KPI impact if approved: { {k: v for k, v in imp.items() if v} }")
        # what-if simulation with an injected disruption, if the simulation module is available
        try:
            from sim import whatif  # noqa: F401
            from agent import AgentContext, AgentRun, MockModel, Turn, MemorySink
            from agent import tools as T
            ctx = AgentContext(w.principal("planner"), harness.comp(), w.factory, pipeline=w.pipeline)
            o = T.run_tool(ctx, "scenario_open", {"name": "extra disruption"}, "d1")
            o2 = T.run_tool(ctx, "scenario_simulate", {"days": 3, "injections": [
                {"kind": "shipment_delay", "duration_h": 24}]}, "d2")
            say(f"\nWhat-if simulation in scenario {ctx.sid}: " + (
                f"{o2.envelope['data']['actions_simulated']} simulated actions, digest {o2.envelope['data']['digest'][:12]}"
                if o2.ok else f"unavailable ({o2.error})"))
            o3 = T.run_tool(ctx, "scenario_compare", {}, "d3")
            if o3.ok:
                d = o3.envelope["data"]["kpis"]["delta"]
                say("Compare with main (scenario - main): " + str({k: v for k, v in d.items() if v}))
            ctx.close()
        except ImportError:
            say("\n(sim.whatif is not available: skipping the injected what-if simulation)")
        c = w.factory()
        try:
            for s in scenarios.list_(c, w.principal("planner")):
                scenarios.discard(c, w.principal("planner"), s["id"])
                say(f"Discarded scenario {s['id']} ({s['name']}).")
        finally:
            c.close()
        say("\nDone. Main was never modified; the proposal awaits a human approval.")
        return 0
    finally:
        w.close()


if __name__ == "__main__":
    sys.exit(main())
