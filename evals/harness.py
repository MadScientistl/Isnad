"""Run one agent run against a fresh deterministic world and collect everything the checks need."""
from __future__ import annotations

import copy
import json
import os
import re
import time
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
        self.times: dict = {}   # (kind, tool_call_id) -> perf_counter at emit (events carry no timestamps)

    def run_started(self, *a, **kw):
        self.db.run_started(*a, **kw)
        self.mem.run_started(*a, **kw)

    def emit(self, ev):
        cid = (ev.get("payload") or {}).get("tool_call_id")
        if cid and ev.get("kind") in ("tool_call", "tool_result"):
            self.times[(ev["kind"], cid)] = time.perf_counter()
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
    times: dict = field(default_factory=dict)
    latency_s: float = 0.0
    fp_before: str = ""
    scenario_ids: list = field(default_factory=list)
    base_seq: int = 0
    scn_overrides: list = field(default_factory=list)   # override rows captured when the run ended

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
    return admin_rows(world, "SELECT seq, name, actor, role, source, status, on_behalf_of, proposal_id, run_id, params, "
                             "scenario_id FROM audit_event WHERE seq > %s ORDER BY seq", [after_seq])


def _max_seq(world) -> int:
    return int(admin_rows(world, "SELECT coalesce(max(seq),0) AS m FROM audit_event")[0]["m"])


def make_model(mode: str, plan: str | None, args: dict):
    if mode == "mock":
        return Recording(scripts.PlanFollowingMock(scripts.PLANS[plan], **args)), f"scripted plan: {plan}"
    if not have_key():
        raise RuntimeError("ANTHROPIC_API_KEY is not set; live mode refused")
    from agent.model_anthropic import AnthropicModel
    return Recording(AnthropicModel(os.environ.get("ISNAD_EVAL_MODEL") or "claude-haiku-5-5", effort="low")), "live model"


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
    captured: list = []

    def _capture():       # the run's unreferenced scenarios are purged when it ends; keep what the checks need
        captured.extend(admin_rows(world, "SELECT o.scenario_id, o.kind, o.type, o.key_a FROM scenario_override o "
                                          "JOIN scenario s ON s.id = o.scenario_id WHERE s.run_id = %s",
                                   [ctx.run_id]))
    ctx.on_finish = _capture
    mem, db = MemorySink(), DbSink(world.factory, principal)
    tee = TeeSink(db, mem)
    fp_before = world.fingerprint()
    t0 = time.perf_counter()
    try:
        result = AgentRun(ctx, model, tee).run(prompt)
    finally:
        db.close()
    latency = time.perf_counter() - t0
    r = Run(world, role, prompt, mode, result, mem.events, model.sent, ctx.run_id,
            allowed_tools(comp().model, role), allowed_actions(comp().model, role), plan_label=label,
            caps=ctx.caps.as_dict())
    r.times, r.latency_s, r.fp_before = tee.times, latency, fp_before
    r.proposals = admin_rows(
        world, "SELECT p.id, p.action, p.params, p.proposer, p.proposer_role, p.on_behalf_of, p.rationale, p.evidence, "
               "p.dry_run, p.expires_at, p.run_id, p.scenario_id, a.decision FROM proposal p LEFT JOIN approval a "
               "ON a.proposal_id = p.id WHERE p.run_id = %s ORDER BY p.seq", [ctx.run_id])
    r.base_seq = base
    r.scn_overrides = captured
    r.scenario_ids = [int(x["id"]) for x in admin_rows(
        world, "SELECT id FROM scenario WHERE run_id = %s ORDER BY id", [ctx.run_id])]
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
    os.environ.setdefault("ISNAD_OUTBOX_WORKER", "0")
    os.environ.pop("ISNAD_DEFAULT_ROLE", None)
    from fastapi.testclient import TestClient
    from engine.api.main import create_app
    return TestClient(create_app(ONTOLOGY, schema=world.schema, pipeline=world.pipeline or False))


def H(role: str) -> dict:
    return {"x-role": role}


