"""Shared helpers for the ontology-generator tests (no network, no database)."""
from __future__ import annotations

import json
from pathlib import Path

from agent.mock_model import MockModel, Turn
from agent.ontogen.cassette import Resolver
from agent.ontogen.generate import profile_hash_of
from agent.ontogen.tools import as_dict
from evals.ontogen import make_cassettes as mc
from evals.ontogen.run import load_profile

DATA = Path(__file__).resolve().parents[3] / "evals" / "ontogen" / "datasets"


def profile_of(ds: str) -> dict:
    return as_dict(load_profile(DATA / ds)[0])


def resolver(pd: dict) -> Resolver:
    return Resolver(pd, profile_hash_of(pd))


def widget_proposal(pd: dict, **over) -> dict:
    """A minimal valid proposal over the injection dataset's plain `widgets` table."""
    p = mc.wrap({"objectTypes": [mc.typ("Widget", "widgets", "widget_id", [
        mc.s("code", "widgets", "widget_id", req=True), mc.s("name", "widgets", "name", req=True),
        mc.fl("price", "widgets", "price"), mc.en("condition", "widgets", "condition", ["new", "used"])])],
        "linkTypes": []}, "plain widgets")
    p = resolver(pd).walk(p)
    p.update(over)
    return p


def tid(pd: dict, name: str) -> str:
    return next(t["id"] for t in pd["tables"] if t["name"] == name)


def script(*turns):
    """Turns: (name, input) tuples or (text,) for a text-only end."""
    out = []
    for i, t in enumerate(turns):
        if len(t) == 1:
            out.append(Turn(text=t[0]))
        else:
            out.append(Turn(tool_uses=[(f"tu{i}", t[0], t[1])]))
    return MockModel(out)


def ok_report():
    return {"ok": True, "checks": [{"name": "key_violations", "ok": True, "value": 0, "threshold": 0}], "stats": {}}


def bad_report(example="bad\x07 value"):
    return {"ok": False, "checks": [{"name": "enum_violation_rate", "ok": False, "value": 0.5, "threshold": 0.02,
                                     "detail": "x" * 400, "examples": [example, "b", "c", "d-extra"]}], "stats": {}}


def feedback_blocks(model: MockModel, call_index: int) -> list:
    msgs = model.calls[call_index]["messages"]
    last = msgs[-1]["content"]
    return [b for b in last if isinstance(b, dict) and b.get("type") == "text"]


def loads_env(text: str) -> dict:
    return json.loads(text[text.index("{"):])
