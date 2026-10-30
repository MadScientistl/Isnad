"""The engine/API work end to end on an ontology that is not the supply chain (no Shipment/Port/Invoice, no sim,
declared KPIs only). Library: Branch (point), Book, Member, Loan."""
from engine.db import bitemporal as bt
from engine.db import connect

def H(role, scn=None):
    return {"x-role": role, **({"x-scenario": str(scn)} if scn else {})}


PT = lambda x, y: {"type": "Point", "coordinates": [x, y]}  # noqa: E731


def _world(lib):
    """Branch + three books (a recommends b recommends c) + a member, created through the API."""
    if getattr(lib, "world", None):
        return lib.world
    br = lib.make("Branch", "admin", name="Central", city="Paris", location=PT(2.35, 48.85))
    a = lib.make("Book", title="A", pages=100, status="available")
    b = lib.make("Book", title="B", pages=200, status="available")
    c = lib.make("Book", title="C", pages=300, status="available")
    m = lib.make("Member", "admin", name="Mia", email="mia@example.org")
    for x in (a, b, c):
        assert lib.post("/links/held_at", "admin", json={"src": x, "dst": br}).status_code == 201
    assert lib.post("/links/recommends", "admin", json={"src": a, "dst": b}).status_code == 201
    assert lib.post("/links/recommends", "admin", json={"src": b, "dst": c}).status_code == 201
    lib.world = dict(br=br, a=a, b=b, c=c, m=m)
    return lib.world


# ---------------------------------------------------------------- /types
def test_types_describes_the_loaded_ontology(lib):
    t = lib.get("/types", "reader").json()
    assert t["name"] == "Library" and t["namespace"] == "https://example.org/lib#" and t["sim"] is False
    by = {o["name"]: o for o in t["objectTypes"]}
    assert set(by) == {"Branch", "Book", "Member", "Loan"}
    assert by["Branch"]["geo"] is True and by["Book"]["geo"] is False
    assert by["Book"]["volatile"] is False
    st = next(p for p in by["Book"]["properties"] if p["name"] == "status")
    assert st["enum"] == ["available", "on_loan", "lost"] and st["volatile"] is False and st["required"] is False
    assert {x["link"] for x in by["Book"]["links"]["out"]} == {"held_at", "recommends"}
    assert {x["link"] for x in by["Book"]["links"]["in"]} == {"loan_of", "recommends"}
    assert {l["name"] for l in t["linkTypes"]} == {"held_at", "loan_of", "borrowed_by", "recommends"}
    assert set(t["kpis"]) == {"books_on_loan", "total_pages"}
    assert t["role"] == "reader"


# ---------------------------------------------------------------- objects, masking, permissions
def test_object_crud_masking_and_permissions(lib):
    w = _world(lib)
    got = lib.get(f"/objects/Member/{w['m']}").json()
    assert got["name"] == "Mia" and got["email"] is None            # masked for the librarian
    assert lib.get(f"/objects/Member/{w['m']}", "admin").json()["email"] == "mia@example.org"
    assert lib.get(f"/objects/Member/{w['m']}", "reader").status_code == 404   # type not readable
    assert lib.post("/objects/Book", "reader", json={"title": "X"}).status_code == 403
    r = lib.c.put(f"/objects/Book/{w['a']}", headers=H("librarian"),
                  json={"title": "A", "pages": 110, "status": "available"})
    assert r.status_code == 200 and r.json()["pages"] == 110
    hist = lib.get(f"/objects/Book/{w['a']}/history").json()
    assert len(hist) >= 2
    assert lib.c.delete(f"/objects/Book/{w['a']}", headers=H("librarian")).status_code == 403   # admin only


# ---------------------------------------------------------------- geo
def test_geo_follows_ontology_geo_types(lib):
    w = _world(lib)
    fc = lib.get("/geo/Branch", "reader").json()
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 1
    f = fc["features"][0]
    assert f["id"] == w["br"] and f["geometry"]["coordinates"] == [2.35, 48.85]
    assert f["properties"]["name"] == "Central" and "origin" not in f["properties"]
    assert len(lib.get("/geo/Branch?bbox=0,40,5,50").json()["features"]) == 1
    assert len(lib.get("/geo/Branch?bbox=100,40,105,50").json()["features"]) == 0
    r = lib.get("/geo/Book")
    assert r.status_code == 404                                     # not a geo type here
    assert lib.get("/geo/Shipment").status_code == 404


