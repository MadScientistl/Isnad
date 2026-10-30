"""PostgreSQL reader: read-only transaction, statement timeout, catalog structure, safe identifiers.
The DSN comes only from the caller (server side config) and is never stored on the Source."""
from __future__ import annotations

import hashlib
import json
from typing import Iterator

import psycopg
from psycopg import sql

from ..names import clean_text
from .base import (ColumnInfo, DeclaredFK, Source, SourceError, SourceLimits, SourceTable, cell_str, dedupe_names,
                   unique_table_names)

_FETCH = 5000


class PgTable(SourceTable):
    def __init__(self, src: "PgSource", raw: str, name: str, cols: list[tuple[str, str, str, bool]]) -> None:
        self._src, self._raw = src, raw
        self.name = name
        self._raw_cols = cols
        names = dedupe_names([c[0] for c in cols])
        self.columns = [ColumnInfo(n, c[1], c[3]) for n, c in zip(names, cols)]

    def _select(self) -> sql.Composed:
        parts = []
        for cname, _dt, udt, _n in self._raw_cols:
            ident = sql.Identifier(cname)
            if udt in ("geometry", "geography") and self._src._postgis:
                parts.append(sql.SQL("ST_AsGeoJSON({}::geometry)::text").format(ident))
            elif udt in ("geometry", "geography"):
                parts.append(sql.SQL("{}::text").format(ident))
            else:
                parts.append(ident)
        return sql.SQL("SELECT {} FROM {}.{}").format(sql.SQL(", ").join(parts), sql.Identifier(self._src.schema),
                                                       sql.Identifier(self._raw))

    def rows(self, limit: int | None = None) -> Iterator[tuple]:
        cap = min(limit, self._src.limits.max_rows) if limit is not None else self._src.limits.max_rows
        q = self._select() + sql.SQL(" LIMIT {}").format(sql.Literal(int(cap)))
        try:
            with self._src._conn.cursor(name=f"c{abs(hash((self._raw, id(self)))) % 10**9}") as cur:
                cur.itersize = _FETCH
                cur.execute(q)
                for row in cur:
                    yield tuple(cell_str(v) for v in row)
        except psycopg.Error as e:
            self._src._recover()
            raise SourceError(f"postgres read failed ({clean_text(e, 80)})") from None

    def row_count(self) -> int | None:
        try:
            with self._src._conn.transaction():
                with self._src._conn.cursor() as cur:
                    cur.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(self._src.schema), sql.Identifier(self._raw)))
                    return int(cur.fetchone()[0])
        except psycopg.Error:
            self._src._recover()
            return None


class PgSource(Source):
    kind = origin = "postgres"

    def __init__(self, dsn: str, *, name: str, schema: str = "public", limits: SourceLimits | None = None) -> None:
        super().__init__()
        self.limits = limits or SourceLimits()
        self.schema = schema
        self.name = clean_text(name, 64) or "postgres"
        self.bytes = None
        try:
            self._conn = psycopg.connect(dsn, connect_timeout=10, autocommit=False)
            self._conn.read_only = True
            with self._conn.cursor() as cur:
                cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(
                    sql.Literal(f"{int(self.limits.statement_timeout_s) * 1000}")))
                cur.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis')")
                self._postgis = bool(cur.fetchone()[0])
                cur.execute("""SELECT table_name FROM information_schema.tables
                               WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name""", (schema,))
                raw_tables = [r[0] for r in cur.fetchall()]
                if len(raw_tables) > self.limits.max_tables:
                    self.warnings.append(f"only the first {self.limits.max_tables} tables are read")
                    raw_tables = raw_tables[: self.limits.max_tables]
                cur.execute("""SELECT table_name, column_name, data_type, udt_name, is_nullable
                               FROM information_schema.columns WHERE table_schema = %s
                               ORDER BY table_name, ordinal_position""", (schema,))
                cols: dict[str, list] = {}
                for t, c, dt, udt, nul in cur.fetchall():
                    cols.setdefault(t, []).append((c, dt, udt, nul == "YES"))
                cur.execute("""
                  SELECT c.contype, cl.relname, fcl.relname, fn.nspname,
                    ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
                          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum ORDER BY k.ord),
                    ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(attnum, ord)
                          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum ORDER BY k.ord)
                  FROM pg_constraint c
                  JOIN pg_class cl ON cl.oid = c.conrelid
                  JOIN pg_namespace n ON n.oid = cl.relnamespace
                  LEFT JOIN pg_class fcl ON fcl.oid = c.confrelid
                  LEFT JOIN pg_namespace fn ON fn.oid = fcl.relnamespace
                  WHERE c.contype IN ('p', 'f') AND n.nspname = %s
                  ORDER BY c.conname""", (schema,))
                cons = cur.fetchall()
        except psycopg.Error as e:
            try:
                self._conn.close()
            except Exception:
                pass
            raise SourceError(f"cannot read the PostgreSQL source ({clean_text(e, 80)})") from None
        names = unique_table_names([clean_text(t, 64) or "table" for t in raw_tables])
        self._tables = [PgTable(self, r, n, cols[r]) for r, n in zip(raw_tables, names) if cols.get(r)]
        by_raw = {t._raw: t for t in self._tables}

        def cmap(t: PgTable) -> dict[str, str]:
            return {rc[0]: c.name for rc, c in zip(t._raw_cols, t.columns)}

        self._pks: dict[str, list[str]] = {}
        self._fks: list[DeclaredFK] = []
        for ctype, rel, frel, fns, cols_, fcols in cons:
            t = by_raw.get(rel)
            if t is None:
                continue
            if ctype == "p":
                self._pks[t.name] = [cmap(t).get(c, c) for c in cols_]
            elif ctype == "f" and fns == schema and frel in by_raw:
                ft = by_raw[frel]
                self._fks.append(DeclaredFK(t.name, tuple(cmap(t).get(c, c) for c in cols_),
                                            ft.name, tuple(cmap(ft).get(c, c) for c in fcols)))
        # fingerprint of the structure: the "sha256" of a live database (content is not hashed)
        fp = {"schema": schema, "tables": [[t.name, [(c.name, c.declared_type) for c in t.columns]] for t in self._tables],
              "pks": self._pks, "fks": [[f.from_table, f.from_cols, f.to_table, f.to_cols] for f in self._fks]}
        self.sha256 = hashlib.sha256(json.dumps(fp, sort_keys=True, default=list).encode()).hexdigest()

    def _recover(self) -> None:
        try:
            self._conn.rollback()
            with self._conn.cursor() as cur:
                cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(
                    sql.Literal(f"{int(self.limits.statement_timeout_s) * 1000}")))
        except psycopg.Error:
            pass

    def tables(self) -> list[SourceTable]:
        return self._tables

    def declared_keys(self) -> dict[str, list[str]]:
        return dict(self._pks)

    def declared_fks(self) -> list[DeclaredFK]:
        return list(self._fks)

    def close(self) -> None:
        try:
            self._conn.rollback()
            self._conn.close()
        except psycopg.Error:
            pass
