"""SQLite reader: read-only URI, query_only, authorizer, time limit; structure via pragma table functions."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from ..names import clean_text
from .base import (ColumnInfo, DeclaredFK, Source, SourceError, SourceLimits, SourceTable, cell_str, dedupe_names,
                   sha256_file, unique_table_names)

MAGIC = b"SQLite format 3\x00"
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def q(ident: str) -> str:
    """Quote an identifier for SQLite."""
    return '"' + str(ident).replace('"', '""') + '"'


_PRAGMAS = {"table_info", "table_xinfo", "foreign_key_list"}


def _auth(action, a1, a2, db, trig):
    if action in _ALLOWED:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and str(a1).lower() in _PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class SqliteTable(SourceTable):
    def __init__(self, src: "SqliteSource", raw_name: str, name: str, cols: list[tuple[str, str, int]]) -> None:
        self._src, self._raw = src, raw_name
        self.name = name
        self._raw_cols = [c[0] for c in cols]
        self.columns = [ColumnInfo(n, c[1] or None, not c[2]) for n, c in zip(dedupe_names(self._raw_cols), cols)]

    def rows(self, limit: int | None = None) -> Iterator[tuple]:
        cap = min(limit, self._src.limits.max_rows) if limit is not None else self._src.limits.max_rows
        sql = f"SELECT {', '.join(q(c) for c in self._raw_cols)} FROM {q(self._raw)} LIMIT {int(cap)}"
        cur = self._src._conn.cursor()
        try:
            self._src._arm()
            for row in cur.execute(sql):
                yield tuple(cell_str(v) for v in row)
        except sqlite3.Error as e:
            raise SourceError(f"sqlite read failed ({clean_text(e, 80)})") from None
        finally:
            cur.close()

    def row_count(self) -> int | None:
        try:
            self._src._arm()
            return self._src._conn.execute(f"SELECT count(*) FROM {q(self._raw)}").fetchone()[0]
        except sqlite3.Error:
            return None


class SqliteSource(Source):
    kind = origin = "sqlite"

    def __init__(self, path: str | os.PathLike, *, name: str | None = None, limits: SourceLimits | None = None) -> None:
        super().__init__()
        self.limits = limits or SourceLimits()
        p = Path(path)
        if not p.is_file():
            raise SourceError("file not found")
        with open(p, "rb") as f:
            if f.read(16) != MAGIC:
                raise SourceError("not a SQLite database file")
        self.sha256, self.bytes = sha256_file(p, self.limits.max_bytes * 4)
        self.name = clean_text(name or p.name, 64) or "database"
        uri = "file:" + quote(str(p.resolve()), safe="/") + "?mode=ro"
        try:
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("PRAGMA query_only = ON")
            try:
                self._conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 50_000_000)
            except (AttributeError, sqlite3.Error):
                pass
            self._conn.set_authorizer(_auth)
            self._conn.set_progress_handler(self._progress, 100_000)
            self._deadline = 0.0
            self._arm()
            raw_tables = [r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        except sqlite3.Error as e:
            raise SourceError(f"cannot open SQLite file ({clean_text(e, 80)})") from None
        if len(raw_tables) > self.limits.max_tables:
            self.warnings.append(f"only the first {self.limits.max_tables} tables are read")
            raw_tables = raw_tables[: self.limits.max_tables]
        names = unique_table_names([clean_text(t, 64) or "table" for t in raw_tables])
        self._tables: list[SourceTable] = []
        self._by_raw: dict[str, str] = {}
        self._pks: dict[str, list[str]] = {}
        self._fks: list[DeclaredFK] = []
        try:
            for raw, nm in zip(raw_tables, names):
                info = self._conn.execute(
                    'SELECT name, type, "notnull", pk FROM pragma_table_info(?) ORDER BY cid', (raw,)).fetchall()
                if not info:
                    continue
                self._tables.append(SqliteTable(self, raw, nm, [(r[0], r[1], r[2]) for r in info]))
                self._by_raw[raw] = nm
                pk = [r for r in info if r[3]]
                if pk:
                    pk.sort(key=lambda r: r[3])
                    cleaned = dict(zip([r[0] for r in info], self._tables[-1].columns))
                    self._pks[nm] = [cleaned[r[0]].name for r in pk]
            for t in self._tables:
                raw = t._raw
                rows = self._conn.execute(
                    'SELECT id, seq, "table", "from", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq', (raw,)).fetchall()
                groups: dict[int, list] = {}
                for r in rows:
                    groups.setdefault(r[0], []).append(r)
                cmap = dict(zip(t._raw_cols, [c.name for c in t.columns]))
                for g in groups.values():
                    target_raw = g[0][2]
                    if target_raw not in self._by_raw:
                        continue
                    target = next(x for x in self._tables if x._raw == target_raw)
                    tmap = dict(zip(target._raw_cols, [c.name for c in target.columns]))
                    to_cols = []
                    for r in g:
                        if r[4] is None:  # implicit reference to the parent's primary key
                            to_cols = list(self._pks.get(target.name, []))
                            break
                        to_cols.append(tmap.get(r[4], r[4]))
                    from_cols = [cmap.get(r[3], r[3]) for r in g]
                    if to_cols and len(to_cols) == len(from_cols):
                        self._fks.append(DeclaredFK(t.name, tuple(from_cols), target.name, tuple(to_cols)))
        except sqlite3.Error as e:
            raise SourceError(f"cannot read SQLite schema ({clean_text(e, 80)})") from None

    def _arm(self) -> None:
        self._deadline = time.monotonic() + self.limits.statement_timeout_s * 4

    def _progress(self) -> int:
        return 1 if time.monotonic() > self._deadline else 0

    def tables(self) -> list[SourceTable]:
        return self._tables

    def declared_keys(self) -> dict[str, list[str]]:
        return dict(self._pks)

    def declared_fks(self) -> list[DeclaredFK]:
        return list(self._fks)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