def _scn_exec_count(run: Run) -> int:
    if not run.scenario_ids:
        return 0
    return int(admin_rows(run.world, "SELECT count(*) AS n FROM audit_event WHERE scenario_id = ANY(%s) AND "
                                     "status = 'executed'", [run.scenario_ids])[0]["n"])


def _merge_conflict_hook(run: Run, cl, out: dict) -> None:
    """Main moves on after the fork (same shipment the scenario touched), then the owner tries to merge."""
    from engine.actions import execute
    from evals.world import SIM
    sid = run.scenario_ids[0]
    ov = admin_rows(run.world, "SELECT key_a FROM scenario_override WHERE scenario_id = %s AND kind = 'obj' "
                               "AND type = 'Shipment' ORDER BY key_a", [sid])
    if not ov:
        out["merge"] = {"status": None, "body": "scenario touched no shipment"}
        return
    c = run.world.factory()
    try:
        execute(c, "delay_shipment", {"shipment": ov[0]["key_a"], "delay_hours": 33.0, "reason": "main moved on"},
                SIM, valid_at=datetime.now(UTC), source="sim", run_id="post-hook")
    finally:
        c.close()
    fp_pre, rows_pre = run.world.fingerprint(), len(_exec_rows(run.world, 0))
    r = cl.post(f"/scenarios/{sid}/merge", json={}, headers=H(run.role))
    out["merge"] = {"status": r.status_code, "body": _short(r), "code": (r.json() or {}).get("code") if r.status_code >= 400 else None}
    out["merge_fp"] = (fp_pre, run.world.fingerprint())
    out["merge_rows"] = (rows_pre, len(_exec_rows(run.world, 0)))
    out["merge_exec"] = [e["name"] for e in _exec_rows(run.world, 0) if e["source"] == "scenario_merge"]


def _merge_rows(world, sql_after: int, scn_run_id: str) -> tuple[list, list]:
    ex = admin_rows(world, "SELECT seq, name, actor, source, status, on_behalf_of, scenario_id, origin_action_id FROM "
                           "audit_event WHERE seq > %s AND scenario_id = 0 AND status = 'executed' ORDER BY seq",
                    [sql_after])
    pr = admin_rows(world, "SELECT p.id, p.action, p.proposer, p.proposer_role, p.on_behalf_of, p.origin_action_id, "
                           "p.run_id, a.decision FROM proposal p LEFT JOIN approval a ON a.proposal_id = p.id "
                           "WHERE p.origin_action_id IS NOT NULL AND p.rationale LIKE 'Merged from scenario%%' "
                           "ORDER BY p.seq")
    return ex, pr


def _merge_hook(run: Run, cl, out: dict) -> None:
    """The scenario OWNER merges the agent's scenario through the API: agent work must come back as proposals."""
    sid = run.scenario_ids[0]
    agent_exec = [e for e in _exec_rows(run.world, 0) if e["scenario_id"] == sid and e["status"] == "executed"
                  and (str(e["actor"]).startswith("agent:") or e["source"] == "agent")]
    seq0 = _max_seq(run.world)
    pre_props = {str(x["id"]) for x in admin_rows(run.world, "SELECT id FROM proposal")}
    fp_pre = run.world.fingerprint()
    r = cl.post(f"/scenarios/{sid}/merge", json={"comment": "eval merge"}, headers=H(run.role))
    ex, pr = _merge_rows(run.world, seq0, run.run_id)
    new = [x for x in pr if str(x["id"]) not in pre_props]
    out["merge_agent"] = {"status": r.status_code, "body": _short(r), "scenario_agent_execs": len(agent_exec),
                          "main_execs": [dict(e, origin_action_id=str(e["origin_action_id"])) for e in ex],
                          "new_proposals": [{k: (str(v) if v is not None else None) for k, v in x.items()} for x in new],
                          "fp": (fp_pre, run.world.fingerprint()),
                          "origin_ids": sorted(str(e["action_id"]) for e in _agent_action_ids(run, sid))}


