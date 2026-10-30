"""CSV / TSV readers (single file, bytes, directory). Stdlib csv only; streaming; capped."""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import os
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from ..names import clean_text
from .base import (MAX_CELL_CHARS, ColumnInfo, Source, SourceError, SourceLimits, SourceTable, dedupe_names,
                   sha256_file, unique_table_names)

csv.field_size_limit(MAX_CELL_CHARS * 4)

SNIFF_BYTES = 256 * 1024
DELIMS = [",", ";", "\t", "|"]


class _Limited(io.RawIOBase):
    """Raw reader that fails once more than ``limit`` bytes have been read (real bytes, not headers)."""

    def __init__(self, inner: BinaryIO, limit: int) -> None:
        self._inner, self._limit, self._n = inner, limit, 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._inner.read(len(b))
        self._n += len(data)
        if self._n > self._limit:
            raise SourceError(f"data exceeds the {self._limit} byte limit")
        b[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            super().close()


def sniff_encoding(head: bytes) -> str:
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        dec.decode(head, final=False)  # a cut multi-byte char at the end is fine with final=False
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def sniff_delimiter(text: str, hint: str | None = None) -> str:
    lines = [ln for ln in text.splitlines()[:30] if ln.strip()]
    if not lines:
        return hint or ","
    best, best_score = hint or ",", (-1, -1)
    for d in DELIMS:
        try:
            rows = list(csv.reader(lines, delimiter=d))
        except csv.Error:
            continue
        counts = [len(r) for r in rows]
        mode = max(set(counts), key=lambda c: (counts.count(c), c))
        if mode < 2:
            continue
        score = (counts.count(mode), mode)
        if hint == d:
            score = (score[0], score[1] + 0.5)
        if score > best_score:
            best, best_score = d, score
    return best


class CsvTable(SourceTable):
    def __init__(self, name: str, opener: Callable[[], BinaryIO], limits: SourceLimits, *, hint_delim: str | None = None,
                 warnings: list[str] | None = None, size_hint: int | None = None) -> None:
        self.name = name
        self._open, self._limits = opener, limits
        self._warn = warnings if warnings is not None else []
        with self._open() as f:
            head = f.read(SNIFF_BYTES)
        if head[:1024].count(b"\x00") > 8 and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            raise SourceError(f"{clean_text(name, 40)}: looks like a binary file, not CSV")
        self.encoding = sniff_encoding(head)
        text = head.decode(self.encoding, errors="replace")
        self.delimiter = sniff_delimiter(text, hint_delim)
        self._ragged = 0
        with self._stream() as reader:
            header = None
            for rec in reader:
                if any(c.strip() for c in rec):
                    header = rec
                    break
        if header is None:
            raise SourceError(f"{clean_text(name, 40)}: empty file")
        if len(header) > limits.max_columns:
            raise SourceError(f"{clean_text(name, 40)}: too many columns ({len(header)} > {limits.max_columns})")
        names = dedupe_names(header)
        if names != [clean_text(h, 64) or f"col_{i + 1}" for i, h in enumerate(header)]:
            self._warn.append(f"{clean_text(name, 40)}: header names were cleaned or de-duplicated")
        self.columns = [ColumnInfo(n) for n in names]
        self.truncated_rows = False

    def _stream(self):
        outer = self

        class _Ctx:
            def __enter__(s):
                raw = io.BufferedReader(_Limited(outer._open(), outer._limits.max_bytes))
                s.txt = io.TextIOWrapper(raw, encoding=outer.encoding, errors="replace", newline="")
                return csv.reader(s.txt, delimiter=outer.delimiter)

            def __exit__(s, *a):
                s.txt.close()

        return _Ctx()

    def rows(self, limit: int | None = None) -> Iterator[tuple]:
        ncol = len(self.columns)
        cap = min(limit, self._limits.max_rows) if limit is not None else self._limits.max_rows
        n = 0
        first = True
        try:
            with self._stream() as reader:
                for rec in reader:
                    if first:
                        if not any(c.strip() for c in rec):
                            continue
                        first = False
                        continue  # header
                    if not rec or (len(rec) == 1 and not rec[0].strip() and ncol > 1):
                        continue  # blank line
                    ln = len(rec)
                    if ln != ncol:
                        self._ragged += 1
                        rec = rec[:ncol] if ln > ncol else rec + [None] * (ncol - ln)
                    if n >= cap:
                        if limit is None:
                            self.truncated_rows = True
                        return
                    n += 1
                    yield tuple(c if c is None or len(c) <= MAX_CELL_CHARS else c[:MAX_CELL_CHARS] for c in rec)
        except csv.Error as e:
            raise SourceError(f"{clean_text(self.name, 40)}: CSV parse error ({clean_text(e, 80)})") from None

    @property
    def ragged_rows(self) -> int:
        return self._ragged


def _table_name_from_file(fname: str) -> str:
    stem = Path(str(fname).replace("\\", "/")).name
    for ext in (".csv", ".tsv", ".txt", ".CSV", ".TSV"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return clean_text(stem, 64) or "table"


class CsvFileSource(Source):
    kind, origin = "csv", "csv_file"

    def __init__(self, path: str | os.PathLike | None = None, *, data: bytes | None = None, name: str | None = None,
                 limits: SourceLimits | None = None) -> None:
        super().__init__()
        self.limits = limits or SourceLimits()
        if (path is None) == (data is None):
            raise SourceError("give either a path or data")
        fname = name or (Path(path).name if path is not None else "upload.csv")
        self.name = clean_text(fname, 64) or "upload.csv"
        hint = "\t" if str(fname).lower().endswith(".tsv") else None
        if path is not None:
            p = Path(path)
            if not p.is_file():
                raise SourceError("file not found")
            self.sha256, self.bytes = sha256_file(p, self.limits.max_bytes)
            opener = lambda: open(p, "rb")  # noqa: E731
        else:
            if len(data) > self.limits.max_bytes:
                raise SourceError("data exceeds the byte limit")
            self.sha256, self.bytes = hashlib.sha256(data).hexdigest(), len(data)
            opener = lambda: io.BytesIO(data)  # noqa: E731
        self._table = CsvTable(_table_name_from_file(fname), opener, self.limits, hint_delim=hint, warnings=self.warnings)

    def tables(self) -> list[SourceTable]:
        return [self._table]


class CsvDirSource(Source):
    """A directory of .csv/.tsv files (non-recursive, symlinks ignored, sorted by name)."""
    kind, origin = "csv", "dir"

    def __init__(self, path: str | os.PathLike, *, name: str | None = None, limits: SourceLimits | None = None) -> None:
        super().__init__()
        self.limits = limits or SourceLimits()
        d = Path(path)
        if not d.is_dir():
            raise SourceError("directory not found")
        self.name = clean_text(name or d.name, 64) or "directory"
        files = sorted(p for p in d.iterdir()
                       if p.suffix.lower() in (".csv", ".tsv") and p.is_file() and not p.is_symlink()
                       and not p.name.startswith("."))
        if not files:
            raise SourceError("no .csv or .tsv files in the directory")
        if len(files) > self.limits.max_tables:
            self.warnings.append(f"only the first {self.limits.max_tables} files are read")
            files = files[: self.limits.max_tables]
        h = hashlib.sha256()
        total = 0
        names = unique_table_names([_table_name_from_file(p.name) for p in files])
        self._tables: list[SourceTable] = []
        for p, nm in zip(files, names):
            digest, size = sha256_file(p, self.limits.max_bytes)
            total += size
            if total > self.limits.max_total_bytes:
                raise SourceError("directory exceeds the total byte limit")
            h.update(p.name.encode() + b"\0" + digest.encode() + b"\0")
            hint = "\t" if p.suffix.lower() == ".tsv" else None
            self._tables.append(CsvTable(nm, (lambda p=p: open(p, "rb")), self.limits, hint_delim=hint, warnings=self.warnings))
        self.sha256, self.bytes = h.hexdigest(), total

    def tables(self) -> list[SourceTable]:
        return self._tables
