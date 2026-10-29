# Open Ontology Platform (Foundry/AIP-inspired): Research and Build Plan

**Scope (your decisions):** simulated supply chain, geospatial events and finance ops data; local-first open source; formal reasoning is a goal; the graph carries the semantic layer; the agent proposes actions, a human approves, the agent executes them (internal writeback, external calls, or both, fully audited); agent permissions are bounded by the user's permissions; the agent can reason over current and historical values.

**Evidence note:** most sources are third-party write-ups, vendor pages, or papers. Where I found nothing, I say so. Nothing here has been built or benchmarked.

---

## 1. What Foundry's ontology is

Sources: [Core concepts](https://www.palantir.com/docs/foundry/ontology/core-concepts), [Foundry summary](https://palantir.com/docs/foundry/getting-started/foundry-platform-summary-llm/), [explainer](https://www.puppygraph.com/blog/palantir-ontology).

| Layer | Foundry concept | What we build |
|---|---|---|
| Semantic | Object types, properties, link types, interfaces, value types | OWL ontology + SHACL shapes (formal, reasoned over) |
| Data | Datasets backing objects, indexed object store, time series | Postgres/PostGIS, bitemporal |
| Kinetic | **Action types**: typed parameters, submission criteria, rules, writeback and side-effect webhooks | Transactional action engine with audit and webhook outbox |
| Logic | Functions (TypeScript/Python) | Sandboxed Python functions callable from UI and agent |
| Security | Roles, markings, object- and property-level restrictions | RBAC + row-level security + property masking, enforced in the API |
| Apps | Object Explorer, Workshop, Vertex, Map, Quiver | React app: explorer, 360 view, map, graph, dashboards, scenarios |
| AI (AIP) | Ontology-grounded agents, Logic; tools are queries, functions, actions | Claude tool-use agent, approval inbox, audit trail |

Writeback semantics to copy: writeback runs inside the action transaction and rolls back on failure; side-effect webhooks run after commit and do not roll back (secondary source).

Important difference from Foundry: Foundry's ontology is an operational layer (objects, links, actions) with little formal inference. You asked for formal reasoning, so we add an OWL/SHACL semantic layer on top of the operational core. That is a deliberate extension, not a Foundry copy.

## 2. Goals (validated and extended)

Your original goals stand: full-featured ontology, a rich dashboard UI (maps etc.), automatic ontology generation, and an approval-gated agent. Added and agreed:

1. **Action types as the only write path** for agents.
2. **Permissions and governance** at definition and instance level; the agent runs as the approving user, capped by an agent allowlist.
3. **Audit and lineage**: append-only record of proposals, approvals, executions, diffs, rollbacks, webhooks.
4. **Simulation engine and scenarios** (what-if forks).
5. **Evaluation harness** for agent behaviour.
6. **Ontology versioning** with proposals and diffs.
7. **Formal reasoning** (new): OWL 2 RL-style inference plus SHACL validation.
8. **Temporal history** (new): bitemporal data so the agent can compare old and new values and ask "as of" questions.
9. **Multi-hop graph traversal** as a first-class capability with per-hop permission filtering.

---

## 3. Architecture decision: three roles, two stores

```
                 YAML ontology (single authoring source)
                         │ compiler
      ┌──────────────────┼───────────────────────┐
      ▼                  ▼                        ▼
 Postgres DDL     OWL + SHACL (Turtle)      API / SDK / action defs
      │                  │
┌─────▼──────────┐  ┌────▼───────────────────────────┐
│ PostgreSQL +   │  │ Semantic graph store (Oxigraph) │
│ PostGIS        │  │  - ontology (classes, links)    │
│ source of truth│─▶│  - instance IDs + edges         │
│ bitemporal     │  │  - inferred edges (flagged)     │
│ RLS, audit,    │  │  derived; rebuildable           │
│ actions, outbox│  └──────────┬──────────────────────┘
└─────┬──────────┘             │ reasoner (OWL 2 RL), SHACL
      │                        │
      └──────────┬─────────────┘
                 ▼
        FastAPI engine ── permission filter ── Agent / UI
```

- **Postgres is the source of truth for instance data, transactions, history, permissions and audit.** Actions need one atomic transaction plus row-level security, which is hard to get in a triplestore.
- **The semantic graph carries the semantic layer** (your requirement): classes, subclasses, property domains and ranges, link types, inverses, transitivity, and the instance graph (ids and edges). It is a derived store that can be rebuilt from the YAML ontology plus Postgres, so losing it never loses data.
- **Reasoning and SHACL run against the semantic graph;** results come back as flagged inferred edges and validation reports.

**Change from the previous draft:** I dropped Apache AGE. With an RDF store carrying the semantics and reasoning, AGE would be a third store doing overlapping work. Multi-hop traversal uses SPARQL property paths in the semantic graph (and recursive SQL in Postgres as a fallback). AGE research is kept in the appendix in case you want it back.

## 4. Reasoning layer research

### 4.1 Reasoners

| Option | Findings | Fit |
|---|---|---|
| **reasonable** (Rust, Python bindings) | OWL 2 RL reasoner, `pip install reasonable`; its own benchmark claims it is about 38x faster than OWLRL and 7x faster than Allegro on a building-model workload (self-reported) ([PyPI](https://pypi.org/project/reasonable/0.1.49_alpha9)). OntoLogos' self-assessment calls it the incumbent for RL materialization in Rust/Python ([comparison](https://ontologos.readthedocs.io/en/latest/comparison/)). Versions on PyPI are alphas. | **First choice** for embedded OWL 2 RL materialization. Risk: alpha maturity. |
| **owlrl** (Python, rdflib) | Slower per the same benchmark; used by pySHACL for pre-inferencing. | Fallback and reference for correctness checks. |
| **Apache Jena** | Fuseki supports configured rule-based inference; an old Jena doc describes translating OWL 2 RL into JenaRules ([doc](https://svn.apache.org/repos/asf/jena/branches/jena-owl2/doc/inference/OWL2RLExpt.html)). Not a full OWL 2 DL reasoner ([ESWC 2023 paper](https://ceur-ws.org/Vol-3443/ESWC_2023_DMKG_paper_2861.pdf)). JVM. | Option if we need a server with SHACL and rules. Not benchmarked. |
| **GraphDB Free, RDFox, Stardog** | GraphDB advertises efficient reasoning; RDFox is commercial RL/Datalog ([comparison](https://ontologos.readthedocs.io/en/latest/comparison/)). I found no head-to-head numbers or current licence terms. | Not planned (licence/openness unverified). |
| **HermiT and DL reasoners** | HermiT listed as stagnant in the OntoLogos comparison. | Avoid. Full DL reasoning is also costly. |

### 4.2 Semantic graph store

| Option | Findings | Fit |
|---|---|---|
| **Oxigraph** | Rust, RocksDB-backed, in-memory or disk, SPARQL 1.1 Query/Update/Federation and Protocol server, Apache 2.0 or MIT, Python bindings (`pyoxigraph`), rdflib adapter `oxrdflib` ([overview](https://www.linuxlinks.com/oxigraph-graph-database/)). Preliminary SPARQL/RDF 1.2 support only; **I could not confirm SHACL or RDF-star support**. | **Chosen** as embedded store. Validation done by pySHACL. |
| **Apache Jena TDB/Fuseki** | Mature, JVM, SHACL support (per a SHACL review that lists it among SHACL-enabled stores). | Fallback if Oxigraph proves too limited. |
| **Ontop** (virtual KG) | Answers SPARQL over relational DBs by rewriting to SQL, R2RML mappings, RDFS and **OWL 2 QL** only, Apache 2.0, version 5.5.0 (Feb 2026) ([guide](https://ontop-vkg.org/guide/), [repo](https://github.com/ontop/ontop/)). | Rejected as the main path: OWL 2 QL does not give the rule-style inference (e.g. transitive chains) we want. Could be an add-on for read-only SPARQL over Postgres. |

### 4.3 Validation (SHACL)

- pySHACL runs in pure Python on rdflib and can pre-inference with `owlrl`; a 2026 benchmark found it the slowest of the SHACL engines tested (Jena about 4.2x faster, TopBraid about 3.3x), and with high variance ([TIO-SHACL](https://arxiv.org/pdf/2604.27359)). At our synthetic data size this is acceptable; run it on changed subgraphs, not the whole store.
- Postgres constraints remain the hard gate on writes; SHACL covers semantic constraints (e.g. "a Shipment's route ports must belong to its carrier's network").

### 4.4 What reasoning will and won't do

- **Will:** subclass/subproperty inference, domain and range typing, inverse and symmetric properties, transitive properties (e.g. `supplies` chains, `contains` hierarchies), property chains, equivalence. These cover impact analysis: "who is indirectly exposed to Port X".
- **Won't (v1):** full OWL DL (cardinality reasoning, complex class expressions), probabilistic reasoning, anything open-world and heavy.
- **Business rules** like "shipment at risk if its port is closed and ETA slack is under 2 days" are **not** OWL: implement them as Python functions or SPARQL rules whose output is stored as derived properties with provenance.
- **Provenance:** every inferred edge is flagged inferred, with the rule and the data version it came from. Humans and the agent can tell asserted from inferred facts. The agent should say which it relied on.
- **Operations:** the "current" inferred graph is re-materialized incrementally when actions change data (initially a full recompute per batch; measure before optimizing).

### 4.5 Sync from Postgres to the semantic graph

- A single path: the action engine commits to Postgres, then writes a change record to an outbox; a sync worker applies the delta (ids, types, edges) to the semantic graph and triggers incremental reasoning. If the graph store lags or fails, Postgres is still correct; the graph is rebuilt from scratch on demand.
- I found nothing in my searches on incremental materialization strategies for this setup, so start simple (recompute per affected subgraph) and benchmark.

## 5. Temporal model research

**Pattern: bitemporal tables in Postgres.** Two time axes: valid time (when a fact is true in the world) and transaction time (when the system recorded it). The pg_bitemporal approach uses two range columns with two exclusion constraints keyed on the entity, using `btree_gist` (source: tutorials summarized from [Simple Talk](https://www.red-gate.com/simple-talk/databases/postgresql/saving-data-historically-with-temporal-tables-part-1-queries) and [Better Stack](https://betterstack.com/community/guides/databases/postgres-temporal-constraints/)). Updates are close-and-insert; deletes close the validity period. PostgreSQL 18 adds `WITHOUT OVERLAPS` and temporal foreign keys (`PERIOD`) per one guide; I did not verify against the official release notes, so check that before depending on it. Known cost: applications carry much of the burden of splitting and stitching ranges, so put that logic in one engine function.

**Pattern: bitemporal knowledge graph edges.** Graphiti (Zep) keeps bitemporal validity windows on graph edges and invalidates superseded facts instead of overwriting them ([Thoughtworks Radar](https://www.thoughtworks.com/en-us/radar/platforms/graphiti), [Zep paper](https://arxiv.org/pdf/2501.13956.pdf)). It is pre-1.0, uses per-write LLM extraction and a Neo4j backend, so study the model rather than adopt it.

**Not found:** I found nothing on RDF-star or named-graph versioning in my searches. For the RDF side, plan to model change as explicit event/state objects rather than relying on those features.

**Our design**
- **Postgres:** every object-type table has version rows: `valid_during tstzrange`, `recorded_during tstzrange`, `changed_by`, `action_id`, `scenario_id`. A current view selects the open rows. Exclusion constraints prevent overlaps per entity.
- **Links** carry the same two ranges.
- **Semantic graph:** holds the **current** state (edges without history) for fast traversal and reasoning. History is queried in Postgres and, when needed, a historical snapshot graph is built on demand for an `as_of` timestamp (cached).
- **Derived facts** (inferred edges, risk scores) are stamped with the data version they were computed from. A fact computed from state T1 is not silently reused at T2.
- **Agent tools:** `get_history(object, property)`, `as_of(ts)`, `diff(t1, t2)`, `what_changed_since(ts)`. Example: "Why did risk rise?" compares the object's neighbours between two timestamps.
- **Permissions apply to history:** losing access to an object also removes access to its past versions.
- **Scenarios:** a scenario is a branch overlay keyed by `scenario_id`, reusing the same version machinery.

## 6. Agent actions, permissions and audit

- Foundry separates *definition-layer* permissions (who can view or edit types) from *instance-layer* permissions (which objects and which property values) ([analysis](https://www.besthub.dev/articles/don-t-give-your-ai-agent-a-super-admin-palantir-ontology-permission-design-ae5fb6b3f9fa)). Build both.
- In AIP Logic, read-time security limits what the model reads, but not what it outputs or edits ([FDE guide](https://fde.academy/blog/palantir-aip-explained-what-it-is-and-how-fdes-use-it)); so permissions are re-checked in the action engine at execution.
- Edits by an agent can be applied automatically or staged for human approval; the pattern that survives audit is propose, then a named human approves anything with financial or customer impact ([guide](https://www.bdemerson.com/article/what-is-palantir-aip)).
- **Decision:** the agent runs as the approving user, intersected with a per-agent action allowlist (never a union). Some argue for task-scoped permissions instead of identity inheritance to limit blast radius ([source](https://www.besthub.dev/articles/don-t-give-your-ai-agent-a-super-admin-palantir-ontology-permission-design-ae5fb6b3f9fa)); the allowlist gives us that.
- Gateway pattern: deny-by-default tool gate, blocking or audit-only approval modes, every request, decision and response recorded in Postgres ([patterns](https://www.agno.com/changelog/enforce-human-approval-gates-in-agentic-workflows)).
- Postgres: least-privilege agent role, `ROW LEVEL SECURITY` with `FORCE`, statement timeouts, read-only defaults ([reference](https://pgxn.org/dist/pg_policy/docs/research/03-agentic-ai-guardrails.html)). I found nothing on OPA or Casbin, so v1 uses an in-app policy engine plus RLS.
- **Audit:** append-only tables for proposal, approval, execution, before/after diff, rollback, webhook delivery. Each row records actor, on-behalf-of user, agent run id and tool trace. RLS on audit and approval tables with no public policy.

### Multi-hop permission filtering (main security risk)
- A [SurrealDB advisory](https://radar.offseq.com/threat/surrealdb-graph-traversal-bypasses-table-select-permissions-cve-2026-63746-b19dddfa40ff980a) (CVE-2026-63746) describes traversals skipping the target table's permission check, exposing anything reachable from a readable start node.
- Pattern from graph-authorization material: check every vertex on the path and drop inaccessible ones; keep authorization decisions bounded and explicit while allowing discovery ([source](https://nhimg.org/faq/what-should-iam-teams-do-when-permission-logic-depends-on-many-graph-hops/)). Sources are vendor blogs and patents; nothing specific to LLM agents.
- **Design:** the semantic graph stores ids, types and edges only (no sensitive properties). Every traversal and every SPARQL result passes through the permission filter before leaving the API; an inaccessible node cuts the path. Depth, fan-out and time limits on every query. **Inferred edges are filtered too:** an inferred fact is shown only if the user can see all the facts it was derived from (conservative rule). A dedicated test suite tries to reach hidden objects via multi-hop and inferred paths.

## 7. Simulation, scenarios, evaluation

- **Simulation:** [SupplyNetPy](https://arxiv.org/pdf/2607.09745) (SimPy-based; node and link disruptions, stochastic demand and lead times, random topologies) is the first library to evaluate. Papers exist on turning disruption text into SimPy scenarios ([Aston](https://research.aston.ac.uk/en/publications/a-supply-chain-disruption-framework-for-discrete-event-simulation)). Design: a seeded SimPy world writes events into the ontology as ticks advance, through the same action path.
- **Scenarios:** Foundry's [ontology scenarios](https://palantir.com/docs/foundry/ontology/overview-ontology-scenario/) fork the ontology for what-if analysis with compare and merge. Ours: overlay rows with `scenario_id`; agent and actions run inside a scenario; compare and optionally merge. Dry-run previews of proposals run in a scenario.
- **Evaluation:** loop of observe, pick step, validate, call tool, record; cases include duplicate invoices, missing approvals and ambiguous requests that must hand off to a human. Split outcomes into hard guardrail, soft guidance and telemetry. Seeded scenarios with golden assertions (right action proposed, no forbidden action, permission denials respected, no double execution, correct use of history and inferred-vs-asserted facts). I found no ready-made tool-use eval framework, so this is a small custom harness.
- **Versioning:** ontology definitions are versioned with proposals, diffs and rollback (Foundry Branching covers Ontology Manager and pipelines ([docs](https://palantir.com/docs/foundry/foundry-branching/supported-functionality))). Data branching is handled by scenarios.

## 8. Automatic ontology generation (agent)

Yes, an agent can do it with a human review gate. Pipeline:
1. **Deterministic profiling:** types, keys, uniqueness, foreign keys (declared and inferred by value overlap), enums, geo and time columns, cardinality.
2. **LLM proposal:** object types, properties, link types with cardinality, class hierarchy, inverse/transitive hints, suggested actions and interfaces.
3. **Validation loop:** generate OWL/SHACL and mappings, test them against the data (do links resolve, do keys hold, does SHACL pass, is the reasoner consistent), repair.
4. **Human review:** schema diff UI; approved result becomes a versioned YAML ontology.

Related work: [RIGOR](https://arxiv.org/abs/2506.01232) (LLM builds OWL from relational schemas table by table with a judge LLM), Montage (human-in-the-loop schema-to-R2RML mapping), OntoGenix (CSV to OWL/RML; stale). None targets an operational Foundry-style ontology, so this part is ours.

## 9. Implementation design

**Repository layout:** `ontology/` (YAML), `engine/` (compiler, object API, action engine, policy, audit, sync, reasoning), `sim/`, `agent/` (tools, approval queue, runner), `evals/`, `web/`, `sdk/`.

**Authoring format:** one YAML ontology: object types, properties, link types (cardinality, inverse, transitive flags), class hierarchy, interfaces, actions, functions, permissions. The compiler emits: (a) Postgres DDL with bitemporal columns, (b) OWL Turtle + SHACL shapes, (c) API routes and Pydantic models, (d) TypeScript SDK. YAML is exported on every change, so the semantic graph is always rebuildable.

**Postgres:** registry tables (versioned definitions), `obj_<type>` and `link_<type>` bitemporal tables with `scenario_id`, governance tables (`users`, `roles`, `policies`, `markings`), `proposal`, `approval`, `execution`, `audit_event`, `webhook_outbox`, `graph_outbox`.

**Action flow:** validate params; load action type; check submission criteria and permissions for the effective user (approver); open transaction; apply rules (close old versions, insert new, update links); write audit with before/after diff; commit. Then the outbox delivers webhooks (retries, idempotency key) and the sync worker updates the semantic graph and triggers reasoning. Webhook failure doesn't roll back data.

**Agent loop (Claude tool use):** tools `query_objects`, `traverse` (depth/fan-out capped, permission-filtered), `find_paths`, `impact_analysis`, `get_history`, `as_of`, `diff`, `run_function`, `propose_action`. Reads run as the user. `propose_action` only writes a proposal with rationale, affected objects, which facts were inferred vs asserted, and a dry-run diff from a scenario. Approval triggers execution as the approver, capped by the allowlist. Every tool call and message is stored under the run id.

**Frontend pages:** Object Explorer, Object 360 (properties, links, timeline with history, actions), Map (MapLibre/deck.gl), Graph (Cytoscape; asserted and inferred edges styled differently), Dashboards, Approval Inbox, Agent console, Audit log, Time slider (as-of view), Scenario manager, Ontology manager (schema diff and review), Simulation controls.

**Stack:** PostgreSQL 18 + PostGIS (+ `btree_gist`); DuckDB for analytics; Oxigraph via `pyoxigraph`; `reasonable` (fallback `owlrl`); pySHACL; FastAPI; SimPy; React + Vite + TypeScript; MapLibre/deck.gl; Cytoscape; ECharts; shadcn/ui; Claude via API.

## 10. Build order (each phase ends in a demo)

1. **Foundations + feasibility gate.** YAML compiler, Postgres bitemporal tables, object/link API, explorer UI, Oxigraph sync. **Gate:** load a synthetic graph (about 100k objects, 500k edges), run `reasonable` materialization, 4-hop SPARQL path queries, and SHACL on a changed subgraph; record times. If too slow or unstable, switch to Jena or reduce inference scope.
2. **Simulation and visuals.** SimPy world, map, graph view, dashboards, time slider.
3. **Governance and actions.** Roles, RLS, property masking, action engine, audit, outbox, multi-hop and inferred-edge permission test suite.
4. **Agent.** Tools, approval inbox, execute-as-approver, allowlist, history tools.
5. **Scenarios and evals.** What-if branches, dry-run previews, eval harness (including history and inference cases).
6. **Auto-generation agent** and polish.

## 11. Risks

- **Reasoner maturity:** `reasonable` ships as alphas; Oxigraph's SHACL/RDF-star status is unconfirmed. Mitigated by the Phase 1 gate and fallbacks (owlrl, Jena).
- **Inference cost and surprise:** limit to OWL 2 RL, measure, keep business rules as functions, always label inferred facts.
- **Permission leaks through multi-hop and inferred edges:** filtering at the API boundary plus the dedicated test suite.
- **Dual stores drifting:** one writer path (Postgres first, then outbox), graph rebuildable.
- **Temporal complexity:** all range splitting in one engine function; verify PostgreSQL 18 temporal feature claims against official docs.
- **Scope:** Foundry is huge; one vertical slice per phase.

## 12. Open questions

Resolved: Claude (API) is the agent model; v1 uses a UI role switcher (no real login). The backend still enforces permissions per request from the selected role, so real auth can be added later without changing the engine.

Remaining: start Phase 1 now?

## Appendix: Apache AGE (previously chosen, now dropped)

AGE (openCypher inside Postgres) is an Apache top-level project (Apache 2.0), runs on Postgres 11–18; v1.7.0 (Jan 2026) added Row Level Security; releases are sparse (v1.5.0 Jan 2024, v1.6.0 Sep 2025); no benchmarks or documented RLS caveats found ([release notes](https://age.apache.org/release-notes)). Dropped because the RDF store now carries the semantic layer and reasoning, making a third store redundant. Memgraph (Business Source License per one unverified page) and Neo4j Community (no clustering; auditing in enterprise per competitor pages) remain possible fallbacks if SPARQL traversal proves inadequate; both would need a Postgres sync.
