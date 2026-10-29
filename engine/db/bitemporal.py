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

import re
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.range import Range

COMMON_COLS = ("valid_during", "recorded_during", "changed_by", "action_id", "scenario_id")

_cols_cache: dict[tuple, tuple[str, ...]] = {}


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


def _prop_cols(conn: psycopg.Connection, table: str, keycols: Sequence[str]) -> tuple[str, ...]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_schemas(false)::text")
        sp = cur.fetchone()[0]
        key = (sp, table)
        if key not in _cols_cache:
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_name = %s AND table_schema = ANY(current_schemas(false))
                   ORDER BY ordinal_position""",
                [table],
            )
            cols = [r[0] for r in cur.fetchall()]
            if not cols:
                raise LookupError(f"table {table} not found on search_path")
            skip = set(COMMON_COLS) | set(keycols)
            _cols_cache[key] = tuple(c for c in cols if c not in skip)
    return _cols_cache[key]


_geo_cache: dict[tuple, tuple[tuple[str, ...], tuple[str, ...]]] = {}


def _table_cols(conn: psycopg.Connection, table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(all columns in order, geometry columns) of `table` on the current search_path."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_schemas(false)::text")
        key = (cur.fetchone()[0], table)
        if key not in _geo_cache:
            cur.execute(
                """SELECT column_name, udt_name FROM information_schema.columns
                   WHERE table_name = %s AND table_schema = ANY(current_schemas(false))
                   ORDER BY ordinal_position""",
                [table],
            )
            rows = cur.fetchall()
            if not rows:
                raise LookupError(f"table {table} not found on search_path")
            _geo_cache[key] = (tuple(r[0] for r in rows),
                               tuple(r[0] for r in rows if r[1] == "geometry"))
    return _geo_cache[key]


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

    with conn.transaction():
        _lock(conn, table, keyvals, scenario_id)
        now = _now(conn)
        keyw = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(_tbl(k)) for k in keycols)
        sel = sql.SQL(
            "SELECT {pc}, valid_during, lower(recorded_during) AS rec_lo, changed_by, action_id "
            "FROM {t} WHERE {kw} AND scenario_id = %s AND (upper_inf(recorded_during) OR upper(recorded_during) = 'infinity') "
            "AND (valid_during && %s::tstzrange OR valid_during -|- %s::tstzrange)"
        ).format(
            pc=sql.SQL(", ").join(
                sql.SQL("encode(ST_AsEWKB({c}), 'hex') AS {c}").format(c=_tbl(c)) if c in geo
                else _tbl(c) for c in pcols) if pcols else sql.SQL("1 AS _dummy"),
            t=_tbl(table), kw=keyw,
        )
        rng = Range(vf, vt, "[)")
        with conn.cursor(row_factory=dict_row) as cur:
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
            return False

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


def _fetch(conn, table, where: str, params: list, order: str = "") -> list[dict]:
    q = sql.SQL("SELECT {s} FROM {t} WHERE {w} {o}").format(
        s=_select_list(conn, table), t=_tbl(table), w=sql.SQL(where), o=sql.SQL(order))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q, params)
        return cur.fetchall()


def get_object(conn, type, id: UUID, as_of_valid: datetime | None = None,
               as_of_recorded: datetime | None = None, scenario_id: int = 0) -> dict | None:
    """The object as true at `as_of_valid`, as believed at `as_of_recorded`."""
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    rows = _fetch(conn, obj_table(type), f"id = %s AND scenario_id = %s AND {c}",
                  [id, scenario_id, *p])
    return rows[0] if rows else None


def list_objects(conn, type, as_of_valid=None, as_of_recorded=None, scenario_id: int = 0,
                 where: str | None = None, params: Sequence[Any] = (), *,
                 limit: int | None = None, offset: int = 0, order_by: str | None = None,
                 desc: bool = False) -> list[dict]:
    """All objects of a type as of the given instants. `where` is an optional trusted SQL
    fragment (use %s placeholders with `params`). `order_by` is a column name (default id);
    limit/offset are pushed down into SQL."""
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    w = f"scenario_id = %s AND {c}" + (f" AND ({where})" if where else "")
    table = obj_table(type)
    ob = "id"
    if order_by:
        if order_by not in _table_cols(conn, table)[0]:
            raise ValueError(f"unknown order_by column {order_by!r}")
        ob = f'"{order_by}"'
    order = f"ORDER BY {ob}{' DESC' if desc else ''}" + ("" if ob == "id" else ", id")
    args = [scenario_id, *p, *params]
    if limit is not None:
        order += " LIMIT %s"
        args.append(int(limit))
    if offset:
        order += " OFFSET %s"
        args.append(int(offset))
    return _fetch(conn, table, w, args, order)


def list_objects_bbox(conn, type, bbox: Sequence[float], as_of_valid=None, as_of_recorded=None,
                      geom_col: str | None = None, scenario_id: int = 0) -> list[dict]:
    """Objects whose geometry intersects bbox=(west, south, east, north) (EPSG:4326)."""
    geo = geometry_columns(conn, type)
    col = geom_col or (geo[0] if geo else None)
    if col is None or col not in geo:
        raise ValueError(f"{type} has no geometry column {geom_col!r}")
    w, s_, e, n = (float(x) for x in bbox)
    return list_objects(conn, type, as_of_valid, as_of_recorded, scenario_id,
                        where=f'"{col}" && ST_MakeEnvelope(%s,%s,%s,%s,4326)', params=[w, s_, e, n])


def history(conn, type, id: UUID, scenario_id: int = 0) -> list[dict]:
    """Every stored version, ordered by recorded time then valid time (oldest first).
    Adds valid_from/valid_to/recorded_from/recorded_to (None = open)."""
    rows = _fetch(conn, obj_table(type), "id = %s AND scenario_id = %s", [id, scenario_id],
                  "ORDER BY lower(recorded_during), lower(valid_during)")
    return [_decorate(r) for r in rows]


def link_history(conn, link: str, src: UUID, dst: UUID, scenario_id: int = 0) -> list[dict]:
    rows = _fetch(conn, link_table(link), "src = %s AND dst = %s AND scenario_id = %s",
                  [src, dst, scenario_id], "ORDER BY lower(recorded_during), lower(valid_during)")
    return [_decorate(r) for r in rows]


def _decorate(r: dict) -> dict:
    r = dict(r)
    r["valid_from"], r["valid_to"] = r["valid_during"].lower, _open(r["valid_during"].upper)
    r["recorded_from"], r["recorded_to"] = r["recorded_during"].lower, _open(r["recorded_during"].upper)
    return r


def neighbours(conn, link: str, id: UUID, direction: str = "out", as_of_valid=None,
               as_of_recorded=None, scenario_id: int = 0) -> list[UUID]:
    """Ids linked from (direction='out': id is src) or to (direction='in') the given id."""
    if direction not in ("out", "in"):
        raise ValueError("direction must be 'out' or 'in'")
    mine, other = ("src", "dst") if direction == "out" else ("dst", "src")
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    rows = _fetch(conn, link_table(link), f"{mine} = %s AND scenario_id = %s AND {c}",
                  [id, scenario_id, *p], f"ORDER BY {other}")
    return [r[other] for r in rows]


def list_links(conn, link: str, as_of_valid=None, as_of_recorded=None,
               scenario_id: int = 0) -> list[dict]:
    c, p = _asof_clause(as_of_valid, as_of_recorded)
    return _fetch(conn, link_table(link), f"scenario_id = %s AND {c}", [scenario_id, *p],
                  "ORDER BY src, dst")


# ------------------------------------------------------------------- bulk
def _rng_text(lo: datetime) -> str:
    return f"[{_utc(lo).isoformat()},)"


def bulk_load_objects(conn, type, rows: Iterable[tuple[UUID, dict]], valid_from: datetime,
                      changed_by: str, *, scenario_id: int = 0, outbox: bool = True,
                      action_id: UUID | None = None) -> int:
    """COPY brand-new entities (no existing rows for these ids!). Bypasses splitting and
    locking; the exclusion constraint still rejects any accidental overlap."""
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
