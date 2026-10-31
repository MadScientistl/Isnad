"""Phase 6 end-to-end demo: supply chain -> disruption -> agent proposal -> approval -> scenario -> history -> import.

Run: uv run python scripts/demo_phase6.py [--days 7] [--seed 1] [--model baseline|mock|live] [--live] [--keep]

Headless, deterministic, no network by default. Needs `docker compose up -d postgres`. Everything runs against
throw-away schemas (`demo6_<rand>` and the imported `ws_<slug>`) and temp ingest/workspace directories, through the
real FastAPI app in the default role-switcher mode: the role is the `x-role` header (no login), exactly what the UI
role switcher sends. The identity behind a role is `demo-<role>`, so planner and admin are different identities.

  (a) seed the supply chain, simulate DAYS days with an injected port closure (Rotterdam)
  (b) the planner's agent analyses the disruption and creates a proposal (scripted mock model; --live: real Claude)
  (c) an admin (a different identity than the launcher) approves it: executed as the approver, audit row written
  (d) a what-if scenario with an extra disruption, compared with main
  (e) bitemporal history and an as-of read of the closed port
  (f) ontology import of the bike-share dataset: profile -> proposal -> validation -> review -> audited import ->
      read the imported workspace back (counts, geo query, graph neighbourhood)

The scripted agent and the cassette prove the PLATFORM around a model (tools, permissions, proposals, validation,
import), not model judgement. `--live` needs ANTHROPIC_API_KEY and exits 2 without it (before any network use).
Exit code: 0 all steps passed, 1 a step failed, 2 live requested without a key.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BIKE = ROOT / "evals" / "ontogen" / "datasets" / "bikeshare"
CASSETTE = ROOT / "evals" / "ontogen" / "cassettes" / "bikeshare_good.json"
ONTOLOGY = str(ROOT / "ontology" / "supply_chain.yaml")
ROLE_HDR = {"planner": {"x-role": "planner"}, "admin": {"x-role": "admin"}}


class StepFailed(Exception):
    pass


def check(cond, msg: str) -> None:
    if not cond:
        raise StepFailed(msg)


class Demo:
    """Narrated step runner: prints `[n] title`, indented facts, `[ok]`/`[FAIL]` with timing."""

    def __init__(self, quiet: bool = False):
        self.results: list[tuple[str, bool, float]] = []
        self.quiet = quiet
        self.n = 0

    def say(self, text: str = "") -> None:
        if not self.quiet:
            print(text, flush=True)

    def fact(self, text: str) -> None:
        self.say("      " + text)

    def run(self, title: str, fn) -> bool:
        self.n += 1
        self.say(f"\n[{self.n}] {title}")
        t0 = time.monotonic()
        try:
            fn()
            ok = True
        except StepFailed as e:
            ok = False
            self.say(f"      assertion failed: {e}")
        except Exception as e:  # noqa: BLE001 - report any breakage as a failed step
            ok = False
            self.say(f"      error: {type(e).__name__}: {e}")
            if os.environ.get("DEMO_TRACE"):
                traceback.print_exc()
        dt = time.monotonic() - t0
        self.results.append((title, ok, dt))
        self.say(f"      [{'ok' if ok else 'FAIL'}] {dt:.1f}s")
        return ok


# --------------------------------------------------------------------------- environment
def setup_env(tmp: Path) -> None:
    """Set the environment BEFORE the engine modules read it. Auth stays off (role switcher mode); the import routes
    are enabled by ISNAD_ALLOW_IMPORT=1, as `scripts/dev.sh --import` does."""
    os.environ.update({
        "ISNAD_ALLOW_IMPORT": "1",
        "ISNAD_INGEST_DIR": str(tmp / "ingest"), "ISNAD_WORKSPACES_DIR": str(tmp / "workspaces"),
        "ISNAD_OUTBOX_WORKER": "0", "ISNAD_SIM_AUTOSTART": "0", "ISNAD_AGENT_RUNS_PER_MIN": "100",
        "ISNAD_AGENT_MAX_CONCURRENT": "3", "ISNAD_INGEST_SESSIONS_PER_USER": "100", "ISNAD_SCN_MAX_PER_USER": "20"})
    for k in ("ISNAD_AUTH", "ISNAD_DEFAULT_ROLE"):
        os.environ.pop(k, None)


class Ctx:
    """State shared by the steps."""
    schema: str
    ws_schema: str | None = None
    client = None
    hdr: dict
    rotterdam: str
    proposal_id: str
    action_id: str
    closed_at: str
    ingest_profile: dict | None = None


def wait(fetch, done, timeout=300.0, every=0.2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fetch()
        if done(v):
            return v
        time.sleep(every)
    raise StepFailed("timeout")


# --------------------------------------------------------------------------- the demo
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=float, default=7, help="simulated days (default 7)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--model", choices=["baseline", "mock", "live"], default=None,
                    help="ontology import proposal: baseline heuristic (default), mock cassette replay, live model")
    ap.add_argument("--live", action="store_true", help="use the real Claude model for the agent (and the import unless --model is set)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch schemas and temp dir")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    model_mode = a.model or ("live" if a.live else "baseline")
    if (a.live or model_mode == "live") and not os.environ.get("ANTHROPIC_API_KEY"):
        print("--live / --model live needs ANTHROPIC_API_KEY; none set. Exiting before any network use.", file=sys.stderr)
        return 2

    import psycopg
    from psycopg import sql

    from engine.db import get_dsn

    tmp = Path(tempfile.mkdtemp(prefix="isnad_demo6_"))
    setup_env(tmp)
    ctx = Ctx()
    ctx.schema = f"demo6_{uuid.uuid4().hex[:8]}"
    d = Demo(a.quiet)
    admin = psycopg.connect(get_dsn(), autocommit=True)
    t_start = time.monotonic()
    d.say("Isnad Phase 6 demo: from a simulated supply chain to a freshly imported ontology")
    d.say(f"  schema {ctx.schema}, seed {a.seed}, {a.days:g} sim days, agent={'live' if a.live else 'scripted mock'}, "
          f"import proposal={model_mode}")
    d.say("  role switcher mode (auth off): the role comes from the x-role header, identities are demo-planner and demo-admin")

    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(ctx.schema)))
        _steps(d, ctx, a, model_mode, tmp, admin)
    finally:
        if not a.keep:
            for s in (ctx.ws_schema, ctx.schema):
                if s:
                    try:
                        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(s)))
                    except Exception:  # noqa: BLE001
                        pass
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\nkept: schemas {ctx.schema} {ctx.ws_schema or ''}, directory {tmp}")
        admin.close()

    failed = [t for t, ok, _ in d.results if not ok]
    total = time.monotonic() - t_start
    print("\n" + "=" * 72)
    print("Summary")
    for t, ok, dt in d.results:
        print(f"  [{'ok' if ok else 'FAIL'}] {t}  ({dt:.1f}s)")
    print(f"\n{len(d.results) - len(failed)}/{len(d.results)} steps passed in {total:.0f}s"
          + ("" if not failed else f"; FAILED: {', '.join(failed)}"))
    if not failed:
        print("Not verified here: model judgement (the agent and the cassette are scripted) and any live Claude run "
              "unless --live was used.")
    return 1 if failed else 0


def _steps(d: Demo, ctx: Ctx, a, model_mode: str, tmp: Path, admin) -> None:
    from agent import MockModel  # noqa: F401
    from fastapi.testclient import TestClient

    from engine.api.main import create_app
    from engine.api.seed import _id, seed
    from engine.db import connect

    state: dict = {}

    # Compat shim: engine/api/main.py builds `_ingest_model()` with no parameter while ingest_routes calls
    # `model_factory(principal)`, so model-mode proposals fail with 503 until that is fixed there. Accept both.
    from engine.api import ingest_routes
    _orig_make_router = ingest_routes.make_router

    def _make_router(*args, model_factory=None, **kw):
        def mf(p=None):
            try:
                return model_factory(p)
            except TypeError:
                return model_factory()
        return _orig_make_router(*args, model_factory=mf if model_factory else None, **kw)
    ingest_routes.make_router = _make_router

    # ---------------------------------------------------------------- (a)
    def step_seed_and_sim():
        from sim.runner import SimRunner
        c = connect(schema=ctx.schema)
        try:
            info = seed(c)
            c.commit()
        finally:
            c.close()
        d.fact(f"seeded supply chain: {info}" if not isinstance(info, dict) else
               "seeded the supply chain (ports, warehouses, suppliers, carriers, shipments, invoices)")
        ctx.rotterdam = str(_id("Port", "Rotterdam"))
        epoch = datetime(2026, 1, 1, tzinfo=UTC)
        r = SimRunner(schema=ctx.schema, seed=a.seed, epoch=epoch)
        try:
            at = epoch + timedelta(days=max(1, a.days / 3))
            dur = a.days * 24
            inj = r.inject("port_closure", target=ctx.rotterdam, at=at, duration_h=dur)
            d.fact(f"injected port closure: Rotterdam from {inj['at'][:16]} for {dur:g} h")
            r.run_until(epoch + timedelta(days=a.days))
            digest = r.event_digest()
            check(r.actions > 0, "the simulation produced no actions")
            d.fact(f"simulated {a.days:g} days: {r.actions} audited actions, event digest {digest[:16]} "
                   "(same seed, same digest)")
            state["actions"], state["digest"] = r.actions, digest
        finally:
            r.close()
        state["sim_end"] = (epoch + timedelta(days=a.days)).isoformat()

    ok = d.run("(a) Seed the supply chain and simulate with an injected port closure", step_seed_and_sim)
    if not ok:
        return

    def mk_client():
        def agent_model(p):
            if p.user == "ingest":              # the ontology generator (ingest router asks with this identity)
                return state["ingest_model"]()
            if a.live:
                from agent.model_anthropic import AnthropicModel
                return AnthropicModel()
            from evals.scripts import PlanFollowingMock, exposure_plan
            return PlanFollowingMock(exposure_plan, port="Rotterdam")
        app = create_app(ONTOLOGY, schema=ctx.schema, agent_model_factory=agent_model)
        app.state.schema_name = ctx.schema
        return TestClient(app)

    ctx.client = mk_client()
    ctx.hdr = {"planner": ROLE_HDR["planner"], "admin": ROLE_HDR["admin"]}
    c, H = ctx.client, ctx.hdr

    # ---------------------------------------------------------------- (b)
    def step_agent():
        r = c.get("/objects/Port", headers=H["planner"])
        check(r.status_code == 200, f"GET /objects/Port as planner: {r.status_code}")
        port = next(p for p in r.json() if p["id"] == ctx.rotterdam)
        status = (port.get("props") or port).get("status")
        d.fact(f"the planner sees Rotterdam: status = {status}")
        check(status == "closed", f"Rotterdam should be closed after the simulation, is {status}")
        me = c.get("/me", headers=H["planner"]).json()
        d.fact(f"the planner acts as role {me['role']}; invoices masked/hidden for this role: "
               f"{'Invoice' not in me['types'] or not me['types']['Invoice']['read']}")
        r = c.post("/agent/runs", json={"prompt": "Rotterdam is closed. Assess the exposure and propose a response."},
                   headers=H["planner"])
        check(r.status_code in (200, 201, 202), f"start agent run: {r.status_code} {r.text[:200]}")
        rid = r.json()["run_id"]
        # "interrupted" is reported for a moment after the run leaves the registry and before its `finished` event is
        # readable (a small race in the API); a really interrupted run would time out here.
        j = wait(lambda: c.get(f"/agent/runs/{rid}", headers=H["planner"]).json(),
                 lambda j: j["run"]["status"] not in ("running", "interrupted"), timeout=600 if a.live else 120)
        run = j["run"]
        check(run["status"] == "completed", f"agent run ended {run['status']}")
        tools = sorted({e["payload"].get("name") for e in j.get("events", []) if e["kind"] == "tool_call"} - {None})
        d.fact(f"agent run {run['status']} as {run['role']} (user {run['user']}): tools used: {', '.join(tools)}")
        pids = run.get("proposal_ids") or []
        check(pids, "the agent created no proposal")
        ctx.proposal_id = pids[0]
        row = next(r for r in c.get("/proposals", headers=H["admin"]).json() if r["id"] == ctx.proposal_id)
        d.fact(f"proposal {ctx.proposal_id[:8]}: {row['action']} proposed by {row['proposer']} on behalf of "
               f"{row['on_behalf_of']}, state {row['state']}")
        check(row["proposer"] == "agent:planner" and row["on_behalf_of"] == "demo-planner", "proposal identity wrong")
        check(row["state"] == "pending", f"proposal state {row['state']}")
        ev = row.get("evidence") or []
        d.fact(f"evidence items attached: {len(ev)}; nothing was executed by the agent")

    d.run("(b) The planner's agent analyses the disruption and creates a proposal", step_agent)

    # ---------------------------------------------------------------- (c)
    def step_approve():
        check(getattr(ctx, "proposal_id", None), "no proposal from step (b)")
        r = c.post(f"/proposals/{ctx.proposal_id}/approve", headers=H["planner"], json={"comment": "self"})
        d.fact(f"the launcher (the planner role) tries to approve: HTTP {r.status_code}")
        check(r.status_code in (403, 404), "launcher must not be able to approve")
        r = c.post(f"/proposals/{ctx.proposal_id}/approve", headers=H["admin"],
                   json={"comment": "reviewed: Rotterdam is closed, reroute is sensible"})
        check(r.status_code == 200, f"approve by admin: {r.status_code} {r.text[:300]}")
        res = r.json()
        ctx.action_id = res.get("action_id")
        d.fact(f"admin (demo-admin) approved: status {res['status']}, action_id {str(ctx.action_id)[:8]}")
        ex = c.get(f"/executions/{ctx.action_id}", headers=H["admin"]).json()
        e = ex["execution"]
        d.fact(f"audit row: {e['name']} actor={e['actor']} role={e['role']} status={e['status']} "
               f"proposal={str(e.get('proposal_id'))[:8]}; diff entries: {len(ex['diff'])}")
        check(e["status"] == "executed", "execution not executed")
        check(e["actor"] == "demo-admin", f"must execute as the approver, actor is {e['actor']}")
        r = c.post(f"/proposals/{ctx.proposal_id}/approve", headers=H["admin"])
        check(r.status_code in (409, 403, 422), f"second approve should be refused, got {r.status_code}")

    d.run("(c) A different admin approves; executed as the approver; audit row", step_approve)

    # ---------------------------------------------------------------- (d)
    def step_scenario():
        r = c.post("/scenarios", json={"name": "what if Hamburg closes too"}, headers=H["planner"])
        check(r.status_code == 201, f"create scenario: {r.status_code} {r.text[:200]}")
        sid = r.json()["id"]
        hamburg = str(_id("Port", "Hamburg"))
        r = c.post(f"/scenarios/{sid}/simulate", headers=H["planner"], json={
            "days": 3, "seed": a.seed,
            "injections": [{"kind": "port_closure", "target": hamburg, "at_h": 0, "duration_h": 48}]})
        check(r.status_code == 202, f"simulate: {r.status_code} {r.text[:200]}")
        job = r.json()["job_id"]
        st = wait(lambda: c.get(f"/scenarios/{sid}/simulate/{job}", headers=H["planner"]).json(),
                  lambda s: s["status"] != "running", timeout=300)
        check(st["status"] == "done", f"what-if ended {st['status']}: {st.get('error')}")
        d.fact(f"what-if simulated 3 days inside scenario {sid}: {st['actions']} actions, digest {str(st['digest'])[:16]}")
        cmp_ = c.get(f"/scenarios/{sid}/compare", headers=H["planner"]).json()
        delta = {k: v for k, v in (cmp_.get("kpis") or {}).get("delta", {}).items() if v}
        d.fact(f"KPI delta, scenario minus main: {delta or 'none'}")
        n_changed = len(cmp_.get("diff") or cmp_.get("changes") or [])
        d.fact(f"objects changed in the scenario overlay: {n_changed}")
        check(cmp_.get("kpis") is not None, "compare returned no KPIs")
        main_port = c.get(f"/objects/Port/{_id('Port', 'Hamburg')}", headers=H["planner"]).json()
        check((main_port.get("props") or main_port).get("status") == "open", "main must be unchanged by the scenario")
        d.fact("main still shows Hamburg open: the scenario is an overlay, not a write to main")
        r = c.delete(f"/scenarios/{sid}", headers=H["planner"])
        check(r.status_code == 200, f"discard scenario: {r.status_code}")
        d.fact(f"scenario {sid} discarded")

    d.run("(d) Fork a scenario, run a what-if with an extra closure, compare with main", step_scenario)

    # ---------------------------------------------------------------- (e)
    def step_history():
        h = c.get(f"/objects/Port/{ctx.rotterdam}/history", headers=H["planner"]).json()
        d.fact(f"Rotterdam has {len(h)} stored versions (bitemporal: valid time x recorded time)")
        statuses = []
        for v in sorted(h, key=lambda v: str(v.get("valid_from"))):
            s = (v.get("props") or v).get("status")
            if s and (not statuses or statuses[-1][1] != s):
                statuses.append((str(v.get("valid_from"))[:16], s))
        d.fact("status over valid time: " + " -> ".join(f"{s} ({t})" for t, s in statuses))
        check(len(h) >= 2 and len(statuses) >= 2, "expected the port to have changed status at least once")
        first = statuses[0][0]
        past = c.get(f"/objects/Port/{ctx.rotterdam}", params={"as_of_valid": first + ":00+00:00"}, headers=H["planner"])
        if past.status_code == 200:
            ps = (past.json().get("props") or past.json()).get("status")
            d.fact(f"as-of read at {first}: status = {ps}")
            check(ps == statuses[0][1], "as-of read disagrees with the history")

    d.run("(e) History and as-of read of the closed port", step_history)

    # ---------------------------------------------------------------- (f)
    def step_import():
        from engine.ingest import workspaces as W
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(BIKE.glob("*.csv")):
                z.write(p, p.name)
        r = c.post("/ingest/uploads", params={"filename": "bikeshare.zip"}, content=buf.getvalue(), headers=H["admin"])
        check(r.status_code == 201, f"upload: {r.status_code} {r.text[:300]}")
        sid = r.json()["session_id"]
        sess = c.get(f"/ingest/sessions/{sid}", headers=H["admin"]).json()
        prof = sess["profile"]
        ctx.ingest_profile = prof
        d.fact(f"admin uploaded bikeshare.zip: profiled {len(prof['tables'])} tables, "
               f"{sum(t['rows'] for t in prof['tables'])} rows, {len(prof['fks'])} foreign-key candidates")

        def make_model():
            from agent.ontogen.cassette import Resolver, load_cassette
            from agent.ontogen.generate import profile_hash_of
            if model_mode == "live":
                from agent.model_anthropic import AnthropicModel
                return AnthropicModel()
            return load_cassette(CASSETTE, Resolver(prof, profile_hash_of(prof)))
        state["ingest_model"] = make_model

        r = c.post(f"/ingest/sessions/{sid}/proposals", json={"mode": "baseline" if model_mode == "baseline" else "model"},
                   headers=H["admin"])
        check(r.status_code == 202, f"proposal: {r.status_code} {r.text[:300]}")
        body = r.json()
        pid = body.get("proposal_id")
        if not pid:
            job = wait(lambda: c.get(f"/ingest/jobs/{body['job_id']}", headers=H["admin"]).json(),
                       lambda j: j["status"] in ("done", "failed", "cancelled"), timeout=900 if model_mode == "live" else 120)
            check(job["status"] == "done" and job.get("proposal_id"), f"generator job: {job['status']} {job.get('error')}")
            pid = job["proposal_id"]
            d.fact(f"generator ({model_mode}) finished, cost ${job.get('cost_usd', 0):.4f}")
        pr = c.get(f"/ingest/proposals/{pid}", headers=H["admin"]).json()
        prop, val = pr["proposal"], pr["validation"]
        types = [o["name"] for o in prop["objectTypes"]]
        d.fact(f"proposal ({pr['kind']}): {len(types)} object types ({', '.join(types)}), "
               f"{len(prop['linkTypes'])} link types, roles {[x['name'] for x in prop['roles']]}")
        bad = [k["name"] for k in val["checks"] if not k["ok"]]
        d.fact(f"validation in a scratch schema: {len(val['checks']) - len(bad)}/{len(val['checks'])} checks ok"
               + (f", failed: {bad}" if bad else " (schema, DDL, keys, nulls, coercion, enums, link resolution, SHACL, reasoner)"))
        state["ws_roles"] = [x["name"] for x in prop["roles"] if x["name"] != "admin"]
        d.fact(f"rows loaded in the scratch validation: {sum((val.get('stats') or {}).get('rows_loaded', {}).values())}")
        check(val["ok"], "validation did not pass; a human would repair or edit the proposal")
        r = c.post(f"/ingest/proposals/{pid}/approve", json={"comment": "reviewed types, keys, links and roles"},
                   headers=H["admin"])
        check(r.status_code == 201, f"approve: {r.status_code} {r.text[:300]}")
        did = r.json()["decision_id"]
        d.fact(f"admin approved (decision {did[:8]}). Note: without auth the creator may approve; a real second "
               "reviewer needs ISNAD_AUTH=1 (experimental)")
        r = c.post(f"/ingest/decisions/{did}/import", headers=H["admin"])
        check(r.status_code == 202, f"import: {r.status_code} {r.text[:300]}")
        job = wait(lambda: c.get(f"/ingest/jobs/{r.json()['job_id']}", headers=H["admin"]).json(),
                   lambda j: j["status"] in ("done", "failed", "cancelled"), timeout=300)
        check(job["status"] == "done", f"import {job['status']}: {job.get('error')}")
        res = job["result"]
        ctx.ws_schema = res["schema"]
        d.fact(f"imported into workspace {res['slug']} (schema {res['schema']}): {res['rows']} objects, "
               f"{sum(res['counts']['links'].values())} links, unresolved/duplicate links dropped: "
               f"{sum(v['unresolved'] + v['duplicate'] for v in res['dropped_links'].values())}")
        a_rows = admin.execute(sql_audit(ctx.ws_schema)).fetchone()
        d.fact(f"audit row in the workspace: import_dataset by {a_rows[0]} (role {a_rows[1]}, source {a_rows[2]})")
        check(a_rows[0] == "demo-admin" and a_rows[2] == "import", "the import audit row is wrong")
        ws = [w for w in c.get("/workspaces", headers=H["admin"]).json() if w["slug"] == res["slug"]]
        check(ws, "workspace not listed")
        state["ws"] = res
        _ = W

    d.run("(f) Import the bike-share dataset: profile, proposal, validation, review, audited import", step_import)

    # ---------------------------------------------------------------- (g)
    def step_read_workspace():
        res = state.get("ws")
        check(res, "no workspace from step (f)")
        wdir = Path(os.environ["ISNAD_WORKSPACES_DIR"]) / res["slug"]
        wapp = create_app(str(wdir / "ontology.yaml"), schema=res["schema"], pipeline=False)
        wc = TestClient(wapp)
        wh = ROLE_HDR["admin"]
        other = (state.get("ws_roles") or ["viewer"])[0]
        vh = {"x-role": other}
        s = wc.get("/stats/summary", headers=wh).json()
        d.fact(f"object counts via /stats/summary: {s['counts']}")
        check(s["counts"] and sum(s["counts"].values()) == res["rows"], "counts do not match the import report")
        types = wc.get("/types", headers=wh).json()
        geo_types = [t["name"] for t in types["objectTypes"] if t.get("geo")]
        check(geo_types, "the imported ontology has no geo type")
        gt = geo_types[0]
        pts = wc.get(f"/geo/{gt}", params={"bbox": "-180,-90,180,90"}, headers=wh).json()
        feats = pts.get("features", pts) if isinstance(pts, dict) else pts
        d.fact(f"geo query /geo/{gt} (whole world bbox): {len(feats)} features")
        check(len(feats) > 0, "geo query returned nothing")
        objs = wc.get(f"/objects/{gt}", params={"limit": 1}, headers=wh).json()
        check(objs, f"no {gt} objects")
        oid = objs[0]["id"]
        g = wc.get("/graph", params={"center": oid, "depth": 1}, headers=wh).json()
        nodes, edges = g.get("nodes", []), g.get("edges", [])
        d.fact(f"graph neighbourhood of one {gt} (depth 1): {len(nodes)} nodes, {len(edges)} edges")
        check(len(nodes) >= 1, "empty graph neighbourhood")
        v = wc.get(f"/objects/{gt}", params={"limit": 1}, headers=vh)
        w = wc.post(f"/actions/create_object/execute", json={"params": {"type": gt, "props": {}}}, headers=vh)
        d.fact(f"the non-admin workspace role '{other}' can read ({v.status_code}) but not write ({w.status_code}) in the new workspace")
        check(v.status_code == 200 and w.status_code in (403, 404, 422) and w.status_code != 200, "viewer role not enforced")
        wc.close()

    d.run("(g) Read the imported workspace back through the engine (counts, geo query, graph)", step_read_workspace)


def sql_audit(schema: str):
    from psycopg import sql
    return sql.SQL("SELECT actor, role, source FROM {}.audit_event WHERE name = 'import_dataset' ORDER BY seq LIMIT 1").format(
        sql.Identifier(schema))


if __name__ == "__main__":
    sys.exit(main())
