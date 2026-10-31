"""Record and replay model turns as JSON, so proposal generation can run offline in CI via `MockModel`.

A cassette is `{"version":1, "name", "description", "turns":[{"text", "tool_uses":[{"id","name","input"}], "usage":[in,out]}]}`.
Hand-authored cassettes may use placeholders that are resolved against the dataset's profile at load time (column ids
depend on the profiler's numbering):  "@tbl(file)" -> table id,  "@col(file|Column name)" -> column id, "@fmt(file|Column name)" -> the profiler's format string of that column,
"@profile_hash". Recorded cassettes contain concrete ids and need no resolver.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent.mock_model import MockModel, Turn
from agent.types import ModelTurn

_PH = re.compile(r"@(tbl|col|fmt)\(([^)|]*?)(?:\|([^)]*))?\)|@profile_hash")


def _stem(s: str) -> str:
    s = s.strip().lower()
    return s[:-4] if s.endswith((".csv", ".tsv")) else s


class Resolver:
    def __init__(self, profile_dict: dict, profile_hash: str = ""):
        self.hash = profile_hash
        self.tables = {_stem(t.get("name", "")): t for t in profile_dict.get("tables", [])}
        self.cols, self.fmts = {}, {}
        for t in profile_dict.get("tables", []):
            for c in t.get("columns", []):
                k = (_stem(t.get("name", "")), str(c.get("name", "")).strip()[:40])
                self.cols[k], self.fmts[k] = c["id"], c.get("format") or "iso8601"

    def sub(self, s: str) -> str:
        def rep(m):
            if m.group(0) == "@profile_hash":
                return self.hash
            kind, a, b = m.group(1), m.group(2), m.group(3)
            if kind == "tbl":
                t = self.tables.get(_stem(a))
                if t is None:
                    raise KeyError(f"cassette placeholder: no table {a!r}")
                return t["id"]
            key = (_stem(a), (b or "").strip()[:40])   # profilers truncate long (hostile) names
            if key not in self.cols:
                raise KeyError(f"cassette placeholder: no column {a!r}|{b!r}")
            return self.cols[key] if kind == "col" else self.fmts[key]
        return _PH.sub(rep, s)

    def walk(self, x):
        if isinstance(x, str):
            return self.sub(x) if "@" in x else x
        if isinstance(x, dict):
            return {k: self.walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [self.walk(v) for v in x]
        return x


def load_cassette(path, resolver: Resolver | None = None) -> MockModel:
    data = json.loads(Path(path).read_text())
    if data.get("version") != 1:
        raise ValueError("unsupported cassette version")
    turns = []
    for t in data["turns"]:
        uses = [(u["id"], u["name"], resolver.walk(u["input"]) if resolver else u["input"]) for u in t.get("tool_uses", [])]
        turns.append(Turn(text=t.get("text", ""), tool_uses=uses, usage=tuple(t.get("usage", (100, 50))),
                          stop_reason=t.get("stop_reason")))
    m = MockModel(turns)
    m.name = data.get("model", "mock-model")
    return m


class RecordingModel:
    """Wraps a real ModelClient and records each turn; `save` writes the cassette (concrete ids, no placeholders)."""

    def __init__(self, inner):
        self.inner, self.name, self.turns = inner, inner.name, []

    def complete(self, **kw) -> ModelTurn:
        t = self.inner.complete(**kw)
        self.turns.append({"text": t.text, "stop_reason": t.stop_reason,
                           "usage": [t.usage.get("input_tokens", 0), t.usage.get("output_tokens", 0)],
                           "tool_uses": [{"id": u.id, "name": u.name, "input": u.input} for u in t.tool_uses]})
        return t

    def save(self, path, *, name: str = "", description: str = "") -> None:
        Path(path).write_text(json.dumps({"version": 1, "name": name, "description": description,
                                          "model": self.name, "turns": self.turns}, indent=1) + "\n")
