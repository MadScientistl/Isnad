"""Source rows + import mapping -> typed object rows and link rows (shared by validation and the importer).

Pure functions plus a streaming API:

    stats = new_stats()
    for uuid, props in iter_objects(source, mapping, "Shipment", stats=stats): ...
    for src, dst in iter_links(source, mapping, "carrier", stats=stats): ...

``mapping`` is the dict produced by ``engine.ingest.proposal.to_yaml`` (``types`` / ``links``). It carries no
namespace and no enum values, so call ``enrich_mapping(mapping, yaml_text)`` once (it adds ``namespace`` and the
``enum`` list of enum columns). Without it ids fall back to the namespace ``to_yaml`` derives when no slug is given,
and enum membership is only checked for ``enum_map`` targets that are listed (never silently wrong: unknown enum
values are then not checked and the database CHECK would refuse them).

Rules (contract):
  * Object id = ``uuid5(NAMESPACE_URL, f"{namespace}{Type}/{key values joined by \\x1f}")``. A key value is the
    cell text with control characters removed and surrounding whitespace stripped; the profiler's null tokens
    ("", "NA", "n/a", "null", "none", "-", "?", "nan", "#n/a", "nil") are null. A null key part drops the row (counted).
  * The same key repeated with identical values (case-insensitive, trimmed) is merged silently (counted in
    ``rows_merged``); the same key with conflicting values counts as ``duplicate_keys`` (the first row wins).
  * Coercion failures never raise: the value becomes null and is counted per property (<= 3 sanitised examples).
    A required property that ends up null (null cell or failed coercion) drops the row (``null_required``).
  * Transforms: none / int / float / bool / timestamp:<fmt> / enum_map:<json> / point(lat,lon) / geojson.
      - int: ``12``, ``+12``, ``12.0`` (integral), ``1,234`` (comma only as a US thousands separator, groups of 3)
      - float: plain/exponent notation, ``1,234.5``; never nan/inf; no European decimal comma
      - bool: true/false, t/f, yes/no, y/n, 1/0 (case-insensitive)
      - timestamp: ``iso8601`` (date, or date + time, optional fraction and zone; naive = UTC), ``epoch_s``,
        ``epoch_ms``, ``dd/mm/yyyy`` and ``mm/dd/yyyy`` (separators / . -, optional time ``HH:MM[:SS]``), or any
        ``%``-style strptime format. Dates are never guessed: with ``dd/mm`` ``02/13/2020`` is a failure.
      - point(lat_col,lon_col): GeoJSON Point; lat in [-90,90], lon in [-180,180]; a null half makes the value null
        (one half only is a failure)
      - geojson: a GeoJSON Point / LineString (or a Feature holding one) matching the property type
  * Link ids are computed the same way from the key cells. A link whose endpoint is not among the objects loaded in
    the same ``MatStats`` is dropped and counted (``unresolved_links`` = ``dropped_links``); repeated identical pairs
    are merged (``duplicate_links``); null foreign keys are optional edges (``null_endpoints``).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Iterator

from .names import clean_text
from .profile.infer import NULL_TOKENS

SEP = "\x1f"
UTC = dt.timezone.utc
MAX_EXAMPLES = 3
_BAD = object()
_I64 = 2 ** 63


class MappingError(ValueError):
    """The mapping does not fit the source (unknown table / column, malformed entry). The message is safe to show."""


# ---------------------------------------------------------------------------------------------- stats
@dataclass
class PropStats:
    nonnull: int = 0
    nulls: int = 0
    coercion_failures: int = 0
    enum_violations: int = 0
    null_required: int = 0
    coercion_examples: list = field(default_factory=list)
    enum_examples: list = field(default_factory=list)


@dataclass
class TypeStats:
    rows_read: int = 0
    rows_loaded: int = 0
    rows_merged: int = 0          # identical repeats of an already seen key (merged silently)
    duplicate_keys: int = 0       # same key, conflicting values
    null_keys: int = 0
    rows_dropped: int = 0         # evaluated rows not loaded (required property null / failed)
    props: dict = field(default_factory=dict)
    examples: dict = field(default_factory=dict)       # "duplicate_keys" / "null_keys" / "dropped" -> [str]
    ids: set | None = None

    def prop(self, name: str) -> PropStats:
        p = self.props.get(name)
        if p is None:
            p = self.props[name] = PropStats()
        return p

    def example(self, kind: str, text: str) -> None:
        lst = self.examples.setdefault(kind, [])
        if len(lst) < MAX_EXAMPLES:
            lst.append(clean_text(text, 80))

    @property
    def rows_evaluated(self) -> int:
        return self.rows_loaded + self.rows_dropped

    @property
    def null_required(self) -> int:
        return sum(p.null_required for p in self.props.values())


@dataclass
class LinkStats:
    rows_read: int = 0            # candidate edges with both endpoints present in the source row
    links_loaded: int = 0
    unresolved_links: int = 0
    dropped_links: int = 0
    duplicate_links: int = 0
    null_endpoints: int = 0
    examples: list = field(default_factory=list)

    @property
    def resolution(self) -> float:
        return 1.0 if not self.rows_read else (self.rows_read - self.unresolved_links) / self.rows_read


class MatStats:
    def __init__(self, track_ids: bool = True) -> None:
        self.track_ids = track_ids
        self.types: dict[str, TypeStats] = {}
        self.links: dict[str, LinkStats] = {}

    def type(self, name: str) -> TypeStats:
        t = self.types.get(name)
        if t is None:
            t = self.types[name] = TypeStats(ids=set() if self.track_ids else None)
        return t

    def link(self, name: str) -> LinkStats:
        l = self.links.get(name)
        if l is None:
            l = self.links[name] = LinkStats()
        return l

    def to_dict(self) -> dict:
        types = {}
        for n, t in self.types.items():
            types[n] = {
                "rows_read": t.rows_read, "rows_loaded": t.rows_loaded, "rows_merged": t.rows_merged,
                "duplicate_keys": t.duplicate_keys, "null_keys": t.null_keys, "rows_dropped": t.rows_dropped,
                "null_required": {k: p.null_required for k, p in t.props.items() if p.null_required},
                "coercion_failures": {k: {"count": p.coercion_failures, "examples": list(p.coercion_examples)}
                                      for k, p in t.props.items() if p.coercion_failures},
                "enum_violations": {k: {"count": p.enum_violations, "examples": list(p.enum_examples)}
                                    for k, p in t.props.items() if p.enum_violations},
                "examples": {k: list(v) for k, v in t.examples.items()}}
        links = {n: {"rows_read": l.rows_read, "links_loaded": l.links_loaded, "unresolved_links": l.unresolved_links,
                     "dropped_links": l.dropped_links, "duplicate_links": l.duplicate_links,
                     "null_endpoints": l.null_endpoints, "resolution": round(l.resolution, 6),
                     "examples": list(l.examples)} for n, l in self.links.items()}
        return {"types": types, "links": links}


def new_stats(*, track_ids: bool = True) -> MatStats:
    """Counters for one load. ``track_ids`` keeps the loaded object ids (16-byte ints) so link endpoints can be checked."""
    return MatStats(track_ids)


# ---------------------------------------------------------------------------------------------- cells
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def norm_cell(v: object) -> str | None:
    """Cell -> stripped text without control characters, or None for null / null tokens."""
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    if _CTRL.search(s):
        s = _CTRL.sub("", s)
    s = s.strip()
    if not s or (len(s) < 5 and s.lower() in NULL_TOKENS):
        return None
    return s


def object_id(namespace: str, type_name: str, key_parts: Iterable[str]) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}{type_name}/{SEP.join(key_parts)}")


# ---------------------------------------------------------------------------------------------- scalar parsers
_INT_RE = re.compile(r"[+-]?[0-9]{1,30}", re.A).fullmatch
_THOU_RE = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?", re.A).fullmatch
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.A).fullmatch
_BOOL = {"true": True, "t": True, "yes": True, "y": True, "1": True,
         "false": False, "f": False, "no": False, "n": False, "0": False}


def parse_float(s: str):
    if _THOU_RE(s):
        s = s.replace(",", "")
    if not _FLOAT_RE(s):
        return _BAD
    try:
        f = float(s)
    except (ValueError, OverflowError):
        return _BAD
    return f if math.isfinite(f) else _BAD


def parse_int(s: str):
    if _THOU_RE(s):
        s = s.replace(",", "")
    if _INT_RE(s):
        v = int(s)
        return v if -_I64 < v < _I64 else _BAD
    f = parse_float(s)
    if f is _BAD or f != int(f) or abs(f) >= _I64:
        return _BAD
    return int(f)


def parse_bool(s: str):
    return _BOOL.get(s.lower(), _BAD)


_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]+([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]{1,9}))?)?"
    r"(Z|z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?", re.A).fullmatch
_DMY = re.compile(r"([0-9]{1,2})([/.\-])([0-9]{1,2})\2([0-9]{4})(?:[T ]+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?)?",
                  re.A).fullmatch


def _tz(z: str | None):
    if not z or z in ("Z", "z"):
        return UTC
    sign = -1 if z[0] == "-" else 1
    d = z[1:].replace(":", "")
    h, m = int(d[:2]), int(d[2:4] or 0)
    if h > 23 or m > 59:
        raise ValueError("tz")
    return dt.timezone(sign * dt.timedelta(hours=h, minutes=m))


def parse_iso(s: str):
    m = _ISO(s)
    if not m:
        return _BAD
    y, mo, d, hh, mi, ss, frac, z = m.groups()
    try:
        micro = int((frac or "0").ljust(6, "0")[:6])
        v = dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), micro, tzinfo=_tz(z))
        return v.astimezone(UTC)
    except (ValueError, OverflowError):
        return _BAD


def _parse_dmy(s: str, day_first: bool):
    m = _DMY(s)
    if not m:
        return _BAD
    a, _sep, b, y, hh, mi, ss = m.groups()
    day, mon = (int(a), int(b)) if day_first else (int(b), int(a))
    try:
        return dt.datetime(int(y), mon, day, int(hh or 0), int(mi or 0), int(ss or 0), tzinfo=UTC)
    except ValueError:
        return _BAD


def _parse_epoch(s: str, ms: bool):
    f = parse_float(s)
    if f is _BAD:
        return _BAD
    if ms:
        f /= 1000.0
    if not (0 <= f <= 1e11):
        return _BAD
    try:
        return dt.datetime.fromtimestamp(f, UTC)
    except (OverflowError, OSError, ValueError):
        return _BAD


@lru_cache(maxsize=64)
def _ts_parser(fmt: str):
    f = (fmt or "iso8601").strip()
    low = f.lower()
    if low == "epoch_s":
        return lambda s: _parse_epoch(s, False)
    if low == "epoch_ms":
        return lambda s: _parse_epoch(s, True)
    if "%" in f:
        def strp(s, f=f):
            try:
                v = dt.datetime.strptime(s, f)
            except (ValueError, OverflowError):
                return _BAD
            return (v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC))
        return strp
    m = re.match(r"(dd|mm)([/.\-])(dd|mm)\2yyyy", low)
    if m and m.group(1) != m.group(3):
        day_first = m.group(1) == "dd"
        return lambda s: _parse_dmy(s, day_first)
    if "yyyy" in f:
        # readable pattern: yyyy mm dd Mon Month HH MM(minutes) SS, "[:SS]" optional seconds
        base = f.replace("[:SS]", "\0")
        for a, b in (("yyyy", "%Y"), ("Month", "%B"), ("Mon", "%b"), ("dd", "%d"), ("mm", "%m"), ("HH", "%H"),
                     ("MM", "%M"), ("SS", "%S")):
            base = base.replace(a, b)
        cands = [base.replace("\0", ":%S"), base.replace("\0", "")] if "\0" in base else [base]
        if re.search(r"%[^YBbdmHMS]", base.replace("%%", "")) is None:
            def tok(s, cands=cands):
                for c in cands:
                    try:
                        return dt.datetime.strptime(s, c).replace(tzinfo=UTC)
                    except (ValueError, OverflowError):
                        continue
                return _BAD
            return tok
    return parse_iso


def parse_timestamp(s: str, fmt: str = "iso8601"):
    """-> tz-aware UTC datetime or the failure sentinel (``is_bad``)."""
    return _ts_parser(fmt)(s)


def is_bad(v: object) -> bool:
    return v is _BAD


# ---------------------------------------------------------------------------------------------- geometry
def _lonlat(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def make_point(lat_s: str, lon_s: str):
    la, lo = parse_float(lat_s), parse_float(lon_s)
    if la is _BAD or lo is _BAD or not _lonlat(la, lo):
        return _BAD
    return {"type": "Point", "coordinates": [lo, la]}


def _pos(p) -> list | None:
    if not isinstance(p, (list, tuple)) or len(p) < 2 or len(p) > 3:
        return None
    try:
        lon, lat = float(p[0]), float(p[1])
    except (TypeError, ValueError):
        return None
    if isinstance(p[0], bool) or isinstance(p[1], bool) or not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return [lon, lat] if _lonlat(lat, lon) else None


def parse_geojson(s: str, want: str):
    """``want`` is "point" or "linestring"."""
    if len(s) > 200_000 or s[:1] != "{":
        return _BAD
    try:
        d = json.loads(s)
    except (ValueError, RecursionError):
        return _BAD
    if isinstance(d, dict) and d.get("type") == "Feature":
        d = d.get("geometry")
    if not isinstance(d, dict):
        return _BAD
    t, c = d.get("type"), d.get("coordinates")
    if want == "point" and t == "Point":
        p = _pos(c)
        return {"type": "Point", "coordinates": p} if p else _BAD
    if want == "linestring" and t == "LineString" and isinstance(c, list) and 2 <= len(c) <= 10_000:
        ps = [_pos(x) for x in c]
        return {"type": "LineString", "coordinates": ps} if all(ps) else _BAD
    return _BAD


# ---------------------------------------------------------------------------------------------- mapping
_COL_RE = re.compile(r"t([0-9]{1,4})\.c([0-9]{1,4})")
_POINT_RE = re.compile(r"point\((t[0-9]{1,4}\.c[0-9]{1,4}),(t[0-9]{1,4}\.c[0-9]{1,4})\)")


def enrich_mapping(mapping: dict, yaml_text: str, *, namespace: str | None = None) -> dict:
    """Copy of ``mapping`` with ``namespace`` and, for enum columns, ``enum`` (the allowed values) taken from the
    generated YAML. Call once before ``iter_objects`` / ``iter_links``."""
    from engine.compiler import CompiledOntology
    model = CompiledOntology.from_text(yaml_text).model
    out = json.loads(json.dumps(mapping))
    out["namespace"] = namespace or model.namespace
    for tn, spec in (out.get("types") or {}).items():
        ot = model.object_types.get(tn)
        if ot is None:
            continue
        props = {p.name: p for p in ot.properties}
        for pn, cs in (spec.get("columns") or {}).items():
            p = props.get(pn)
            if p is not None and p.type == "enum":
                cs["enum"] = list(p.enum)
    return out


def mapping_namespace(mapping: dict, namespace: str | None = None) -> str:
    ns = namespace or mapping.get("namespace")
    if ns:
        return str(ns)
    from .proposal import derive_namespace
    return derive_namespace("w" + str(mapping.get("profile_hash") or "")[:10])


def _tables(source) -> list:
    t = getattr(source, "_isnad_tables", None)
    if t is None:
        t = list(source.tables())
        try:
            source._isnad_tables = t
        except AttributeError:  # pragma: no cover - slotted source
            pass
    return t


def _table(source, tid: str):
    m = re.fullmatch(r"t([0-9]{1,4})", str(tid))
    ts = _tables(source)
    if not m or not (1 <= int(m.group(1)) <= len(ts)):
        raise MappingError(f"unknown source table {clean_text(tid, 20)!r}")
    return ts[int(m.group(1)) - 1]


def _col(table, tid: str, col_id: str) -> int:
    m = _COL_RE.fullmatch(str(col_id))
    if not m or f"t{m.group(1)}" != tid or not (1 <= int(m.group(2)) <= len(table.columns)):
        raise MappingError(f"unknown source column {clean_text(col_id, 20)!r} in table {clean_text(tid, 20)!r}")
    return int(m.group(2)) - 1


class _Prop:
    __slots__ = ("name", "ptype", "required", "col", "tr", "fmt", "emap", "emap_ci", "enum", "enum_ci", "pt")

    def __init__(self, name: str, spec: dict, table, tid: str):
        self.name = name
        self.ptype = str(spec.get("type") or "string")
        self.required = bool(spec.get("required"))
        tr = str(spec.get("transform") or "none")
        self.fmt = "iso8601"
        self.emap = self.emap_ci = None
        self.pt = None
        self.col = None
        if tr.startswith("point("):
            m = _POINT_RE.fullmatch(tr)
            if not m:
                raise MappingError(f"{clean_text(name, 40)}: malformed point transform")
            self.pt = (_col(table, tid, m.group(1)), _col(table, tid, m.group(2)))
            self.tr = "point"
        elif tr.startswith("timestamp:"):
            self.tr, self.fmt = "ts", tr[10:]
        elif tr.startswith("enum_map:"):
            try:
                m = json.loads(tr[9:])
            except ValueError:
                raise MappingError(f"{clean_text(name, 40)}: enum_map is not JSON") from None
            if not isinstance(m, dict):
                raise MappingError(f"{clean_text(name, 40)}: enum_map is not an object")
            self.tr = "enum_map"
            self.emap = {str(k): str(v) for k, v in m.items()}
            self.emap_ci = {}
            for k, v in self.emap.items():
                self.emap_ci.setdefault(k.strip().casefold(), v)
        elif tr in ("none", "int", "float", "bool", "geojson"):
            self.tr = tr
        else:
            raise MappingError(f"{clean_text(name, 40)}: unknown transform {clean_text(tr, 30)!r}")
        c = spec.get("col")
        if c is not None:
            self.col = _col(table, tid, c)
        elif self.pt is None:
            self.tr = "nocol"
        en = spec.get("enum")
        self.enum = frozenset(en) if en else None
        self.enum_ci = None
        if en:
            self.enum_ci = {}
            for v in en:
                self.enum_ci.setdefault(v.strip().casefold(), v)

    # -> (value, err) with err in (None, "coerce", "enum")
    def convert(self, cells: dict):
        if self.pt is not None:
            a, b = cells.get(self.pt[0]), cells.get(self.pt[1])
            if a is None and b is None:
                return None, None, None
            if a is None or b is None:
                return None, "coerce", f"{a}|{b}"
            v = make_point(a, b)
            return (None, "coerce", f"{a},{b}") if v is _BAD else (v, None, None)
        raw = cells.get(self.col) if self.col is not None else None
        if raw is None:
            return None, None, None
        pt, tr = self.ptype, self.tr
        if pt == "enum":
            return self._enum(raw)
        if pt in ("point", "linestring"):
            if tr != "geojson":
                return None, "coerce", raw
            v = parse_geojson(raw, pt)
            return (None, "coerce", raw) if v is _BAD else (v, None, None)
        if tr == "geojson":
            return None, "coerce", raw
        if tr in ("none", "enum_map"):
            v = self._by_type(raw, pt)
        else:
            if tr == "int":
                v = parse_int(raw)
            elif tr == "float":
                v = parse_float(raw)
            elif tr == "bool":
                v = parse_bool(raw)
            elif tr == "ts":
                v = parse_timestamp(raw, self.fmt)
            else:
                v = _BAD
            if v is not _BAD:
                v = self._adapt(v, pt)
        return (None, "coerce", raw) if v is _BAD else (v, None, None)

    def _enum(self, raw: str):
        if self.tr == "enum_map":
            m = self.emap.get(raw)
            if m is None:
                m = self.emap.get(raw.strip())
            if m is None:
                m = self.emap_ci.get(raw.casefold())
            if m is None:
                return None, "enum", raw
            if self.enum is not None and m not in self.enum:
                return None, "enum", raw
            return m, None, None
        if self.enum is None:
            return raw, None, None
        if raw in self.enum:
            return raw, None, None
        v = self.enum_ci.get(raw.casefold())
        return (v, None, None) if v is not None else (None, "enum", raw)

    @staticmethod
    def _by_type(raw: str, pt: str):
        if pt == "string":
            return raw
        if pt == "int":
            return parse_int(raw)
        if pt == "float":
            return parse_float(raw)
        if pt == "bool":
            return parse_bool(raw)
        if pt == "timestamp":
            return parse_iso(raw)
        return _BAD

    @staticmethod
    def _adapt(v, pt: str):
        if pt == "string":
            if isinstance(v, str):
                return v
            if isinstance(v, bool):
                return "true" if v else "false"
            return v.isoformat() if isinstance(v, dt.datetime) else str(v)
        if pt == "int":
            if isinstance(v, bool):
                return _BAD
            if isinstance(v, int):
                return v
            return int(v) if isinstance(v, float) and v == int(v) and abs(v) < _I64 else _BAD
        if pt == "float":
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else _BAD
        if pt == "bool":
            return v if isinstance(v, bool) else _BAD
        if pt == "timestamp":
            return v if isinstance(v, dt.datetime) else _BAD
        return _BAD


class _Plan:
    def __init__(self, source, mapping: dict, type_name: str, namespace: str | None):
        spec = (mapping.get("types") or {}).get(type_name)
        if not isinstance(spec, dict):
            raise MappingError(f"type {clean_text(type_name, 40)!r} is not in the mapping")
        self.name = type_name
        self.ns = mapping_namespace(mapping, namespace)
        self.tid = str(spec.get("table"))
        self.table = _table(source, self.tid)
        self.key = [_col(self.table, self.tid, k) for k in (spec.get("key_cols") or [])]
        if not self.key:
            raise MappingError(f"type {clean_text(type_name, 40)!r} has no key")
        self.props = [_Prop(n, cs, self.table, self.tid) for n, cs in (spec.get("columns") or {}).items()]
        need = set(self.key)
        sig = set()
        for p in self.props:
            for c in ((p.col,) if p.col is not None else ()) + (p.pt or ()):
                need.add(c)
                sig.add(c)
        self.need = sorted(need)
        self.sig = sorted(sig)

    def cells(self, row) -> dict:
        n = len(row)
        return {j: norm_cell(row[j] if j < n else None) for j in self.need}


# ---------------------------------------------------------------------------------------------- objects
def iter_objects(source, mapping: dict, type_name: str, *, limit: int | None = None, stats: MatStats | None = None,
                 rows: set | None = None, ids: set | None = None, namespace: str | None = None
                 ) -> Iterator[tuple[uuid.UUID, dict]]:
    """Yield ``(uuid, props)`` for every distinct key of ``type_name`` (see the module docstring for the rules).

    ``rows`` / ``ids`` (optional) restrict the scan to those 0-based row indexes of the source table, plus any row
    whose id is in ``ids`` (a row is read when it matches either); both None reads everything. ``limit`` stops after
    that many yielded objects. Raises MappingError for a mapping that does not fit the source."""
    stats = stats if stats is not None else new_stats()
    plan = _Plan(source, mapping, type_name, namespace)
    ts = stats.type(type_name)
    restrict = rows is not None or ids is not None
    last_row = max(rows) if rows else -1
    seen: dict[int, int] = {}
    for idx, row in enumerate(plan.table.rows()):
        by_row = (not restrict) or (rows is not None and idx in rows)
        if not by_row and ids is None:
            if idx > last_row:
                return
            continue
        cells = plan.cells(row)
        parts = [cells[j] for j in plan.key]
        if any(p is None for p in parts):
            if by_row:
                ts.rows_read += 1
                ts.null_keys += 1
                ts.example("null_keys", f"row {idx + 2}: empty key")
            continue
        uid = object_id(plan.ns, type_name, parts)
        if not by_row and uid not in ids:
            continue
        ts.rows_read += 1
        h = uid.int
        sig = hash(tuple((cells[j] or "").casefold() for j in plan.sig))
        prev = seen.get(h)
        if prev is not None:
            if prev == sig:
                ts.rows_merged += 1
            else:
                ts.duplicate_keys += 1
                ts.example("duplicate_keys", f"key {clean_text(SEP.join(parts).replace(SEP, '|'), 40)} has conflicting rows")
            continue
        seen[h] = sig
        props: dict[str, Any] = {}
        drop = False
        for p in plan.props:
            v, err, raw = p.convert(cells)
            ps = ts.prop(p.name)
            if v is None:
                if err is None:
                    ps.nulls += 1
                else:
                    ps.nonnull += 1
                    if err == "coerce":
                        ps.coercion_failures += 1
                        if len(ps.coercion_examples) < MAX_EXAMPLES:
                            ps.coercion_examples.append(clean_text(raw, 80))
                    else:
                        ps.enum_violations += 1
                        if len(ps.enum_examples) < MAX_EXAMPLES:
                            ps.enum_examples.append(clean_text(raw, 80))
                if p.required:
                    ps.null_required += 1
                    drop = True
            else:
                ps.nonnull += 1
                props[p.name] = v
        if drop:
            ts.rows_dropped += 1
            ts.example("dropped", f"row {idx + 2}: required property empty or invalid")
            continue
        ts.rows_loaded += 1
        if ts.ids is not None:
            ts.ids.add(h)
        yield uid, props
        if limit is not None and ts.rows_loaded >= limit:
            return


# ---------------------------------------------------------------------------------------------- links
def iter_links(source, mapping: dict, link_name: str, *, stats: MatStats | None = None, rows: set | None = None,
               src_ids: set | None = None, only_loaded_src: bool = False, namespace: str | None = None
               ) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Yield ``(src_uuid, dst_uuid)`` for a mapped link. Endpoints are checked against the objects loaded in
    ``stats`` (types never iterated there are assumed present). ``rows`` limits the scan to those row indexes,
    ``src_ids`` to edges starting at those ids (both used for sampling); ``only_loaded_src`` skips (uncounted) edges
    whose source row was not loaded, e.g. dropped or not sampled."""
    stats = stats if stats is not None else new_stats()
    spec = (mapping.get("links") or {}).get(link_name)
    if not isinstance(spec, dict):
        raise MappingError(f"link {clean_text(link_name, 40)!r} is not in the mapping")
    ns = mapping_namespace(mapping, namespace)
    ls = stats.link(link_name)
    ft, tt = str(spec.get("from_type")), str(spec.get("to_type"))
    types = mapping.get("types") or {}
    for t in (ft, tt):
        if t not in types:
            raise MappingError(f"link {clean_text(link_name, 40)!r}: type {clean_text(t, 40)!r} is not in the mapping")
    tid = str(spec.get("table"))
    table = _table(source, tid)
    kind = spec.get("kind")
    composite = False
    if kind == "fk":
        src_cols = [_col(table, tid, c) for c in (spec.get("from_key_cols") or [])]
        dst_col = _col(table, tid, spec.get("from_col"))
        to_keys = list(types[tt].get("key_cols") or [])
        composite = to_keys != [spec.get("to_type_key")]
        if not src_cols:
            raise MappingError(f"link {clean_text(link_name, 40)!r}: no source key")
    elif kind == "join_table":
        src_cols = [_col(table, tid, spec.get("src_col"))]
        dst_col = _col(table, tid, spec.get("dst_col"))
    else:
        raise MappingError(f"link {clean_text(link_name, 40)!r}: unknown kind")
    f_ids = stats.types[ft].ids if ft in stats.types else None
    t_ids = stats.types[tt].ids if tt in stats.types else None
    seen: set[tuple[int, int]] = set()
    last_row = max(rows) if rows else -1
    for idx, row in enumerate(table.rows()):
        if rows is not None:
            if idx not in rows:
                if idx > last_row:
                    return
                continue
        n = len(row)
        sparts = [norm_cell(row[j] if j < n else None) for j in src_cols]
        d = norm_cell(row[dst_col] if dst_col < n else None)
        if any(p is None for p in sparts) or d is None:
            if d is None and not any(p is None for p in sparts):
                ls.null_endpoints += 1
            continue
        s_id = object_id(ns, ft, sparts)
        if src_ids is not None and s_id not in src_ids:
            continue
        if only_loaded_src and f_ids is not None and s_id.int not in f_ids:
            continue
        ls.rows_read += 1
        d_id = object_id(ns, tt, [d])
        bad = composite or (f_ids is not None and s_id.int not in f_ids) or (t_ids is not None and d_id.int not in t_ids)
        if bad:
            ls.unresolved_links += 1
            ls.dropped_links += 1
            if len(ls.examples) < MAX_EXAMPLES:
                ls.examples.append(clean_text(f"row {idx + 2}: target {clean_text(d, 40)!r} not found", 80))
            continue
        k = (s_id.int, d_id.int)
        if k in seen:
            ls.duplicate_links += 1
            continue
        seen.add(k)
        ls.links_loaded += 1
        yield s_id, d_id
