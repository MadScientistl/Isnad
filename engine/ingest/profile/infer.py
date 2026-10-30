"""Streaming per-column accumulator: type inference, null/distinct/top/min/max, geo detection."""
from __future__ import annotations

import json
import math
import re
import zlib
from collections import Counter

from ..names import clean_text

NULL_TOKENS = frozenset({"", "na", "n/a", "null", "none", "-", "?", "nan", "#n/a", "nil"})
BOOL_WORDS = frozenset({"true", "false", "t", "f", "yes", "no", "y", "n"})
INT_M = re.compile(r"[+-]?(?:0|[1-9][0-9]*)", re.ASCII).fullmatch
FLOAT_M = re.compile(r"[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII).fullmatch
ISO_M = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"(?:[T ](?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:[.,][0-9]{1,9})?)?"
    r"(?:Z|[+-][01][0-9](?::?[0-5][0-9])?)?)?", re.ASCII).fullmatch
DMY_M = re.compile(r"([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{4})(?:[T ]([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?))?", re.ASCII).fullmatch
WKT_M = re.compile(r"(?:SRID=[0-9]+;)?(?:POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)"
                   r"\s*(?:Z|M|ZM)?\s*\(", re.I | re.ASCII).match
PAIR_M = re.compile(r"\(?\s*(-?[0-9]+(?:\.[0-9]+)?)\s*[, ;]\s*(-?[0-9]+(?:\.[0-9]+)?)\s*\)?", re.ASCII).fullmatch
GEOJSON_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon",
                 "GeometryCollection", "Feature", "FeatureCollection"}

DISTINCT_CAP = 100_000
LC_BITS = 1 << 21
EPOCH_NAME = re.compile(r"(time|date|stamp|epoch|created|updated|(^|_)ts($|_)|_at$)", re.I)
POINT_NAME = re.compile(r"(loc|coord|position|point|latlon|latlng|geo)", re.I)


def _stats_dead(bad: int, n: int) -> bool:
    return bad > 0.01 * n + 5


