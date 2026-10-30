"""Identifier safety shared by all ingest code.

Every identifier that reaches generated YAML or DDL passes through ``safe_prop`` / ``safe_type`` /
``safe_link``. Original (untrusted) text is only ever kept as data (a ``label``).

- safe_prop / safe_link: ``[a-z][a-z0-9_]{0,40}``
- safe_type: ``[A-Z][A-Za-z0-9]{0,40}`` (no underscore is allowed, so collisions get ``2``, ``3`` ...
  instead of ``_2``)

Pass ``taken`` (a mutable set of already used identifiers) to get collision handling: the result is
made unique and added to the set. Without ``taken`` the functions are pure.
"""
from __future__ import annotations

import re
import unicodedata

MAX_LEN = 41  # 1 + 40

# Engine-owned column names.
ENGINE_RESERVED = frozenset({
    "id", "valid_during", "recorded_during", "changed_by", "action_id", "scenario_id", "src", "dst",
})

# PostgreSQL reserved / non-reserved-but-risky keywords (lower case) plus a few ontology words.
SQL_KEYWORDS = frozenset("""
all analyse analyze and any array as asc asymmetric authorization binary both case cast check collate
collation column concurrently constraint create cross current_catalog current_date current_role
current_schema current_time current_timestamp current_user default deferrable desc distinct do else end
except false fetch for foreign freeze from full grant group having ilike in initially inner intersect
into is isnull join lateral leading left like limit localtime localtimestamp natural not notnull null
offset on only or order outer overlaps placing primary references returning right select session_user
similar some symmetric system_user table tablesample then to trailing true union unique user using
variadic verbose when where window with
abort alter begin between bigint bit boolean call cascade char character checkpoint close cluster comment
commit copy cursor database date decimal declare delete double drop exists explain float function
index insert int integer interval key language level lock move numeric nothing offset option owner
partition position precision prepare procedure public range real release rename replace reset restrict
revoke role rollback row rows rule savepoint schema sequence set show smallint start statement
temporary text time timestamp transaction trigger truncate type unknown update vacuum value values
varchar view void work
""".split())

ONTOLOGY_RESERVED = frozenset({"object", "link", "thing", "class", "uuid", "geometry", "geography"})

_RESERVED_PROP = ENGINE_RESERVED | SQL_KEYWORDS
_RESERVED_TYPE = frozenset(w.lower() for w in (SQL_KEYWORDS | ONTOLOGY_RESERVED | ENGINE_RESERVED))

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NON_ALNUM_ANY = re.compile(r"[^A-Za-z0-9]+")
_WS = re.compile(r"\s+")


_FOLD = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L",
                       "đ": "d", "Đ": "D", "ð": "d", "þ": "th"})


def _ascii_fold(s: object) -> str:
    text = "" if s is None else str(s).translate(_FOLD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")  # control, format, surrogate...
    return text.encode("ascii", "ignore").decode("ascii")


def clean_text(s: object, maxlen: int = 80) -> str:
    """Make untrusted text safe to display or put in a profile: strip control / format / private-use
    characters (incl. bidi and zero-width), collapse whitespace, truncate to ``maxlen`` chars."""
    text = "" if s is None else str(s)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\t\n\r\x0b\x0c" or cat in ("Zs", "Zl", "Zp"):
            out.append(" ")
        elif cat[0] == "C":
            continue
        else:
            out.append(ch)
    text = _WS.sub(" ", "".join(out)).strip()
    if len(text) > maxlen:
        text = text[: max(0, maxlen - 1)] + "…" if maxlen > 1 else text[:maxlen]
    return text


def _finish(base: str, empty: str, prefix: str, reserved: frozenset[str], suffix: str, sep: str,
            taken: set[str] | None, first_ok) -> str:
    if not base:
        base = empty
    if not first_ok(base[0]):
        base = prefix + base
    base = base[:MAX_LEN]
    if base.lower() in reserved:
        base = base[: MAX_LEN - len(suffix)] + suffix
    if taken is not None:
        cand, n = base, 2
        while cand.lower() in {t.lower() for t in taken}:
            tail = f"{sep}{n}"
            cand = base[: MAX_LEN - len(tail)] + tail
            n += 1
        base = cand
        taken.add(base)
    return base


def _snake(s: object) -> str:
    text = _ascii_fold(s)
    text = _CAMEL.sub("_", text).lower()
    return _NON_ALNUM.sub("_", text).strip("_")


def safe_prop(s: object, taken: set[str] | None = None) -> str:
    base = _snake(s)
    return _finish(base, "col", "c_", _RESERVED_PROP, "_x", "_", taken, lambda c: "a" <= c <= "z")


def safe_link(s: object, taken: set[str] | None = None) -> str:
    base = _snake(s)
    return _finish(base, "link", "l_", _RESERVED_PROP, "_x", "_", taken, lambda c: "a" <= c <= "z")


def safe_type(s: object, taken: set[str] | None = None) -> str:
    text = _ascii_fold(s)
    words = [w for w in _NON_ALNUM_ANY.split(text) if w]
    base = "".join(w[0].upper() + w[1:] for w in words)
    return _finish(base, "Entity", "T", _RESERVED_TYPE, "X", "", taken, lambda c: "A" <= c <= "Z")