# ---------------------------------------------------------------- graph (asserted + inferred), neighbours
def test_graph_with_inferred_edges(lib):
    w = _world(lib)
    pl = lib.app.state.runtime["pipeline"]
    assert pl not in (None, False)
    assert pl.cfg.namespace == "https://example.org/lib#"           # not the supply-chain namespace
    pl.sync()
    pl.maybe_reason(force=True)
    for role in ("admin", "librarian"):
        g = lib.get(f"/graph?center={w['a']}&depth=2", role).json()
        ids = {n["id"] for n in g["nodes"]}
        assert {w["a"], w["b"], w["c"], w["br"]} <= ids
        assert {n["type"] for n in g["nodes"]} == {"Book", "Branch"}
        inferred = {(e["src"], e["link"], e["dst"]) for e in g["edges"] if e["inferred"]}
        assert (w["a"], "recommends", w["c"]) in inferred, role      # transitive closure, visible to both
        assert (w["b"], "recommends", w["a"]) not in inferred
    # inverse name is inferred too
    g = lib.get(f"/graph?center={w['c']}&depth=2", "admin").json()
    assert (w["c"], "recommended_by", w["a"]) in {(e["src"], e["link"], e["dst"]) for e in g["edges"] if e["inferred"]}
    # a reader cannot read `recommends`: no edge through it, and nothing inferred from it
    gr = lib.get(f"/graph?center={w['a']}&depth=2", "reader").json()
    assert not any(e["link"] in ("recommends", "recommended_by") for e in gr["edges"])
    nb = lib.get(f"/neighbours/Book/{w['a']}", "reader").json()
    assert "recommends" not in str(nb)
    assert lib.get(f"/graph?center={w['m']}", "reader").status_code == 404   # hidden type == missing


# ---------------------------------------------------------------- stats
def test_stats_summary_generic_shape_and_kpis(lib):
    _world(lib)
    s = lib.get("/stats/summary").json()
    assert set(s) == {"as_of", "counts", "enums", "kpis"}           # no supply-chain legacy keys
    assert s["counts"]["Book"] >= 3 and s["counts"]["Branch"] == 1
    assert set(s["enums"]["Book"]["status"]) == {"available", "on_loan", "lost"}
    assert "email" not in str(s["enums"]) and "Member" in s["counts"]
    assert s["kpis"]["books_on_loan"] == 0 and s["kpis"]["total_pages"] >= 600
    sr = lib.get("/stats/summary", "reader").json()
    assert set(sr["counts"]) == {"Book", "Branch"}                  # only readable types
    ser = lib.get("/stats/series?metric=total_pages&step_hours=24").json()
    assert ser["metric"] == "total_pages" and len(ser["points"]) >= 2
    assert lib.get("/stats/series?metric=value_at_risk").status_code == 422
    assert lib.get("/stats/series?metric=bogus").status_code == 422


# ---------------------------------------------------------------- actions: execute / propose / approve
def test_actions_execute_propose_approve(lib):
    w = _world(lib)
    r = lib.post("/actions/checkout_book/execute", json={"params": {"book": w["b"]}})
    assert r.status_code == 200 and r.json()["status"] == "executed", r.text
    assert lib.get(f"/objects/Book/{w['b']}").json()["status"] == "on_loan"
    again = lib.post("/actions/checkout_book/execute", json={"params": {"book": w["b"]}})
    assert again.status_code == 422 and again.json()["code"] == "criteria_failed"
    assert lib.get("/stats/summary").json()["kpis"]["books_on_loan"] == 1
    assert lib.post("/actions/checkout_book/execute", "reader", json={"params": {"book": w["c"]}}).status_code == 403

    # approval-gated: execute turns into a pending proposal
    r = lib.post("/actions/lose_book/execute", json={"params": {"book": w["c"], "reason": "water"}})
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    pid = r.json()["proposal_id"]
    row = next(x for x in lib.get("/proposals").json() if x["id"] == pid)
    assert row["dry_run"]["diff"] and row["state"] == "pending"
    assert lib.post(f"/proposals/{pid}/approve").status_code == 403           # librarian is no approver
    ok = lib.post(f"/proposals/{pid}/approve", "admin", json={"comment": "ok"})
    assert ok.status_code == 200, ok.text
    assert lib.get(f"/objects/Book/{w['c']}").json()["status"] == "lost"

    # explicit propose route
    r = lib.post("/actions/lose_book/propose", json={"params": {"book": w["a"]}, "rationale": "gone"})
    assert r.status_code == 201
    rej = lib.post(f"/proposals/{r.json()['proposal_id']}/reject", "admin", json={"comment": "no"})
    assert rej.status_code == 200
    assert lib.get(f"/objects/Book/{w['a']}").json()["status"] == "available"
    ev = lib.get("/events").json()
    assert any(e["name"] == "checkout_book" for e in ev) and all(isinstance(e["summary"], str) for e in ev)