class ColAcc:
    __slots__ = ("name", "declared", "rows", "nulls", "n", "vals", "lc", "counts", "counts_full", "topcap",
                 "i_alive", "f_alive", "b_alive", "iso_alive", "dmy_alive", "gj_alive", "wkt_alive", "pt_alive",
                 "i_bad", "f_bad", "iso_bad", "dmy_bad", "gj_bad", "wkt_bad", "pt_bad",
                 "fmin", "fmax", "fmin_s", "fmax_s", "smin", "smax", "iso_time", "dmy_a", "dmy_b", "dmy_seps",
                 "dmy_time", "gj_n", "gj_ok", "gj_types", "pt_lat_ok", "pt_lon_ok", "pt_swap_ok", "pt_hint", "is_numeric_name")

    def __init__(self, name: str, declared: str | None, topcap: int) -> None:
        self.name, self.declared, self.topcap = name, declared, topcap
        self.rows = self.nulls = self.n = 0
        self.vals: set[str] | None = set()
        self.lc: bytearray | None = None
        self.counts: Counter = Counter()
        self.counts_full = True
        self.i_alive = self.f_alive = self.b_alive = self.iso_alive = self.dmy_alive = True
        self.gj_alive = self.wkt_alive = True
        self.pt_hint = bool(POINT_NAME.search(name))
        self.pt_alive = self.pt_hint
        self.i_bad = self.f_bad = self.iso_bad = self.dmy_bad = self.gj_bad = self.wkt_bad = self.pt_bad = 0
        self.fmin = math.inf
        self.fmax = -math.inf
        self.fmin_s = self.fmax_s = None
        self.smin = self.smax = None
        self.iso_time = False
        self.dmy_a = self.dmy_b = 0
        self.dmy_seps: set[str] = set()
        self.dmy_time = False
        self.gj_n = self.gj_ok = 0
        self.gj_types: Counter = Counter()
        self.pt_lat_ok = self.pt_lon_ok = self.pt_swap_ok = 0
        self.is_numeric_name = False

    # -- streaming ---------------------------------------------------------------------------
    def add_block(self, vals) -> None:
        self.rows += len(vals)
        non: list[str] = []
        append = non.append
        for v in vals:
            if v is None:
                continue
            s = v.strip()
            if len(s) < 5 and s.lower() in NULL_TOKENS:
                continue
            append(s)
        self.nulls += len(vals) - len(non)
        k = len(non)
        if not k:
            return
        self.n += k
        if self.vals is not None:
            self.vals.update(non)
            if len(self.vals) > DISTINCT_CAP and len(self.vals) < self.n:
                self._spill()
        else:
            lc = self.lc
            for s in non:
                h = zlib.crc32(s.encode("utf-8", "replace"))
                lc[(h & (LC_BITS - 1)) >> 3] |= 1 << (h & 7)
        if self.counts_full:
            self.counts.update(non)
            if len(self.counts) > self.topcap:
                self.counts_full = False
        lo, hi = min(non), max(non)
        if self.smin is None or lo < self.smin:
            self.smin = lo
        if self.smax is None or hi > self.smax:
            self.smax = hi
        n = self.n
        if self.f_alive:
            good = [s for s in non if FLOAT_M(s)]
            self.f_bad += k - len(good)
            if good:
                fl = list(map(float, good))
                lo_f, hi_f = min(fl), max(fl)
                if lo_f < self.fmin:
                    self.fmin, self.fmin_s = lo_f, good[fl.index(lo_f)]
                if hi_f > self.fmax:
                    self.fmax, self.fmax_s = hi_f, good[fl.index(hi_f)]
            if self.i_alive:
                self.i_bad += k - sum(1 for s in good if INT_M(s))
                if _stats_dead(self.i_bad, n):
                    self.i_alive = False
            if _stats_dead(self.f_bad, n):
                self.f_alive = self.i_alive = False
        if self.b_alive:
            u = set(non)
            if len(u) > 6 or any(x.lower() not in BOOL_WORDS for x in u):
                self.b_alive = False
        if self.iso_alive:
            good = [s for s in non if ISO_M(s)]
            self.iso_bad += k - len(good)
            if not self.iso_time and any(len(s) > 10 for s in good):
                self.iso_time = True
            if _stats_dead(self.iso_bad, n):
                self.iso_alive = False
        if self.dmy_alive:
            for s in non:
                m = DMY_M(s)
                if m is None:
                    self.dmy_bad += 1
                    continue
                a, b = int(m.group(1)), int(m.group(3))
                if a > 31 or b > 31 or a == 0 or b == 0 or (a > 12 and b > 12):
                    self.dmy_bad += 1
                    continue
                self.dmy_seps.add(m.group(2))
                if a > self.dmy_a:
                    self.dmy_a = a
                if b > self.dmy_b:
                    self.dmy_b = b
                if m.group(5):
                    self.dmy_time = True
            if _stats_dead(self.dmy_bad, n) or len(self.dmy_seps) > 1:
                self.dmy_alive = False
        if self.gj_alive:
            gj = [s for s in non if s[:1] == "{" and '"type"' in s[:2000]]
            self.gj_bad += k - len(gj)
            if self.gj_n < 40:
                for s in gj[: 40 - self.gj_n]:
                    self.gj_n += 1
                    try:
                        d = json.loads(s)
                        t = d.get("type") if isinstance(d, dict) else None
                    except (ValueError, RecursionError):
                        t = None
                    if t in GEOJSON_TYPES:
                        self.gj_ok += 1
                        self.gj_types[t] += 1
            if _stats_dead(self.gj_bad, n):
                self.gj_alive = False
        if self.wkt_alive:
            self.wkt_bad += k - sum(1 for s in non if WKT_M(s))
            if _stats_dead(self.wkt_bad, n):
                self.wkt_alive = False
        if self.pt_alive:
            for s in non:
                m = PAIR_M(s)
                if m is None:
                    self.pt_bad += 1
                    continue
                a, b = float(m.group(1)), float(m.group(2))
                if -90 <= a <= 90 and -180 <= b <= 180:
                    self.pt_lat_ok += 1
                elif -90 <= b <= 90 and -180 <= a <= 180:
                    self.pt_swap_ok += 1
                else:
                    self.pt_bad += 1
            if _stats_dead(self.pt_bad, n):
                self.pt_alive = False

    def _spill(self) -> None:
        lc = bytearray(LC_BITS >> 3)
        for s in self.vals:
            h = zlib.crc32(s.encode("utf-8", "replace"))
            lc[(h & (LC_BITS - 1)) >> 3] |= 1 << (h & 7)
        self.lc, self.vals = lc, None

    # -- results -----------------------------------------------------------------------------
    def distinct(self) -> tuple[int, bool]:
        if self.vals is not None:
            return len(self.vals), True
        ones = sum(bin(b).count("1") for b in self.lc)
        zeros = LC_BITS - ones
        est = int(round(-LC_BITS * math.log(zeros / LC_BITS))) if zeros else self.n
        return min(max(est, DISTINCT_CAP), self.n), False

    def resolve(self) -> tuple[str, str | None, dict | None, list[str]]:
        """-> (type, format, geo or None (without pair), notes)"""
        n, notes = self.n, []
        if n == 0:
            return "string", None, None, ["all values null"]
        tol = 0.01 * n
        geo = None
        if self.gj_alive and self.gj_bad <= tol and self.gj_n and self.gj_ok / self.gj_n >= 0.9:
            top = self.gj_types.most_common(1)[0][0] if self.gj_types else "unknown"
            return "geojson", None, {"role": "geojson", "pair": None}, [f"geojson_type={top}"]
        if self.wkt_alive and self.wkt_bad <= tol:
            return "string", None, {"role": "wkt", "pair": None}, ["wkt geometry text"]
        if self.pt_alive and self.pt_bad <= tol and (self.pt_lat_ok + self.pt_swap_ok) > 0:
            if self.pt_swap_ok > self.pt_lat_ok:
                notes.append("pair looks like lon,lat order")
            else:
                notes.append("pair assumed to be lat,lon order; unverified")
            return "string", None, {"role": "point", "pair": None}, notes
        dist_vals = self.vals
        if self.declared and "bool" in self.declared.lower() and dist_vals is not None and dist_vals <= {"0", "1"}:
            return "bool", None, None, notes
        if self.b_alive:
            return "bool", None, None, notes
        if self.i_alive and self.i_bad <= tol:
            if self.i_bad:
                notes.append(f"{self.i_bad} values do not parse as int")
            if (EPOCH_NAME.search(self.name) and self.fmin_s is not None):
                if 1e9 <= self.fmin and self.fmax <= 4.1e9:
                    return "timestamp", "epoch_s", None, notes
                if 1e12 <= self.fmin and self.fmax <= 4.1e12:
                    return "timestamp", "epoch_ms", None, notes
            return "int", None, None, notes
        if self.f_alive and self.f_bad <= tol:
            if self.f_bad:
                notes.append(f"{self.f_bad} values do not parse as float")
            return "float", None, None, notes
        if self.iso_alive and self.iso_bad <= tol:
            if self.iso_bad:
                notes.append(f"{self.iso_bad} values do not parse as timestamp")
            return "timestamp", "iso8601", None, notes
        if self.dmy_alive and self.dmy_bad <= tol and self.dmy_seps:
            sep = next(iter(self.dmy_seps))
            if self.dmy_a > 12:
                order = "dd{0}mm"
            elif self.dmy_b > 12:
                order = "mm{0}dd"
            else:
                order = "dd{0}mm"
                notes.append("ambiguous dd/mm vs mm/dd (all parts <= 12); assumed day first")
            fmt = order.format(sep) + sep + "yyyy" + (" HH:MM[:SS]" if self.dmy_time else "")
            if self.dmy_bad:
                notes.append(f"{self.dmy_bad} values do not parse as timestamp")
            return "timestamp", fmt, None, notes
        if self.f_bad or self.i_bad:
            near = min(x for x in (self.i_bad, self.f_bad) if x) if (self.i_bad and self.f_bad) else (self.f_bad or self.i_bad)
            if self.n and near / self.n < 0.2 and self.f_alive:
                notes.append("mostly numeric but with non-numeric values; kept as string")
        return "string", None, None, notes

    def top(self, k: int) -> list[list]:
        merged: dict[str, int] = {}
        for v, c in self.counts.items():
            cv = clean_text(v, 80)
            merged[cv] = merged.get(cv, 0) + c
        return [[v, c] for v, c in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]

    def minmax(self, typ: str, fmt: str | None) -> tuple[str | None, str | None]:
        if self.n == 0:
            return None, None
        if typ in ("int", "float") or (typ == "timestamp" and fmt and fmt.startswith("epoch")):
            return clean_text(self.fmin_s, 80), clean_text(self.fmax_s, 80)
        if typ == "timestamp" and fmt != "iso8601":
            return None, None
        if typ in ("bool", "geojson"):
            return None, None
        return clean_text(self.smin, 80), clean_text(self.smax, 80)