def _agent_action_ids(run, sid):
    return admin_rows(run.world, "SELECT action_id FROM audit_event WHERE scenario_id = %s AND status = 'executed' "
                                 "AND (actor LIKE 'agent:%%' OR source = 'agent')", [sid])


def _approve_merged_hook(run: Run, cl, out: dict) -> None:
    m = out.get("merge_agent") or {}
    props = m.get("new_proposals") or []
    out["merged_owner_approve"], out["merged_admin_approve"] = [], []
    for pp in props:
        r = cl.post(f"/proposals/{pp['id']}/approve", json={}, headers=H(run.role))
        out["merged_owner_approve"].append({"status": r.status_code, "body": _short(r)})
    out["merged_after_owner"] = [_proposal_execs(run.world, pp["id"]) for pp in props]
    for pp in props:
        r = cl.post(f"/proposals/{pp['id']}/approve", json={"comment": "ok"}, headers=H("admin"))
        out["merged_admin_approve"].append({"status": r.status_code, "body": _short(r)})
    out["merged_execs"] = [_proposal_execs(run.world, pp["id"]) for pp in props]


def run_post_hooks(run: Run, hooks: list) -> dict:
    if not hooks:
        return {}
    cl = api_client(run.world)
    out: dict = {}
    if "merge_agent" in hooks and run.scenario_ids:
        _merge_hook(run, cl, out)
        if "approve_merged" in hooks:
            _approve_merged_hook(run, cl, out)
    if "merge_conflict" in hooks and run.scenario_ids:
        _merge_conflict_hook(run, cl, out)
    if not run.proposals:
        return out
    pid = str(run.proposals[0]["id"])
    out["proposal_id"] = pid
    out["scn_exec_before"] = _scn_exec_count(run)
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
    out["scn_exec_after"] = _scn_exec_count(run)
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
def _setup_foreign_scenario(world: World) -> None:
    """Another user (the admin) owns an open scenario in which SH-1001 has an unmistakable delay (777.5 h)."""
    from engine import scenarios
    from engine.actions import execute
    c, adm = world.factory(), world.principal("admin")
    try:
        d = scenarios.create(c, adm, "admin what-if", ontology=comp())
        execute(c, "delay_shipment", {"shipment": world.ids["shipment"]["SH-1001"], "delay_hours": 777.5,
                                      "reason": "foreign what-if"}, adm, valid_at=datetime.now(UTC), source="api",
                scenario_id=d["id"], ontology=comp())
        world.ids["scn"] = {"foreign": d["id"]}
    finally:
        c.close()


SETUPS = {"foreign_scenario": _setup_foreign_scenario}


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


def _trim(x, n: int = 120, depth: int = 0):
    """Bounded copy of a tool input for the report (long strings cut, big lists/dicts capped)."""
    if isinstance(x, str):
        return x if len(x) <= n else x[:n] + f"...(+{len(x) - n})"
    if isinstance(x, dict):
        items = list(x.items())[:12]
        return {str(k): _trim(v, n, depth + 1) for k, v in items} if depth < 4 else "{...}"
    if isinstance(x, (list, tuple)):
        out = [_trim(v, n, depth + 1) for v in list(x)[:8]] if depth < 4 else ["..."]
        return out + [f"...(+{len(x) - 8} more)"] if len(x) > 8 else out
    return x


def tool_trace(run: Run) -> list:
    out = []
    for call, res, den in run.calls():
        cid = call["tool_call_id"]
        t0, t1 = run.times.get(("tool_call", cid)), run.times.get(("tool_result", cid))
        out.append({"name": call["name"], "input": _trim(call.get("input")), "ok": bool(res and res.get("ok")),
                    "denied": bool(den), "ms": round((t1 - t0) * 1000, 2) if t0 is not None and t1 is not None else None})
    return out


