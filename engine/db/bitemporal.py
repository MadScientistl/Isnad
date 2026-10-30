"""Bitemporal data engine. The ONLY code that splits/stitches ranges.

Model: every row has valid_during (when the fact is true in the world) and
recorded_during (when the database believed it). "Current knowledge" is the set
of rows whose recorded_during is open-ended (upper_inf). A write closes the
recorded range of every affected current row and inserts replacement rows with
recorded_during = [now, infinity). Nothing is ever updated in place, so every
past belief stays queryable.

Open-ended ranges are stored as UNBOUNDED upper bounds (NULL), not the literal
'infinity' bound: Postgres reports upper_inf() = false for the latter and
psycopg cannot load 'infinity' timestamps. Python represents "open" as None.

Public API: write_object, delete_object, write_link, end_link, get_object,
history, list_objects, as_of queries, neighbours, link_history, bulk_load_*.
"""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

COMMON_COLS = ("valid_during", "recorded_during", "changed_by", "action_id", "scenario_id")

_cols_cache: dict[tuple, tuple[str, ...]] = {}
_rd_cache: dict[tuple, bool] = {}


# ---------------------------------------------------------------- helpers
def snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.lower()


def obj_table(type_: str) -> str:
    return f"obj_{snake(type_)}"


def link_table(link: str) -> str:
    return f"link_{snake(link)}"


_MAX = datetime.max.replace(tzinfo=UTC)


def _open(ts):
    """Normalise an open-ended bound: None or an 'infinity' loaded as datetime.max."""
    return None if ts is None or ts >= _MAX else ts


def _utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _tbl(name: str) -> sql.Identifier:
    return sql.Identifier(name)


_COLS_SQL = """SELECT a.attname, t.typname FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
               WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
               ORDER BY a.attnum"""


def _sp(conn) -> str:
    return conn.execute("SELECT current_schemas(false)::text").fetchone()[0]


def clear_caches() -> None:
    """Forget cached column lists / view existence (call after DDL on the same process)."""
    _cols_cache.clear()
    _geo_cache.clear()
    _rd_cache.clear()


def _prop_cols(conn: psycopg.Connection, table: str, keycols: Sequence[str]) -> tuple[str, ...]:
    key = (_sp(conn), table, tuple(keycols))
    if key not in _cols_cache:
        cols = [r[0] for r in conn.execute(_COLS_SQL, [table]).fetchall()]
        if not cols:
            raise LookupError(f"table {table} not found on search_path")
        skip = set(COMMON_COLS) | set(keycols)
        _cols_cache[key] = tuple(c for c in cols if c not in skip)
    return _cols_cache[key]


_geo_cache: dict[tuple, tuple[tuple[str, ...], tuple[str, ...]]] = {}


