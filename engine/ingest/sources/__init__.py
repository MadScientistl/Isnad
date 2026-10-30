"""Data-source readers for the ingest profiler. See ``open_source``."""
from __future__ import annotations

import os
from pathlib import Path

from .base import (ColumnInfo, DeclaredFK, Source, SourceError, SourceLimits, SourceTable)
from .registry import NamedSource, load_named_sources

__all__ = ["open_source", "Source", "SourceTable", "ColumnInfo", "DeclaredFK", "SourceError", "SourceLimits",
           "NamedSource", "load_named_sources"]


def open_source(kind: str, target: str | os.PathLike | bytes | None = None, *, name: str | None = None,
                dsn: str | None = None, schema: str = "public", limits: SourceLimits | None = None) -> Source:
    """Open a source.

    kind: "csv" (file path or bytes + name), "dir", "zip" (path or bytes), "sqlite" (path),
    "postgres" (``dsn`` from server-side config, optional ``schema``), or "auto" (decide from a path).
    The result is a context manager; use ``source.tables()`` / ``declared_fks()``.
    """
    limits = limits or SourceLimits.from_env()
    if kind == "auto":
        p = Path(os.fspath(target))
        if p.is_dir():
            kind = "dir"
        else:
            with open(p, "rb") as f:
                head = f.read(16)
            kind = ("sqlite" if head.startswith(b"SQLite format 3\x00") else "zip" if head[:2] == b"PK" else "csv")
    if kind in ("csv", "tsv"):
        from .csv_source import CsvFileSource
        if isinstance(target, (bytes, bytearray)):
            return CsvFileSource(data=bytes(target), name=name, limits=limits)
        return CsvFileSource(os.fspath(target), name=name, limits=limits)
    if kind == "dir":
        from .csv_source import CsvDirSource
        return CsvDirSource(os.fspath(target), name=name, limits=limits)
    if kind == "zip":
        from .zip_source import ZipSource
        if isinstance(target, (bytes, bytearray)):
            return ZipSource(data=bytes(target), name=name, limits=limits)
        return ZipSource(os.fspath(target), name=name, limits=limits)
    if kind == "sqlite":
        from .sqlite_source import SqliteSource
        return SqliteSource(os.fspath(target), name=name, limits=limits)
    if kind in ("postgres", "postgresql"):
        from .pg_source import PgSource
        if not dsn:
            raise SourceError("a DSN is required")
        return PgSource(dsn, name=name or "postgres", schema=schema, limits=limits)
    raise SourceError(f"unknown source kind: {str(kind)[:20]!r}")