# ---------------------------------------------------------------- /sim is absent
def test_sim_routes_are_404_sim_unavailable(lib):
    for method, path in (("get", "/sim/state"), ("post", "/sim/control"), ("post", "/sim/inject")):
        r = getattr(lib.c, method)(path, headers=H("admin"), **({} if method == "get" else {"json": {"speed": "1", "kind": "port_closure"}}))
        assert r.status_code == 404 and r.json()["code"] == "sim_unavailable", (path, r.text)
    assert lib.app.state.runtime["runner"] is None                  # the runner was never constructed
    sid = lib.post("/scenarios", json={"name": "s"}).json()["id"]
    r = lib.post(f"/scenarios/{sid}/simulate", json={"days": 1})
    assert r.status_code == 404 and r.json()["code"] == "sim_unavailable"


# ---------------------------------------------------------------- no KPIs: proposals, scenarios, compare
def test_scenarios_and_dry_run_without_kpis(lib0):
    w = _world(lib0)
    assert lib0.get("/stats/summary").json()["kpis"] == {}
    assert lib0.get("/types").json()["kpis"] == {}
    sid = lib0.post("/scenarios", json={"name": "what if"}).json()["id"]
    # run an action inside the scenario, then compare with main
    r = lib0.post("/actions/checkout_book/execute", scn=sid, json={"params": {"book": w["a"]}})
    assert r.status_code == 200, r.text
    assert lib0.get(f"/objects/Book/{w['a']}", scn=sid).json()["status"] == "on_loan"
    assert lib0.get(f"/objects/Book/{w['a']}").json()["status"] == "available"
    cmp_ = lib0.get(f"/scenarios/{sid}/compare").json()
    assert cmp_["kpis"] == {"a": {}, "b": {}, "delta": {}}
    assert [d["type"] for d in cmp_["diff"]] == ["Book"] and cmp_["diff"][0]["op"] == "modify"
    assert lib0.get(f"/stats/summary", scn=sid).json()["enums"]["Book"]["status"]["on_loan"] == 1
    assert lib0.get(f"/scenarios/{sid}/series").status_code == 422     # no KPI to chart

    # proposal made inside the scenario: dry run carries an (empty) impact
    r = lib0.post("/actions/lose_book/propose", scn=sid, json={"params": {"book": w["b"]}, "rationale": "x"})
    assert r.status_code == 201, r.text
    pid = r.json()["proposal_id"]
    row = next(x for x in lib0.get("/proposals").json() if x["id"] == pid)
    assert row["dry_run"]["impact"]["delta"]["kpis"] == {} and row["dry_run"]["diff"]

    # merge: the human action replays on main; the approval-gated one becomes/keeps a proposal
    m = lib0.post(f"/scenarios/{sid}/merge", json={})
    assert m.status_code == 200, m.text
    assert lib0.get(f"/objects/Book/{w['a']}").json()["status"] == "on_loan"
    assert lib0.post("/scenarios", "reader", json={"name": "r"}).status_code in (201, 403)


# ---------------------------------------------------------------- agent (MockModel)
def _tu(i, name, /, **inp):
    return (i, name, inp)


