"""Run one agent run against a fresh deterministic world and collect everything the checks need."""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agent import AgentContext, AgentRun, Caps, DbSink, MemorySink
from agent.policy import allowed_actions, allowed_tools
from engine.compiler import compile_ontology
from engine.db.principal import scoped
from evals import scripts
from evals.world import ONTOLOGY, World, build_world

CASES_DIR = Path(__file__).resolve().parent / "cases"
LIVE_CAPS = dict(max_turns=10, max_tool_calls=20, max_output_tokens=6000, max_cost_usd=0.25, max_wall_s=90.0)
_COMP = None


def comp():
    global _COMP
    if _COMP is None:
        _COMP = compile_ontology(ONTOLOGY)
    return _COMP


def have_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class TeeSink:
    """DbSink (real audit tables) + MemorySink (events for the checks)."""

    def __init__(self, db: DbSink, mem: MemorySink):
        self.db, self.mem = db, mem

    def run_started(self, *a, **kw):
        self.db.run_started(*a, **kw)
        self.mem.run_started(*a, **kw)

    def emit(self, ev):
        self.db.emit(ev)
        self.mem.emit(ev)


class Recording:
    """Wraps a model client and keeps every message list it was sent (what the model could see)."""

    def __init__(self, inner):
        self.inner, self.name, self.sent = inner, inner.name, []

    def complete(self, **kw):
        self.sent.append(copy.deepcopy(kw["messages"]))
        return self.inner.complete(**kw)


@dataclass
class Run:
    world: World
    role: str
    prompt: str
    mode: str
    result: object = None
    events: list = field(default_factory=list)
    sent: list = field(default_factory=list)
    run_id: str = ""
    tools: tuple = ()
    actions: tuple = ()
    proposals: list = field(default_factory=list)
    agent_executions: list = field(default_factory=list)
    executions: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)
    post: dict = field(default_factory=dict)
    plan_label: str = ""
    caps: dict = field(default_factory=dict)

    # ---- views over the events
    def calls(self):
        """[(tool_call event payload, tool_result payload, denied payload|None)] in order."""
        res = {e["payload"]["tool_call_id"]: e["payload"] for e in self.events if e["kind"] == "tool_result"}
        den = {e["payload"]["tool_call_id"]: e["payload"] for e in self.events if e["kind"] == "denied"}
        return [(e["payload"], res.get(e["payload"]["tool_call_id"]), den.get(e["payload"]["tool_call_id"]))
                for e in self.events if e["kind"] == "tool_call"]

    def transcript(self) -> str:
        """Everything the model and the audit trail saw, minus the user's own prompt and the system prompt."""
        parts = [json.dumps(self.events, default=str)]
        for msgs in self.sent:
            parts.append(json.dumps(msgs[1:], default=str))
        return "\n".join(parts)

    def summary(self) -> str:
        return getattr(self.result, "summary", "") or ""

    def inferred_edges(self) -> set:
        out = set()
        for _c, r, _d in self.calls():
            if r and r.get("result"):
                out |= {i["edge"] for i in r["result"].get("provenance", {}).get("inferred", [])}
        return out


def admin_rows(world: World, query: str, params=()) -> list:
    from psycopg.rows import dict_row
    p = world.principal("admin")
    conn = world.owner()
    try:
        with conn.transaction():
            with scoped(conn, p):
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(query, list(params))
                    return cur.fetchall()
    finally:
        conn.close()


def _exec_rows(world, after_seq: int) -> list:
    return admin_rows(world, "SELECT seq, name, actor, role, source, status, on_behalf_of, proposal_id, run_id, params "
                             "FROM audit_event WHERE seq > %s ORDER BY seq", [after_seq])


def _max_seq(world) -> int:
    return int(admin_rows(world, "SELECT coalesce(max(seq),0) AS m FROM audit_event")[0]["m"])


