# Demo

Two ways to show Isnad: the scripted headless demo (always works, no key) and a click path in the UI.

## 1. Scripted end-to-end demo

```bash
docker compose up -d postgres        # or: make setup (once)
make demo                            # = uv run python scripts/demo_phase6.py --model mock
```

About 20-30 seconds. It is deterministic (seeded; the simulation digest is identical between runs), uses throw-away schemas
(`demo6_<random>` and the imported `ws_<slug>`) and temp directories, and cleans up. It drives the real FastAPI app in the
default role-switcher mode: the role is the `x-role` header, the identity behind a role is `demo-<role>`.

| Step | What it shows |
|---|---|
| (a) | Seeds the supply chain, simulates 7 days with an injected Rotterdam closure through the action engine (audited) |
| (b) | The planner's agent (scripted plan) queries, traces impact, calls a read-only function and creates a **proposal** with evidence. Nothing is executed |
| (c) | The launcher cannot approve (403). An admin approves; the action runs as the approver; the audit row and diff are read back |
| (d) | A what-if scenario with an extra port closure is simulated, compared with main (KPI deltas), main is shown unchanged, the scenario is discarded |
| (e) | Bitemporal history of the port and an as-of read before the closure |
| (f) | Upload of the bike-share dataset, profile, proposal, scratch-schema validation, approval, audited import into `ws_<slug>` |
| (g) | The workspace is read back through the engine: object counts, a geo query, a graph neighbourhood, and a non-admin role that can read but not write |

Options:

```text
--days N                simulated days for step (a) (default 7)
--seed N                simulation seed (default 1)
--model baseline|mock|live
                        import proposal: heuristic baseline (default when run directly), replay of
                        evals/ontogen/cassettes/bikeshare_good.json (make demo), or the real generator
--live                  real Claude for the agent (and for the import unless --model is given)
--keep                  keep the scratch schemas and temp directory for inspection
--quiet                 only the final summary
```

`--live` and `--model live` exit with status 2, before any network use, when `ANTHROPIC_API_KEY` is not set. Exit status 1 means a
step's assertion failed; the summary names it. Set `DEMO_TRACE=1` for tracebacks. `tests/demo/test_demo_smoke.py` runs it with 2 sim days.

What this does not prove: the scripted agent and the cassette show the platform around a model (tools, permissions, proposals,
validation, import), not model judgement. Without auth, "a different admin" is the planner/admin identity pair; the session creator
and the approver of an import are the same `demo-admin` (separation of duties needs the experimental token login).

## 2. Live UI demo (click path, about 10 minutes)

The click path was written from the code, not rehearsed in a browser; check the labels once before presenting.

Prepare: `make setup`, then `make dev-import`, open <http://localhost:5173>. In the admin role start the simulation from the
time bar (speed 24) for a minute, then pause it. Bind stays on `127.0.0.1`; do not expose the port (no authentication, see README).

1. **Dashboard.** Role switcher = `planner`. KPIs, shipments and ports. Switch to `finance_analyst`: invoices are visible, the planner could not see them.
   Switch to `regional_ops_lead`: only EMEA rows. This is row-level security and masking in Postgres, not UI hiding.
2. **Map and Graph.** Ports and shipments on the map; open a shipment, then Graph: neighbours, and the inferred `supplies`
   edges drawn differently (inferred, with provenance).
3. **Disruption.** As `admin`, inject a port closure (time bar / sim controls) or close a port in Actions. Watch affected shipments turn delayed.
4. **Agent.** As `planner`, open Agent, run "Rotterdam is closed, assess the exposure and propose a response". Without
   `ANTHROPIC_API_KEY` the run answers 503; start the API with a key for this step, or show the demo script's step (b) instead.
   Show the tool trace and the proposal card with evidence.
5. **Approvals.** Switch to `admin`, open Approvals, read the dry-run impact, approve. Open Audit: execution, diff, actor, proposal link.
   Roll the execution back to show it is reversible.
6. **Scenarios.** As `planner`, create a scenario, run a what-if with an injected closure, open Compare (KPI deltas, changed
   objects), show that main is unchanged, discard or merge.
7. **History.** Explorer, open a port, the history tab: versions over valid time and recorded time; set the time slider into the past.
8. **Import.** As `admin`, open Ontology, upload `evals/ontogen/datasets/bikeshare` (zip the CSVs first), create a **baseline**
   proposal (or **model** with a key), walk the profile, the types and links, the validation report and the generated roles,
   approve, import. Then in a second terminal `scripts/serve_workspace.sh <slug> 8001` and use the workspace picker to browse the
   new ontology: stations on the map, the graph of trips. Try `messy_supply` to show how dirty data is reported by validation.

Talking points: Postgres is the single source of truth and the graph is derived; every write is an action with an audit
row; the agent proposes and a human approves; permissions are enforced in the database; generated ontologies go through
validation in a scratch schema and a human before anything is loaded.