def _table_cols(conn: psycopg.Connection, table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(all columns in order, geometry columns) of `table` on the current search_path."""
    key = (_sp(conn), table)
    if key not in _geo_cache:
        rows = conn.execute(_COLS_SQL, [table]).fetchall()
        if not rows:
            raise LookupError(f"table {table} not found on search_path")
        _geo_cache[key] = (tuple(r[0] for r in rows),
                           tuple(r[0] for r in rows if r[1] == "geometry"))
    return _geo_cache[key]


def read_source(conn, table: str, masked: bool = True) -> str:
    """Relation to read an object table through. masked => the generated `rd_<table>` view
    (property masking + RLS, contract Phase 3) when it exists in this schema; otherwise the
    base table. The fallback ONLY applies while the views are absent (pre-Phase-3 schemas).
    Link tables are read directly."""
    if not masked or not table.startswith("obj_"):
        return table
    rd = f"rd_{table}"
    key = (_sp(conn), rd)
    if key not in _rd_cache:
        _rd_cache[key] = conn.execute("SELECT to_regclass(%s) IS NOT NULL", [rd]).fetchone()[0]
    if _rd_cache[key]:
        return rd
    if conn.execute("SELECT current_user = 'amarh_app'").fetchone()[0]:
        raise LookupError(f"masked view {rd} is missing: refusing to read the base table as a scoped role")
    return table


def geometry_columns(conn: psycopg.Connection, table_or_type: str) -> tuple[str, ...]:
    """Geometry column names of a table (or of an object type's table)."""
    t = table_or_type if table_or_type.startswith(("obj_", "link_")) else obj_table(table_or_type)
    return _table_cols(conn, t)[1]


def _point_hex(coords) -> str:
    import struct
    return (b"\x01" + struct.pack("<IIdd", 0x20000001, 4326, float(coords[0]), float(coords[1]))).hex()


def _geom_hex(conn, g) -> str | None:
    """Normalise a geometry value (GeoJSON dict/str, or EWKB hex) to canonical EWKB hex."""
    import json
    if g is None:
        return None
    if isinstance(g, (bytes, memoryview)):
        g = bytes(g).hex()
    if isinstance(g, str) and not g.lstrip().startswith("{"):
        return conn.execute("SELECT encode(ST_AsEWKB(%s::geometry), 'hex')", [g]).fetchone()[0]
    if isinstance(g, str):
        g = json.loads(g)
    if g.get("type") == "Point" and len(g.get("coordinates") or ()) == 2:
        return _point_hex(g["coordinates"])
    return conn.execute(
        "SELECT encode(ST_AsEWKB(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)), 'hex')",
        [json.dumps(g)]).fetchone()[0]


def _select_list(conn, table: str) -> sql.Composable:
    cols, geo = _table_cols(conn, table)
    return sql.SQL(", ").join(
        sql.SQL("ST_AsGeoJSON({c})::jsonb AS {c}").format(c=_tbl(c)) if c in geo else _tbl(c)
        for c in cols)


def _now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT clock_timestamp()").fetchone()[0]


def _lock(conn: psycopg.Connection, table: str, keyvals: Sequence[Any], scenario_id: int) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        [f"{table}:{scenario_id}:{':'.join(str(k) for k in keyvals)}"],
    )


def _lt(a: datetime | None, b: datetime | None) -> bool:
    """a < b where None means +infinity."""
    if a is None:
        return False
    if b is None:
        return True
    return a < b


def _min(a, b):
    return a if _lt(a, b) else b


def _max(a, b):
    return b if _lt(a, b) else a



# ------------------------------------------------------------ scenarios (Phase 5)
class ScenarioLimit(Exception):
    """A scenario reached AMARH_SCN_MAX_ROWS override rows."""


ZERO_UUID = UUID(int=0)
_SCN_META = ("valid_during", "recorded_during", "changed_by", "action_id", "scenario_id")


def _max_rows() -> int:
    return int(os.environ.get("AMARH_SCN_MAX_ROWS", "50000"))


def _scn_ctx(conn, sid: int) -> tuple[datetime, datetime | None]:
    """(base_recorded, clock) of scenario `sid`. Taken from the scoped GUCs when they name this scenario,
    otherwise from the scenario tables (owner / loader / sim code). A scoped (amarh_app) connection whose GUC
    names a different scenario is refused: reads and writes are only possible inside the scoped scenario."""
    g_sid, g_base, g_now, cu = conn.execute(
        "SELECT coalesce(nullif(current_setting('amarh.scenario', true), ''), '0')::int, "
        "nullif(current_setting('amarh.scenario_base', true), '')::timestamptz, "
        "nullif(current_setting('amarh.now', true), '')::timestamptz, current_user").fetchone()
    if cu == "amarh_app" and g_sid != sid:
        raise ValueError(f"scenario {sid} is not the scoped scenario ({g_sid or 'main'})")
    if g_sid == sid and g_base is not None:
        return g_base, g_now
    row = conn.execute(
        "SELECT s.base_recorded, st.clock FROM scenario s LEFT JOIN scenario_state st ON st.scenario_id = s.id "
        "WHERE s.id = %s", [sid]).fetchone()
    if row is None:
        raise ValueError(f"scenario {sid} not found")
    return row[0], (g_now if g_sid == sid and g_now is not None else row[1])


def _scn_where(conn, kind: str, name: str, sid: int, as_of_valid, as_of_recorded) -> tuple[str, list]:
    """WHERE fragment (on the object/link relation, or its rd_ view) selecting the rows that are EFFECTIVE in
    scenario `sid` at the given instants: rows of the scenario itself, plus main rows as believed at the fork
    (never later than `as_of_recorded`) for keys the scenario has not touched. Mirrors compiler eff_pred."""
    base, clock = _scn_ctx(conn, sid)
    v = _utc(as_of_valid) if as_of_valid is not None else None
    r = _utc(as_of_recorded) if as_of_recorded is not None else None
    vexpr = "%s::timestamptz" if v is not None else "coalesce(%s::timestamptz, now())"
    vp = v if v is not None else clock
    key = "o.key_a = id" if kind == "obj" else "o.key_a = src AND o.key_b = dst"
    rec_s, rp = ("recorded_during @> %s::timestamptz", [r]) if r is not None else \
        ("(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity')", [])
    main_rec = min(r, base) if r is not None else base
    frag = (f"((scenario_id = %s AND valid_during @> {vexpr} AND {rec_s}) OR "
            f"(scenario_id = 0 AND valid_during @> {vexpr} AND recorded_during @> %s::timestamptz AND NOT EXISTS ("
            f"SELECT 1 FROM scenario_override o WHERE o.scenario_id = %s AND o.kind = %s AND o.type = %s AND {key})))")
    return frag, [sid, vp, *rp, vp, main_rec, sid, kind, name]


def _is_first_touch(conn, kind, type_name, keyvals, sid) -> bool:
    """True when `sid` has no override row for this key yet (so the write must clone main's rows). Enforces the
    AMARH_SCN_MAX_ROWS cap before any row is written."""
    kb = keyvals[1] if len(keyvals) > 1 else ZERO_UUID
    if conn.execute("SELECT 1 FROM scenario_override WHERE scenario_id = %s AND kind = %s AND type = %s "
                    "AND key_a = %s AND key_b = %s", [sid, kind, type_name, keyvals[0], kb]).fetchone():
        return False
    st = conn.execute("SELECT override_rows FROM scenario_state WHERE scenario_id = %s FOR UPDATE", [sid]).fetchone()
    if st is None:
        raise ValueError(f"scenario {sid} not found")
    if st[0] >= _max_rows():
        raise ScenarioLimit(f"scenario {sid} reached {_max_rows()} override rows (AMARH_SCN_MAX_ROWS)")
    return True


def _clone_from_main(conn, table, kind, type_name, keycols, keyvals, sid, now, skip_lowers) -> None:
    """Clone-on-first-touch: copy ALL of main's current-knowledge rows of the key as of `base_recorded` into
    scenario `sid` (valid ranges preserved, recorded_during re-based to [now, inf)), except the versions whose
    valid lower bound is in `skip_lowers` (the ones the write replaces: they are never copied, so no scenario
    row ever has to be deleted). Then record the override row (with the fork-time `base`) and bump the counter."""
    kb = keyvals[1] if len(keyvals) > 1 else ZERO_UUID
    base, clock = _scn_ctx(conn, sid)
    keyw = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(_tbl(k)) for k in keycols)
    cols = _table_cols(conn, table)[0]
    # what main looked like for this key at the fork (masked source: override rows are readable by shared roles)
    drop = (*_SCN_META, *keycols)
    expr = sql.SQL("to_jsonb(r)") + sql.SQL("").join(sql.SQL(" - {}").format(sql.Literal(c)) for c in drop)
    src = read_source(conn, table, True)
    props = conn.execute(
        sql.SQL("SELECT {e} FROM {t} r WHERE {kw} AND scenario_id = 0 AND recorded_during @> %s::timestamptz "
                "AND valid_during @> coalesce(%s::timestamptz, now())").format(e=expr, t=_tbl(src), kw=keyw),
        [*keyvals, base, clock]).fetchone()
    main_lower = conn.execute(
        sql.SQL("SELECT lower(recorded_during) FROM {t} WHERE {kw} AND scenario_id = 0 AND "
                "(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity') AND "
                "valid_during @> coalesce(%s::timestamptz, now())").format(t=_tbl(table), kw=keyw),
        [*keyvals, clock]).fetchone()
    sel = sql.SQL(", ").join(
        sql.SQL("tstzrange(%s, NULL, '[)')") if c == "recorded_during"
        else sql.SQL("%s") if c == "scenario_id" else _tbl(c) for c in cols)
    conn.execute(
        sql.SQL("INSERT INTO {t} ({c}) SELECT {s} FROM {t} WHERE {kw} AND scenario_id = 0 "
                "AND recorded_during @> %s::timestamptz AND NOT EXISTS ("
                "SELECT 1 FROM unnest(%s::timestamptz[]) u WHERE u IS NOT DISTINCT FROM lower(valid_during))").format(
            t=_tbl(table), c=sql.SQL(", ").join(_tbl(c) for c in cols), s=sel, kw=keyw),
        [now, sid, *keyvals, base, list(skip_lowers)])
    info = {"present": props is not None, "props": props[0] if props else None,
            "main_lower": main_lower[0].isoformat() if main_lower else None}
    conn.execute("INSERT INTO scenario_override (scenario_id, kind, type, key_a, key_b, deleted, base) "
                 "VALUES (%s,%s,%s,%s,%s,false,%s)", [sid, kind, type_name, keyvals[0], kb, Jsonb(info)])
    conn.execute("UPDATE scenario_state SET override_rows = override_rows + 1 WHERE scenario_id = %s", [sid])


