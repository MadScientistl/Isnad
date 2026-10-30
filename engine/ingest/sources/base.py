"""Common source API. Readers yield tables whose rows are tuples of ``str | None`` (never native values),
so the profiler is uniform. Nothing here executes data content; all readers are capped."""
from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from ..names import clean_text

MAX_CELL_CHARS = 20_000


class SourceError(Exception):
    """A source cannot be read safely (bad format, limits exceeded, unsafe archive). The message is safe to show."""


def _int_env(env: dict | None, name: str, default: int) -> int:
    env = os.environ if env is None else env
    try:
        v = int(env.get(name, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SourceLimits:
    max_bytes: int = 256 * 1024 * 1024          # per file / uncompressed member
    max_total_bytes: int = 512 * 1024 * 1024    # all zip members together (uncompressed)
    max_rows: int = 5_000_000                   # rows read per table
    max_columns: int = 1000                     # hard reader limit (the profiler applies its own, smaller, cap)
    max_zip_members: int = 100
    max_zip_ratio: int = 50
    max_tables: int = 500
    statement_timeout_s: int = 30

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SourceLimits":
        return cls(
            max_zip_members=_int_env(env, "ISNAD_INGEST_MAX_ZIP_MEMBERS", 100),
            max_zip_ratio=_int_env(env, "ISNAD_INGEST_MAX_ZIP_RATIO", 50),
        )


@dataclass
class ColumnInfo:
    name: str                       # cleaned, unique within the table
    declared_type: str | None = None
    nullable: bool | None = None


@dataclass(frozen=True)
class DeclaredFK:
    from_table: str
    from_cols: tuple[str, ...]
    to_table: str
    to_cols: tuple[str, ...]


class SourceTable:
    """One table. ``rows()`` streams tuples aligned with ``columns``; ``row_count()`` is exact or None."""

    name: str
    columns: list[ColumnInfo]

    def rows(self, limit: int | None = None) -> Iterator[tuple]:  # pragma: no cover - interface
        raise NotImplementedError

    def row_count(self) -> int | None:
        return None


class Source:
    kind: str = "csv"               # "csv" | "sqlite" | "postgres" (what Profile.source.kind reports)
    origin: str = "csv_file"        # finer: csv_file | dir | zip | sqlite | postgres
    name: str = ""
    sha256: str = ""
    bytes: int | None = None

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def tables(self) -> list[SourceTable]:  # pragma: no cover - interface
        raise NotImplementedError

    def declared_keys(self) -> dict[str, list[str]]:
        """table name -> primary key column names (declared)."""
        return {}

    def declared_fks(self) -> list[DeclaredFK]:
        return []

    def close(self) -> None:
        pass

    def __enter__(self) -> "Source":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def dedupe_names(raw: Sequence[object], *, maxlen: int = 64) -> list[str]:
    """Clean header names, replace empties with col_N, make them unique (a, a_2, a_3 ...)."""
    out: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(raw):
        name = clean_text(r, maxlen) or f"col_{i + 1}"
        base, n = name, 2
        while name.lower() in seen:
            name = f"{base[: maxlen - 4]}_{n}"
            n += 1
        seen.add(name.lower())
        out.append(name)
    return out


def unique_table_names(names: Sequence[str]) -> list[str]:
    return dedupe_names(names)


def cell_str(v: object) -> str | None:
    """Native database value -> str | None (bounded)."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, (int, float, decimal.Decimal, uuid.UUID)):
        s = str(v)
    elif isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        s = v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    elif isinstance(v, (bytes, bytearray, memoryview)):
        s = f"<binary {len(v)} bytes>"
    elif isinstance(v, (dict, list, tuple)):
        s = json.dumps(v, default=str, separators=(",", ":"))
    else:
        s = str(v)
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS]


def sha256_file(path: str | os.PathLike, max_bytes: int | None = None) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            n += len(chunk)
            if max_bytes is not None and n > max_bytes:
                raise SourceError(f"file exceeds the {max_bytes} byte limit")
            h.update(chunk)
    return h.hexdigest(), n
