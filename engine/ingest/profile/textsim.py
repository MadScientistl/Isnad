"""Name similarity helpers for FK inference."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^a-z0-9]+")
IDISH = {"id", "code", "key", "no", "num", "number", "ref", "fk", "uuid", "guid", "pk"}
PARENTISH = {"parent", "manager", "supervisor", "boss", "master", "super", "owner", "predecessor"}


def tokens(name: str) -> list[str]:
    return [t for t in _SPLIT.split(_CAMEL.sub("_", str(name)).lower()) if t]


def singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def stem(name: str) -> list[str]:
    toks = tokens(name)
    while toks and toks[-1] in IDISH:
        toks.pop()
    return [singular(t) for t in toks]


def has_idish_suffix(name: str) -> bool:
    toks = tokens(name)
    return bool(toks) and toks[-1] in IDISH


def name_score(from_col: str, to_table: str, to_col: str, same_table: bool = False) -> float:
    ft = stem(from_col)
    tt = [singular(t) for t in tokens(to_table)]
    a = 0.0
    if ft and tt:
        if ft == tt:
            a = 1.0
        elif len(ft) >= len(tt) and ft[-len(tt):] == tt:
            a = 0.9
        elif len(tt) > len(ft) and tt[-len(ft):] == ft:
            a = 0.8
        else:
            r = SequenceMatcher(None, "".join(ft), "".join(tt)).ratio()
            a = r if r >= 0.75 else 0.0
    if same_table and ft and ft[-1] in PARENTISH:
        a = max(a, 0.8)
    fc, tc = "_".join(tokens(from_col)), "_".join(tokens(to_col))
    b = 0.0
    if fc and fc == tc:
        b = 1.0
    elif has_idish_suffix(from_col) and has_idish_suffix(to_col):
        b = 0.6
    return min(1.0, 0.7 * a + 0.3 * b)