def _after_scenario_write(conn, table, kind, type_name, keycols, keyvals, sid) -> None:
    """Bump the graph cache key and refresh the tombstone flag of the override row."""
    kb = keyvals[1] if len(keyvals) > 1 else ZERO_UUID
    _, clock = _scn_ctx(conn, sid)
    keyw = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(_tbl(k)) for k in keycols)
    alive = conn.execute(
        sql.SQL("SELECT EXISTS (SELECT 1 FROM {t} WHERE {kw} AND scenario_id = %s AND "
                "(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity') AND "
                "valid_during @> coalesce(%s::timestamptz, now()))").format(t=_tbl(table), kw=keyw),
        [*keyvals, sid, clock]).fetchone()[0]
    conn.execute("UPDATE scenario_override SET deleted = %s WHERE scenario_id = %s AND kind = %s AND type = %s "
                 "AND key_a = %s AND key_b = %s", [not alive, sid, kind, type_name, keyvals[0], kb])
    conn.execute("UPDATE scenario_state SET data_version = data_version + 1 WHERE scenario_id = %s", [sid])


def list_overrides(conn, scenario_id: int) -> list[dict]:
    """The scenario's override rows (scenario_id, kind, type, key_a, key_b, deleted, base), ordered by kind,
    type, keys. `base` = fork-time snapshot `{"present": bool, "props": {...}|None, "main_lower": iso|None}`;
    for a scoped caller who is not the owner/admin of the scenario (a shared role) it is None."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM amarh_scenario_overrides(%s)", [scenario_id])
        return cur.fetchall()


# ------------------------------------------------------------ write core
def _apply(
    conn, table, keycols, keyvals, props, valid_from, valid_to, changed_by, action_id,
    scenario_id, kind, type_name, outbox_extra,
) -> bool:
    """Set the valid-time interval [valid_from, valid_to) to `props` (None = absent).

    Returns True if anything changed (and an outbox row was written).
    """
    vf, vt = _utc(valid_from), _utc(valid_to)
    if vf is None:
        raise ValueError("valid_from is required")
    if vt is not None and vt <= vf:
        raise ValueError("valid_to must be after valid_from")
    pcols = _prop_cols(conn, table, keycols)
    geo = set(_table_cols(conn, table)[1])
    if props is not None:
        unknown = set(props) - set(pcols)
        if unknown:
            raise ValueError(f"unknown properties for {table}: {sorted(unknown)}")
        newp = tuple(_geom_hex(conn, props.get(c)) if c in geo else props.get(c) for c in pcols)
    else:
        newp = None

    return _apply_tx(conn, table, keycols, keyvals, newp, pcols, geo, vf, vt, changed_by, action_id,
                     scenario_id, kind, type_name, outbox_extra)


def _apply_tx(conn, table, keycols, keyvals, newp, pcols, geo, vf, vt, changed_by, action_id,
              scenario_id, kind, type_name, outbox_extra) -> bool:
    with conn.transaction():
        _lock(conn, table, keyvals, scenario_id)
        now = _now(conn)
        first = False  # first touch of this key in a scenario: the logic runs on main's rows as of the fork
        if scenario_id:
            _scn_ctx(conn, scenario_id)  # refuses a scoped session whose scenario differs
            first = _is_first_touch(conn, kind, type_name, keyvals, scenario_id)
        keyw = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(_tbl(k)) for k in keycols)
        sel = sql.SQL(
            "SELECT {pc}, valid_during, lower(recorded_during) AS rec_lo, changed_by, action_id "
            "FROM {t} WHERE {kw} AND scenario_id = %s AND "
            + ("recorded_during @> %s::timestamptz" if first
               else "(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity')") +
            " AND (valid_during && %s::tstzrange OR valid_during -|- %s::tstzrange)"
        ).format(
            pc=sql.SQL(", ").join(
                sql.SQL("encode(ST_AsEWKB({c}), 'hex') AS {c}").format(c=_tbl(c)) if c in geo
                else _tbl(c) for c in pcols) if pcols else sql.SQL("1 AS _dummy"),
            t=_tbl(table), kw=keyw,
        )
        rng = Range(vf, vt, "[)")
        with conn.cursor(row_factory=dict_row) as cur:
            if first:
                cur.execute(sel, [*keyvals, 0, _scn_ctx(conn, scenario_id)[0], rng, rng])
            else:
                cur.execute(sel, [*keyvals, scenario_id, rng, rng])
            existing = cur.fetchall()

        # originals keyed by (lo, hi, props)
        originals: dict[tuple, dict] = {}
        pieces: list[list] = []  # [lo, hi, props, changed_by, action_id, is_new]
        for r in existing:
            lo, hi = r["valid_during"].lower, _open(r["valid_during"].upper)
            p = tuple(r[c] for c in pcols)
            originals[(lo, hi, p)] = r
            if _lt(lo, vf):
                pieces.append([lo, _min(hi, vf), p, r["changed_by"], r["action_id"], False])
            if _lt(vt, hi) or (vt is not None and hi is None):
                pieces.append([_max(lo, vt) if vt is not None else lo, hi, p,
                               r["changed_by"], r["action_id"], False])
        if vt is not None and False:  # pragma: no cover
            pass
        if newp is not None:
            pieces.append([vf, vt, newp, changed_by, action_id, True])
        pieces.sort(key=lambda x: (x[0] is not None, x[0] or datetime.min.replace(tzinfo=UTC)))
        merged: list[list] = []
        for pc in pieces:
            if merged and merged[-1][1] is not None and merged[-1][1] == pc[0] and merged[-1][2] == pc[2]:
                prev = merged[-1]
                prev[1] = pc[1]
                if pc[5]:
                    prev[3], prev[4] = pc[3], pc[4]
                prev[5] = prev[5] or pc[5]
            else:
                merged.append(list(pc))
        new_keys = {(m[0], m[1], m[2]): m for m in merged}
        to_close = [k for k in originals if k not in new_keys]
        to_insert = [m for k, m in new_keys.items() if k not in originals]
        if not to_close and not to_insert:
            return False  # (a no-op on a first touch writes nothing: no clone, no override row)

        if first:  # the replaced versions are main's rows: they are simply not cloned
            _clone_from_main(conn, table, kind, type_name, keycols, keyvals, scenario_id, now,
                             [lo for lo, _hi, _p in to_close])
            to_close = []
        for lo, hi, p in to_close:
            r = originals[(lo, hi, p)]
            vr = lo  # lower bound is unique among current rows of a key
            if r["rec_lo"] >= now:  # created within this same instant: drop instead
                conn.execute(
                    sql.SQL("DELETE FROM {t} WHERE {kw} AND scenario_id = %s AND lower(valid_during) = %s "
                            "AND (upper_inf(recorded_during) OR upper(recorded_during) = 'infinity')").format(t=_tbl(table), kw=keyw),
                    [*keyvals, scenario_id, vr],
                )
            else:
                conn.execute(
                    sql.SQL("UPDATE {t} SET recorded_during = tstzrange(lower(recorded_during), %s) "
                            "WHERE {kw} AND scenario_id = %s AND lower(valid_during) = %s "
                            "AND (upper_inf(recorded_during) OR upper(recorded_during) = 'infinity')").format(t=_tbl(table), kw=keyw),
                    [now, *keyvals, scenario_id, vr],
                )
        cols = [*keycols, *pcols, *COMMON_COLS]
        ins = sql.SQL("INSERT INTO {t} ({c}) VALUES ({v})").format(
            t=_tbl(table), c=sql.SQL(", ").join(_tbl(c) for c in cols),
            v=sql.SQL(", ").join(
                sql.SQL("%s::geometry") if c in geo else sql.Placeholder() for c in cols),
        )
        for lo, hi, p, cb, aid, _new in to_insert:
            conn.execute(ins, [*keyvals, *p, Range(lo, hi, "[)"), Range(now, None, "[)"),
                               cb, aid, scenario_id])
        if scenario_id:  # scenario writes never reach the graph outbox
            _after_scenario_write(conn, table, kind, type_name, keycols, keyvals, scenario_id)
            return True
        op = "upsert" if newp is not None else "delete"
        _outbox(conn, kind, type_name, op, **outbox_extra)
        return True


def _outbox(conn, kind, type_, op, id=None, src=None, dst=None) -> None:
    conn.execute(
        "INSERT INTO graph_outbox (kind, type, id, src, dst, op) VALUES (%s,%s,%s,%s,%s,%s)",
        [kind, type_, id, src, dst, op],
    )


# ------------------------------------------------------------ public writes
def write_object(conn, type, id: UUID, props: dict, valid_from: datetime, changed_by: str, *,
                 valid_to: datetime | None = None, action_id: UUID | None = None,
                 scenario_id: int = 0) -> bool:
    """Assert `props` (full property set; missing = NULL) true over [valid_from, valid_to).

    Handles past-dated writes and corrections (history is preserved through
    recorded_during). Returns False (no-op, no outbox row) if nothing changed.
    """
    return _apply(conn, obj_table(type), ("id",), (id,), dict(props), valid_from, valid_to,
                  changed_by, action_id, scenario_id, "obj", type, {"id": id})


def delete_object(conn, type, id: UUID, valid_from: datetime, changed_by: str, *,
                  valid_to: datetime | None = None, action_id: UUID | None = None,
                  scenario_id: int = 0) -> bool:
    """The object ceases to exist from valid_from (until valid_to, default forever)."""
    return _apply(conn, obj_table(type), ("id",), (id,), None, valid_from, valid_to,
                  changed_by, action_id, scenario_id, "obj", type, {"id": id})


def _check_endpoint(conn, type_, id, at, scenario_id) -> None:
    if type_ is None:
        return
    row = get_object(conn, type_, id, as_of_valid=at, scenario_id=scenario_id)
    if row is None:
        raise ValueError(f"{type_} {id} does not exist at {at}")


def write_link(conn, link: str, src: UUID, dst: UUID, valid_from: datetime, changed_by: str, *,
               valid_to: datetime | None = None, action_id: UUID | None = None,
               scenario_id: int = 0, src_type: str | None = None,
               dst_type: str | None = None, props: dict | None = None) -> bool:
    """Assert src -[link]-> dst over [valid_from, valid_to). Endpoint types, when given, are checked."""
    _check_endpoint(conn, src_type, src, _utc(valid_from), scenario_id)
    _check_endpoint(conn, dst_type, dst, _utc(valid_from), scenario_id)
    return _apply(conn, link_table(link), ("src", "dst"), (src, dst), dict(props or {}),
                  valid_from, valid_to, changed_by, action_id, scenario_id, "link", link,
                  {"src": src, "dst": dst})


def end_link(conn, link: str, src: UUID, dst: UUID, valid_from: datetime, changed_by: str, *,
             valid_to: datetime | None = None, action_id: UUID | None = None,
             scenario_id: int = 0) -> bool:
    return _apply(conn, link_table(link), ("src", "dst"), (src, dst), None, valid_from,
                  valid_to, changed_by, action_id, scenario_id, "link", link,
                  {"src": src, "dst": dst})


# ------------------------------------------------------------------ reads
def _asof_clause(as_of_valid, as_of_recorded):
    """Return (sql, params). Default valid = now; default recorded = current knowledge."""
    v = _utc(as_of_valid) if as_of_valid is not None else None
    r = _utc(as_of_recorded) if as_of_recorded is not None else None
    parts, params = [], []
    parts.append("valid_during @> %s::timestamptz" if v is not None else "valid_during @> now()")
    if v is not None:
        params.append(v)
    if r is not None:
        parts.append("recorded_during @> %s::timestamptz")
        params.append(r)
    else:
        parts.append("(upper_inf(recorded_during) OR upper(recorded_during) = 'infinity')")
    return " AND ".join(parts), params


def _fetch(conn, table, where: str, params: list, order: str = "", masked: bool = True) -> list[dict]:
    q = sql.SQL("SELECT {s} FROM {t} WHERE {w} {o}").format(
        s=_select_list(conn, table), t=_tbl(read_source(conn, table, masked)),
        w=sql.SQL(where), o=sql.SQL(order))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, params)
        return cur.fetchall()


def get_object(conn, type, id: UUID, as_of_valid: datetime | None = None,
               as_of_recorded: datetime | None = None, scenario_id: int = 0,
               masked: bool = True) -> dict | None:
    """The object as true at `as_of_valid`, as believed at `as_of_recorded`."""
    if scenario_id:
        w, sp = _scn_where(conn, "obj", type, scenario_id, as_of_valid, as_of_recorded)
        rows = _fetch(conn, obj_table(type), f"id = %s AND {w}", [id, *sp], masked=masked)
        return rows[0] if rows else None
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    rows = _fetch(conn, obj_table(type), f"id = %s AND scenario_id = %s AND {c}",
                  [id, scenario_id, *p], masked=masked)
    return rows[0] if rows else None


def list_objects(conn, type, as_of_valid=None, as_of_recorded=None, scenario_id: int = 0,
                 where: str | None = None, params: Sequence[Any] = (), *,
                 limit: int | None = None, offset: int = 0, order_by: str | None = None,
                 desc: bool = False, masked: bool = True) -> list[dict]:
    """All objects of a type as of the given instants. `where` is an optional trusted SQL
    fragment (use %s placeholders with `params`). `order_by` is a column name (default id);
    limit/offset are pushed down into SQL."""
    if scenario_id:
        sw, sp = _scn_where(conn, "obj", type, scenario_id, as_of_valid, as_of_recorded)
        w, p0 = sw + (f" AND ({where})" if where else ""), sp
    else:
        c, p = _asof_clause(as_of_valid, as_of_recorded)
        w, p0 = f"scenario_id = %s AND {c}" + (f" AND ({where})" if where else ""), [scenario_id, *p]
    table = obj_table(type)
    ob = "id"
    if order_by:
        if order_by not in _table_cols(conn, table)[0]:
            raise ValueError(f"unknown order_by column {order_by!r}")
        ob = f'"{order_by}"'
    order = f"ORDER BY {ob}{' DESC' if desc else ''}" + ("" if ob == "id" else ", id")
    args = [*p0, *params]
    if limit is not None:
        order += " LIMIT %s"
        args.append(int(limit))
    if offset:
        order += " OFFSET %s"
        args.append(int(offset))
    return _fetch(conn, table, w, args, order, masked)


def list_objects_bbox(conn, type, bbox: Sequence[float], as_of_valid=None, as_of_recorded=None,
                      geom_col: str | None = None, scenario_id: int = 0,
                      masked: bool = True) -> list[dict]:
    """Objects whose geometry intersects bbox=(west, south, east, north) (EPSG:4326)."""
    geo = geometry_columns(conn, type)
    col = geom_col or (geo[0] if geo else None)
    if col is None or col not in geo:
        raise ValueError(f"{type} has no geometry column {geom_col!r}")
    w, s_, e, n = (float(x) for x in bbox)
    return list_objects(conn, type, as_of_valid, as_of_recorded, scenario_id,
                        where=f'"{col}" && ST_MakeEnvelope(%s,%s,%s,%s,4326)', params=[w, s_, e, n],
                        masked=masked)


def history(conn, type, id: UUID, scenario_id: int = 0, masked: bool = True) -> list[dict]:
    """Every stored version, ordered by recorded time then valid time (oldest first).
    Adds valid_from/valid_to/recorded_from/recorded_to (None = open)."""
    if scenario_id:
        return _scn_history(conn, "obj", type, ("id",), (id,), scenario_id, masked)
    rows = _fetch(conn, obj_table(type), "id = %s AND scenario_id = %s", [id, scenario_id],
                  "ORDER BY lower(recorded_during), lower(valid_during)", masked=masked)
    return [_decorate(r) for r in rows]


def link_history(conn, link: str, src: UUID, dst: UUID, scenario_id: int = 0,
                 masked: bool = True) -> list[dict]:
    if scenario_id:
        return _scn_history(conn, "link", link, ("src", "dst"), (src, dst), scenario_id, masked)
    rows = _fetch(conn, link_table(link), "src = %s AND dst = %s AND scenario_id = %s",
                  [src, dst, scenario_id], "ORDER BY lower(recorded_during), lower(valid_during)")
    return [_decorate(r) for r in rows]


def _scn_history(conn, kind, name, keycols, keyvals, sid, masked) -> list[dict]:
    """Scenario-effective history: for a key the scenario has touched, the scenario's own versions; for an
    untouched key, main's history up to the fork (recorded ranges that extend past `base_recorded` are shown
    as still open: the scenario never saw the later correction)."""
    table = obj_table(name) if kind == "obj" else link_table(name)
    base, _ = _scn_ctx(conn, sid)
    kb = keyvals[1] if len(keyvals) > 1 else ZERO_UUID
    keyw = " AND ".join(f"{k} = %s" for k in keycols)
    touched = conn.execute("SELECT 1 FROM scenario_override WHERE scenario_id = %s AND kind = %s AND type = %s "
                           "AND key_a = %s AND key_b = %s", [sid, kind, name, keyvals[0], kb]).fetchone()
    order = "ORDER BY lower(recorded_during), lower(valid_during)"
    if touched:
        rows = _fetch(conn, table, f"{keyw} AND scenario_id = %s", [*keyvals, sid], order, masked=masked)
        return [_decorate(r) for r in rows]
    rows = _fetch(conn, table, f"{keyw} AND scenario_id = 0 AND lower(recorded_during) <= %s::timestamptz",
                  [*keyvals, base], order, masked=masked)
    out = []
    for r in rows:
        ru = r["recorded_during"].upper
        if ru is not None and ru > base:
            r = {**r, "recorded_during": Range(r["recorded_during"].lower, None, "[)")}
        out.append(_decorate(r))
    return out


def _decorate(r: dict) -> dict:
    r = dict(r)
    r["valid_from"], r["valid_to"] = r["valid_during"].lower, _open(r["valid_during"].upper)
    r["recorded_from"], r["recorded_to"] = r["recorded_during"].lower, _open(r["recorded_during"].upper)
    return r


def neighbours(conn, link: str, id: UUID, direction: str = "out", as_of_valid=None,
               as_of_recorded=None, scenario_id: int = 0,
               masked: bool = True) -> list[UUID]:
    """Ids linked from (direction='out': id is src) or to (direction='in') the given id."""
    if direction not in ("out", "in"):
        raise ValueError("direction must be 'out' or 'in'")
    mine, other = ("src", "dst") if direction == "out" else ("dst", "src")
    if scenario_id:
        w, sp = _scn_where(conn, "link", link, scenario_id, as_of_valid, as_of_recorded)
        rows = _fetch(conn, link_table(link), f"{mine} = %s AND {w}", [id, *sp], f"ORDER BY {other}",
                      masked=masked)
        return [r[other] for r in rows]
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    rows = _fetch(conn, link_table(link), f"{mine} = %s AND scenario_id = %s AND {c}",
                  [id, scenario_id, *p], f"ORDER BY {other}", masked=masked)
    return [r[other] for r in rows]


def list_links(conn, link: str, as_of_valid=None, as_of_recorded=None,
               scenario_id: int = 0, masked: bool = True) -> list[dict]:
    if scenario_id:
        w, sp = _scn_where(conn, "link", link, scenario_id, as_of_valid, as_of_recorded)
        return _fetch(conn, link_table(link), w, sp, "ORDER BY src, dst", masked=masked)
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    return _fetch(conn, link_table(link), f"scenario_id = %s AND {c}", [scenario_id, *p],
                  "ORDER BY src, dst", masked=masked)


# ------------------------------------------------------------------- bulk
def _rng_text(lo: datetime) -> str:
    return f"[{_utc(lo).isoformat()},)"


def bulk_load_objects(conn, type, rows: Iterable[tuple[UUID, dict]], valid_from: datetime,
                      changed_by: str, *, scenario_id: int = 0, outbox: bool = True,
                      action_id: UUID | None = None) -> int:
    """COPY brand-new entities (no existing rows for these ids!). Bypasses splitting and
    locking; the exclusion constraint still rejects any accidental overlap."""
    if scenario_id:
        raise ValueError("bulk_load_objects is not supported inside a scenario (use write_object)")
    table = obj_table(type)
    pcols = _prop_cols(conn, table, ("id",))
    cols = ["id", *pcols, *COMMON_COLS]
    geo = set(_table_cols(conn, table)[1])
    now = _now(conn)
    vt, rt = _rng_text(valid_from), _rng_text(now)
    n = 0
    with conn.transaction():
        stmt = sql.SQL("COPY {t} ({c}) FROM STDIN").format(
            t=_tbl(table), c=sql.SQL(", ").join(_tbl(c) for c in cols))
        with conn.cursor() as cur, cur.copy(stmt) as cp:
            for id_, props in rows:
                cp.write_row([id_, *((_geom_hex(conn, props.get(c)) if c in geo else props.get(c))
                                    for c in pcols), vt, rt, changed_by,
                              action_id, scenario_id])
                n += 1
        if outbox:
            ids = [i for i, _ in _reiter(conn, table, now, scenario_id)]
            with conn.cursor() as cur, cur.copy(
                    "COPY graph_outbox (kind, type, id, op) FROM STDIN") as cp:
                for id_ in ids:
                    cp.write_row(["obj", type, id_, "upsert"])
    return n


def _reiter(conn, table, now, scenario_id):
    # ids loaded in this call = rows recorded exactly at `now`
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT id FROM {t} WHERE lower(recorded_during) = %s "
                            "AND scenario_id = %s").format(t=_tbl(table)), [now, scenario_id])
        for (i,) in cur.fetchall():
            yield i, None


def bulk_load_links(conn, link: str, rows: Iterable[tuple[UUID, UUID]], valid_from: datetime,
                    changed_by: str, *, scenario_id: int = 0, outbox: bool = True,
                    action_id: UUID | None = None) -> int:
    if scenario_id:
        raise ValueError("bulk_load_links is not supported inside a scenario (use write_link)")
    table = link_table(link)
    now = _now(conn)
    vt, rt = _rng_text(valid_from), _rng_text(now)
    cols = ["src", "dst", *COMMON_COLS]
    n = 0
    with conn.transaction():
        stmt = sql.SQL("COPY {t} ({c}) FROM STDIN").format(
            t=_tbl(table), c=sql.SQL(", ").join(_tbl(c) for c in cols))
        with conn.cursor() as cur, cur.copy(stmt) as cp:
            ob = []
            for s, d in rows:
                cp.write_row([s, d, vt, rt, changed_by, action_id, scenario_id])
                ob.append(("link", link, s, d, "upsert"))
                n += 1
        if outbox:
            with conn.cursor() as cur, cur.copy(
                    "COPY graph_outbox (kind, type, src, dst, op) FROM STDIN") as cp:
                for k, t, s, d, op in ob:
                    cp.write_row([k, t, s, d, op])
    return n
