# Amarh: Problem and Solution Statements

## Problem statement

Enterprises want AI agents that act on operational data (supply chain, geospatial events, finance operations), not just answer questions about it. Four gaps keep agents from being trusted with that work:

1. **No shared meaning.** Operational data sits in tables, documents and APIs with no common model of what things are and how they relate. An agent has to guess relationships, and it misses indirect effects such as "which customers are exposed if Port X closes" because nothing encodes that supply chains are transitive.
2. **No sense of time.** Systems keep only the latest value. An agent cannot say what was true at a past date, what changed, or why a risk rose, so its explanations cannot be checked.
3. **Unsafe access.** Agent permissions are usually enforced in prompts or by a shared super-user credential. A user's agent can read or reach data the user could not, directly or through multi-hop and inferred paths in a graph.
4. **Unsafe writes.** Agents act through free-form tool calls or SQL, with no typed contract, no human approval for consequential changes, and no complete record of who proposed, approved and executed what. Risky or regulated actions therefore stay manual.

The result is that enterprise agent projects stall at read-only copilots. The platforms that close these gaps (for example Palantir Foundry/AIP) are closed and costly, and there is no open, inspectable reference for how to build one.

## Solution statement

Amarh is an open-source, ontology-driven platform that makes it safe for enterprise agents to reason over operational data and to act on it under human control.

1. **One ontology as the single definition.** A YAML ontology compiles to the database schema, an OWL/SHACL semantic layer, the API and a TypeScript SDK. Agents and people work from the same typed model.
2. **Reasoning over a semantic graph.** OWL 2 RL inference and multi-hop traversal give the agent indirect relationships (impact analysis), and every inferred fact is flagged and carries provenance, so asserted and inferred facts are never confused.
3. **Bitemporal history.** Valid time and recorded time on every fact let the agent ask "as of", diff two points in time and explain change.
4. **Permissions enforced below the model.** Role-based definition-level and row-level permissions with property masking are enforced in the API and in Postgres (FORCE row-level security). The agent's effective permissions are the user's, the role's and the agent's allowlist intersected, and traversals and inferred edges are filtered per node and edge. Permissions also cover history.
5. **Propose, approve, execute.** Writes happen only through typed action types in one transaction. The agent can only create a proposal with rationale, evidence and a dry-run diff. A named human approves, execution runs as the approver and is re-checked, and side effects go through an outbox after commit.
6. **Audit and evaluation.** An append-only record of every run, tool call, proposal, approval, execution, diff and rollback, plus a seeded eval suite that checks the agent against golden assertions (right action proposed, forbidden action never taken, denials respected).

Outcome: an organisation can let agents investigate and recommend at machine speed while a human stays accountable for every change, with a verifiable trail.

## Scope note

Amarh is a simulated, local-first reference platform, not a production deployment. Known limits (no real login, view-level masking, trusted identity settings inside a scoped session, no live Claude API run yet) are in `docs/contracts.md`.

## Fit with a central data platform

Assumption: a central data platform (a governed lakehouse/fabric with catalog, pipelines and identity) is the enterprise's system of record for raw data. Amarh does not replace it; it adds a meaning, permission and action layer on top.

- **Read path:** platform tables are mapped by the YAML ontology into typed objects and links (Postgres, bitemporal, row-level security), then into the semantic graph for reasoning.
- **Write path:** agents only propose. An approved action runs in one transaction, is audited, and leaves through the outbox as a webhook or API call to the source system or platform.
- **Identity:** platform SSO groups would map to Amarh roles, replacing the non-secret `x-role` header.
- **Governance:** platform lineage and classification can feed masking and row rules; Amarh adds per-edge filtering and audit.

Not built yet: a connector to a real data platform, SSO, and a live Claude API run. Today Amarh runs on simulated data.
