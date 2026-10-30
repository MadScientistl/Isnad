"""Human-readable rendering of a Run (used by scripts/demo_phase4.py)."""
from __future__ import annotations

import json
import re
import textwrap

from evals.harness import Run, comp

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def names_map(world) -> dict:
    """uuid -> readable name, from the demo operator's own view of the world (not what an agent sees)."""
    out = {}
    for group in ("port", "wh", "shipment", "invoice", "supplier"):
        for k, v in world.ids.get(group, {}).items():
            out[str(v)] = f"{k}" if group != "supplier" else f"Supplier {int(k):02d}"
    out[str(world.ids["closure_event"])] = "closure-event"
    out[str(world.ids["notice_event"])] = "port-notice-event"
    return out


def nice(text, names: dict) -> str:
    return UUID_RE.sub(lambda m: names.get(m.group(0), m.group(0)[:8]), str(text))


def _short(x, n=110) -> str:
    return textwrap.shorten(json.dumps(x, default=str), n, placeholder=" ...")


def describe(call: dict, res: dict | None, den: dict | None, role: str, names: dict) -> str:
    name, inp = call["name"], call.get("input") or {}
    head = f"{name}({nice(_short(inp, 90), names)})"
    if res is None:
        return head + "  -> (no result)"
    if not res.get("ok"):
        err = (res.get("result") or {}).get("error") or "error"
        if den:
            return head + f"\n        DENIED [{den['kind']}] {nice(err, names)}  (rejected before reaching the engine)"
        hid = "  <- hidden objects behave like missing ones" if re.search(r"unknown object type|not found", err) else ""
        return head + f"\n        ERROR: {nice(err, names)}{hid}"
    env = res["result"]
    d = env.get("data") or {}
    prov = env.get("provenance") or {}
    inf = len(prov.get("inferred", []))
    flags = f"  [telemetry: injection-like text flagged: {', '.join(res['flags'])}]" if res.get("flags") else ""
    if name == "query_objects":
        objs = d.get("objects", [])
        masked = []
        if objs:
            acc = comp().model.roles[role].types.get(d.get("type"))
            masked = sorted(acc.mask) if acc and acc.mask else []
        extra = f"; masked (null for this role): {', '.join(masked)}" if masked else ""
        return head + f"\n        {len(objs)} {d.get('type')} rows{extra}{flags}"
    if name == "impact_analysis":
        imp = d.get("impacted", [])
        return head + (f"\n        {len(imp)} impacted objects ({sum(i['basis'] == 'asserted' for i in imp)} asserted, "
                       f"{sum(i['basis'] == 'inferred' for i in imp)} inferred)")
    if name == "run_function":
        r = d.get("result") or {}
        if "count" in r:
            return head + f"\n        {d['function']}: {r['count']} shipments visible to this role"
        return head + f"\n        {d['function']}: {nice(_short({k: v for k, v in r.items() if k != 'reasons'}, 100), names)}"
    if name in ("traverse", "find_paths"):
        return head + f"\n        {len(d.get('nodes', []))} nodes, {len(d.get('edges', []))} edges, {inf} inferred edge(s) with provenance"
    if name == "get_history":
        return head + f"\n        {len(d.get('versions', []))} stored versions"
    if name == "diff":
        return head + f"\n        changed: {nice(_short(d.get('changed'), 120), names)}"
    if name == "as_of":
        return head + f"\n        state at {d.get('valid_at', '')[:10]}: {nice(_short((d.get('props') or d), 100), names)}"
    if name == "propose_action":
        return head + f"\n        proposal {d.get('proposal_id', '')[:8]} {d.get('status')} (nothing executed)"
    return head + f"\n        ok{flags}"


def dry_run_lines(p: dict, names: dict) -> list:
    out = []
    for d in (p.get("dry_run") or {}).get("diff", []):
        out.append(f"{d['op']:6} {d['kind']} {d['type']} {nice(d['key'], names)}")
    return out


def print_run(run: Run, world, title: str, indent: str = "") -> None:
    names = names_map(world)
    w = lambda s="": print(indent + s)  # noqa: E731
    w(f"--- {title} ---")
    w(f"prompt        : {run.prompt}")
    w(f"how           : {run.plan_label}")
    w(f"tools offered : {', '.join(run.tools)}")
    w(f"may propose   : {', '.join(run.actions) or '(nothing)'}   <- role.actions AND agent allowlist")
    for i, (c, r, d) in enumerate(run.calls(), 1):
        w(f"  {i:2}. " + describe(c, r, d, run.role, names).replace("\n", "\n" + indent))
    if run.proposals:
        w("proposals created (status: pending, nothing executed):")
        for p in run.proposals:
            params = nice(_short(p["params"], 140), names)
            w(f"  * {str(p['id'])[:8]} {p['action']} {params}")
            w(f"      proposer={p['proposer']} on_behalf_of={p['on_behalf_of']} expires={str(p['expires_at'])[:16]}")
            w(f"      rationale: {textwrap.shorten(p['rationale'] or '', 150)}")
            for e in p["evidence"] or []:
                w(f"      evidence [{e['kind'].upper():8}] {nice(e['ref'], names)}  ({e['tool_call_id']}) {e['claim']}")
            for line in dry_run_lines(p, names):
                w(f"      dry-run: {line}")
    else:
        w("proposals created: none")
    w("answer:")
    for line in nice(run.summary(), names).splitlines():
        w("    " + line)
    r = run.result
    est = " (mock: tokens estimated, priced at the fail-safe max rate)" if run.mode == "mock" else ""
    w(f"status={r.status}{'/' + r.cap if r.cap else ''} turns={r.turns} tool_calls={r.tool_calls} "
      f"tokens in/out={r.tokens_in}/{r.tokens_out} cost=${r.cost_usd:.4f}{est}")
    w()