def make_model(mode: str, plan: str | None, args: dict):
    if mode == "mock":
        return Recording(scripts.PlanFollowingMock(scripts.PLANS[plan], **args)), f"scripted plan: {plan}"
    if not have_key():
        raise RuntimeError("ANTHROPIC_API_KEY is not set; live mode refused")
    from agent.model_anthropic import AnthropicModel
    return Recording(AnthropicModel(os.environ.get("AMARH_EVAL_MODEL") or "claude-haiku-5-5", effort="low")), "live model"


def run_agent(world: World, role: str, prompt: str, mode: str = "mock", plan: str | None = None,
              args: dict | None = None, caps: dict | None = None) -> Run:
    """One agent run as the human `demo-<role>` (same identity the HTTP API assigns). Never executes anything."""
    model, label = make_model(mode, plan, args or {})
    principal = world.principal(role)
    cap_kw = dict(LIVE_CAPS if mode == "live" else {})
    cap_kw.update(caps or {})
    ctx = AgentContext(principal, comp(), world.factory, pipeline=world.pipeline,
                       caps=Caps.for_role(comp().model.roles[role], **cap_kw))
    base = _max_seq(world)
    mem, db = MemorySink(), DbSink(world.factory, principal)
    try:
        result = AgentRun(ctx, model, TeeSink(db, mem)).run(prompt)
    finally:
        db.close()
    r = Run(world, role, prompt, mode, result, mem.events, model.sent, ctx.run_id,
            allowed_tools(comp().model, role), allowed_actions(comp().model, role), plan_label=label,
            caps=ctx.caps.as_dict())
    r.proposals = admin_rows(
        world, "SELECT p.id, p.action, p.params, p.proposer, p.proposer_role, p.on_behalf_of, p.rationale, p.evidence, "
               "p.dry_run, p.expires_at, p.run_id, a.decision FROM proposal p LEFT JOIN approval a "
               "ON a.proposal_id = p.id WHERE p.run_id = %s ORDER BY p.seq", [ctx.run_id])
    r.executions = _exec_rows(world, base)
    r.agent_executions = [e for e in r.executions if e["status"] == "executed" and (
        str(e["actor"]).startswith("agent:") or e["source"] == "agent" or e["run_id"] == ctx.run_id)]
    runs = admin_rows(world, "SELECT id FROM agent_run WHERE id = %s", [ctx.run_id])
    evs = admin_rows(world, "SELECT kind, count(*) AS n FROM agent_run_event WHERE run_id = %s GROUP BY kind",
                     [ctx.run_id])
    r.audit = {"agent_run_rows": len(runs), "events": {e["kind"]: int(e["n"]) for e in evs}}
    return r


# ------------------------------------------------------------------ HTTP (approval flow)
def api_client(world: World):
    os.environ.setdefault("AMARH_OUTBOX_WORKER", "0")
    os.environ.pop("AMARH_DEFAULT_ROLE", None)
    from fastapi.testclient import TestClient
    from engine.api.main import create_app
    return TestClient(create_app(ONTOLOGY, schema=world.schema, pipeline=world.pipeline or False))


def H(role: str) -> dict:
    return {"x-role": role}


def run_post_hooks(run: Run, hooks: list) -> dict:
    if not hooks or not run.proposals:
        return {}
    cl = api_client(run.world)
    pid = str(run.proposals[0]["id"])
    out: dict = {"proposal_id": pid}
    for h in hooks:
        if h == "launcher_cannot_approve":
            r = cl.post(f"/proposals/{pid}/approve", json={}, headers=H(run.role))
            out["launcher_approve"] = {"status": r.status_code, "body": _short(r)}
            out["executions_after_launcher"] = _proposal_execs(run.world, pid)
        elif h == "approve_twice":
            r1 = cl.post(f"/proposals/{pid}/approve", json={"comment": "ok"}, headers=H("admin"))
            r2 = cl.post(f"/proposals/{pid}/approve", json={"comment": "again"}, headers=H("admin"))
            out["approve1"] = {"status": r1.status_code, "body": _short(r1)}
            out["approve2"] = {"status": r2.status_code, "body": _short(r2)}
    out["executions_for_proposal"] = _proposal_execs(run.world, pid)
    return out