def run_case(case: dict, mode: str = "mock", seed: int = 1) -> dict:
    from evals import checks
    world = build_world(seed)
    try:
        for name in case.get("setup") or []:
            SETUPS[name](world)
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
        return {"id": case["id"], "seed": seed, "role": case["role"], "description": case.get("description", ""),
                "mode": mode, "plan": run.plan_label, "status": res.status, "cap": res.cap, "error": res.error,
                "latency_s": round(run.latency_s, 4),
                "telemetry": {"turns": res.turns, "tool_calls": res.tool_calls, "tokens_in": res.tokens_in,
                              "tokens_out": res.tokens_out, "cost_usd": res.cost_usd,
                              "proposals": len(run.proposals)},
                "tool_trace": tool_trace(run), "scenario_ids": list(run.scenario_ids),
                "summary": run.summary()[:600], "outcomes": outcomes}
    finally:
        world.close()


def run_suite(mode: str = "mock", only: str | None = None, seeds=(1,)) -> list:
    cases = [c for c in load_cases(only) if mode in (c.get("modes") or ["mock", "live"])]
    return [run_case(c, mode, sd) for sd in seeds for c in cases]


def suite_summary(results: list) -> dict:
    tot = {"hard": [0, 0, 0], "soft": [0, 0, 0], "telemetry": [0, 0, 0]}   # pass, fail, n/a
    for r in results:
        for o in r["outcomes"]:
            tot[o["class"]][{"pass": 0, "fail": 1, "na": 2}[o["status"]]] += 1
    return {k: dict(zip(("pass", "fail", "na"), v)) for k, v in tot.items()}


def write_report(results: list, mode: str, out_dir: Path | None = None, md: bool = False) -> Path:
    """JSON report (and a markdown rendering next to it with md=True). Returns the JSON path."""
    out_dir = out_dir or Path(__file__).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    stem = f"report_{mode}_{now.strftime('%Y%m%dT%H%M%SZ')}"
    path = out_dir / f"{stem}.json"
    seeds = sorted({r.get("seed", 1) for r in results})
    report = {"mode": mode, "generated_at": now.isoformat(), "seeds": seeds,
              "summary": suite_summary(results), "cases": results}
    path.write_text(json.dumps(report, indent=2, default=str))
    if md:
        (out_dir / f"{stem}.md").write_text(render_markdown(report))
    return path


def _counts(r: dict, cls: str) -> str:
    os_ = [o for o in r["outcomes"] if o["class"] == cls and o["status"] != "na"]
    return f"{sum(o['status'] == 'pass' for o in os_)}/{len(os_)}"


def render_markdown(report: dict) -> str:
    cases, s = report["cases"], report["summary"]
    cost = sum(r["telemetry"]["cost_usd"] for r in cases)
    lat = sum(r.get("latency_s", 0) for r in cases)
    L = [f"# Eval report ({report['mode']})", "",
         f"Generated {report['generated_at']}; seeds {report.get('seeds')}; {len(cases)} case runs.", "",
         f"- hard: {s['hard']['pass']} pass, {s['hard']['fail']} fail, {s['hard']['na']} n/a",
         f"- soft: {s['soft']['pass']} pass, {s['soft']['fail']} fail, {s['soft']['na']} n/a",
         f"- telemetry: {s['telemetry']['pass']} recorded",
         f"- total cost ${cost:.4f}, total agent latency {lat:.1f} s", "",
         "| case | seed | role | result | hard | soft | calls | cost | latency |", "|---|---|---|---|---|---|---|---|---|"]
    for r in cases:
        bad = any(o["class"] == "hard" and o["status"] == "fail" for o in r["outcomes"])
        L.append(f"| {r['id']} | {r.get('seed', 1)} | {r['role']} | {'FAIL' if bad else 'pass'} | {_counts(r, 'hard')} | "
                 f"{_counts(r, 'soft')} | {r['telemetry']['tool_calls']} | ${r['telemetry']['cost_usd']:.4f} | "
                 f"{r.get('latency_s', 0):.2f} s |")
    fails = [(r, o) for r in cases for o in r["outcomes"] if o["status"] == "fail"]
    if fails:
        L += ["", "## Failed assertions", ""]
        for r, o in fails:
            L.append(f"- [{o['class']}] {r['id']} (seed {r.get('seed', 1)}) {o['check']}: {o['detail']}")
    return "\n".join(L) + "\n"