def test_agent_run_proposes_and_scenario_tools_work(lib0):
    from agent import Turn
    w = _world(lib0)
    ev = lambda ref: {"ref": ref, "claim": "seen", "kind": "asserted", "tool_call_id": "q1"}  # noqa: E731
    lib0.scripts.append([
        Turn(tool_uses=[_tu("q1", "query_objects", type="Book", limit=10)], text="looking"),
        Turn(tool_uses=[_tu("s1", "scenario_open", name="try checkout")]),
        Turn(tool_uses=[_tu("s2", "scenario_apply", action="checkout_book", params={"book": w["c"]})]),
        Turn(tool_uses=[_tu("s3", "scenario_compare")]),
        Turn(tool_uses=[_tu("p1", "propose_action", action="lose_book",
                            params={"book": w["c"], "reason": "damaged"}, rationale="check",
                            evidence=[ev(f"Book:{w['c']}")])]),
        Turn(text="done"),
    ])
    r = lib0.post("/agent/runs", json={"prompt": "tidy the shelves"})
    assert r.status_code == 201, r.text
    j = lib0.wait_run(r.json()["run_id"])
    assert j["run"]["status"] == "completed", j["run"]
    res = {e["payload"]["tool_call_id"]: e["payload"]["result"] for e in j["events"] if e["kind"] == "tool_result"}
    for k in ("q1", "s1", "s2", "s3", "p1"):
        assert res[k].get("status") != "error" and "error" not in res[k], (k, res[k])
    assert res["s3"]["data"]["kpis"] == {"main": {}, "scenario": {}, "delta": {}}
    assert j["run"]["proposals"] == 1
    pid = j["run"]["proposal_ids"][0]
    p = next(x for x in lib0.get("/proposals").json() if x["id"] == pid)
    assert p["proposer"] == "agent:librarian" and p["on_behalf_of"] == "demo-librarian"
    assert p["dry_run"]["impact"]["delta"]["kpis"] == {}            # scenario evidence, no KPIs declared
    # events are paged: ?limit=2 returns a cursor
    pg = lib0.get(f"/agent/runs/{r.json()['run_id']}?limit=2").json()
    assert len(pg["events"]) == 2 and pg["next"] == pg["events"][-1]["seq"]
    rest = lib0.get(f"/agent/runs/{r.json()['run_id']}?after={pg['next']}&limit=2000").json()
    assert rest["next"] is None and rest["events"][0]["seq"] > pg["next"]
    # approve (as admin): executes on main
    ok = lib0.post(f"/proposals/{pid}/approve", "admin", json={"comment": "yes"})
    assert ok.status_code == 200, ok.text
    assert lib0.get(f"/objects/Book/{w['c']}").json()["status"] == "lost"


def test_agent_simulate_is_refused_without_a_sim(lib0):
    from agent import Turn
    from agent.policy import allowed_tools
    # the role's YAML does not list scenario_simulate and the ontology has no sim: never offered
    assert "scenario_simulate" not in allowed_tools(lib0.comp, "librarian")
    prompt_model = lib0.comp.model
    from agent.prompt import build_system_prompt
    sp = build_system_prompt(prompt_model, "librarian")
    assert "Library" in sp and "supply-chain" not in sp and "Shipment" not in sp


# ---------------------------------------------------------------- idempotent re-propose reports the true state
def test_idempotent_repropose_reports_current_state(lib0):
    from agent import AgentContext
    from agent import tools as T
    from engine.actions import decide, make_principal
    w = _world(lib0)
    book = lib0.make("Book", title="D", pages=1, status="available")
    ctx = AgentContext(make_principal("librarian", "lena", lib0.comp), lib0.comp,
                       lambda: connect(schema=lib0.schema))
    ctx.seen_ids[book] = "Book"
    ev = T.Evidence(ref=f"Book:{book}", claim="seen", kind="asserted", tool_call_id="q1")
    ctx.seen[f"Book:{book}"] = {"q1"}
    args = T.ProposeIn(action="lose_book", params={"book": book}, rationale="r", evidence=[ev])
    try:
        first = T.propose_action(ctx, ctx.conn(), args, "p1")
        assert first.data["status"] == "pending_approval"
        again = T.propose_action(ctx, ctx.conn(), args, "p2")
        assert again.data["proposal_id"] == first.data["proposal_id"] and again.data["status"] == "pending_approval"
        admin = make_principal("admin", "adam", lib0.comp)
        c = connect(schema=lib0.schema)
        decide(c, first.data["proposal_id"], admin, True, "ok", ontology=lib0.comp, source="api")
        c.close()
        done = T.propose_action(ctx, ctx.conn(), args, "p3")
        assert done.data["proposal_id"] == first.data["proposal_id"]
        assert done.data["status"] == "approved" and "already" in done.data["note"]
    finally:
        ctx.close()