def _proposal_execs(world, pid) -> list:
    rows = admin_rows(world, "SELECT seq, name, actor, role, source, status, on_behalf_of, proposal_id FROM "
                             "audit_event WHERE proposal_id = %s ORDER BY seq", [pid])
    return [dict(r, proposal_id=str(r["proposal_id"])) for r in rows]


def _short(r):
    try:
        return json.dumps(r.json(), default=str)[:300]
    except ValueError:
        return r.text[:300]


# ------------------------------------------------------------------ cases
def _fill(text: str, world: World) -> str:
    def sub(m):
        cur = world.ids
        for k in m.group(1).split("."):
            cur = cur[k] if isinstance(cur, dict) and k in cur else cur[int(k)]
        return str(cur)
    return re.sub(r"<<([\w.\- ]+)>>", sub, text)


def load_cases(only: str | None = None) -> list:
    cases = []
    for f in sorted(CASES_DIR.glob("*.yaml")):
        for c in yaml.safe_load(f.read_text()) or []:
            if only is None or only in c["id"]:
                cases.append(c)
    return cases


def run_case(case: dict, mode: str = "mock") -> dict:
    from evals import checks
    world = build_world(1)
    try:
        plan = case.get("plan") or {}
        args = {k: (_fill(v, world) if isinstance(v, str) else v) for k, v in (plan.get("args") or {}).items()}
        run = run_agent(world, case["role"], _fill(case["prompt"], world), mode, plan.get("name"), args,
                        case.get("caps"))
        run.post = run_post_hooks(run, case.get("post") or [])
        outcomes = []
        for a in case["asserts"]:
            a = dict(a)
            cls, name = a.pop("class"), a.pop("check")
            if mode == "live" and a.pop("mock_only", False):
                continue
            a.pop("mock_only", None)
            try:
                status, detail = checks.CHECKS[name](run, **{k: (_fill(v, world) if isinstance(v, str) else v)
                                                              for k, v in a.items()})
            except Exception as e:  # noqa: BLE001
                status, detail = "fail", f"check crashed: {type(e).__name__}: {e}"
            outcomes.append({"class": cls, "check": name, "args": a, "status": status, "detail": detail})
        res = run.result
        return {"id": case["id"], "role": case["role"], "description": case.get("description", ""),
                "mode": mode, "plan": run.plan_label, "status": res.status, "cap": res.cap, "error": res.error,
                "telemetry": {"turns": res.turns, "tool_calls": res.tool_calls, "tokens_in": res.tokens_in,
                              "tokens_out": res.tokens_out, "cost_usd": res.cost_usd,
                              "proposals": len(run.proposals)},
                "summary": run.summary()[:600], "outcomes": outcomes}
    finally:
        world.close()


def run_suite(mode: str = "mock", only: str | None = None) -> list:
    cases = [c for c in load_cases(only) if mode in (c.get("modes") or ["mock", "live"])]
    return [run_case(c, mode) for c in cases]


def suite_summary(results: list) -> dict:
    tot = {"hard": [0, 0, 0], "soft": [0, 0, 0], "telemetry": [0, 0, 0]}   # pass, fail, n/a
    for r in results:
        for o in r["outcomes"]:
            tot[o["class"]][{"pass": 0, "fail": 1, "na": 2}[o["status"]]] += 1
    return {k: dict(zip(("pass", "fail", "na"), v)) for k, v in tot.items()}


def write_report(results: list, mode: str, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or Path(__file__).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report_{mode}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"mode": mode, "generated_at": datetime.now(UTC).isoformat(),
                                "summary": suite_summary(results), "cases": results}, indent=2, default=str))
    return path
