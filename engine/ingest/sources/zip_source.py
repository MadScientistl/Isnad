"""ZIP of CSV/TSV files. Members are streamed in memory limits-first, never extracted to disk."""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

from ..names import clean_text
from .base import Source, SourceError, SourceLimits, SourceTable, sha256_file, unique_table_names
from .csv_source import CsvTable, _table_name_from_file

import hashlib
import zlib

_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


def _unsafe_name(n: str) -> bool:
    if "\x00" in n or "\\" in n or n.startswith("/") or _WIN_DRIVE.match(n):
        return True
    return any(part == ".." for part in PurePosixPath(n).parts)


class _CappedMember(io.RawIOBase):
    """Decompressed stream that never yields more than ``limit`` bytes, whatever the header claims."""

    def __init__(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> None:
        self._f = zf.open(info)
        self._limit, self._n = limit, 0

    def readable(self) -> bool:
        return True

    def _rd(self, n: int) -> bytes:
        try:
            return self._f.read(n)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
            raise SourceError("zip member is corrupt or uses an unsupported method") from None

    def readinto(self, b) -> int:
        data = self._rd(len(b))
        self._n += len(data)
        if self._n > self._limit:
            raise SourceError("zip member expands beyond the allowed size")
        b[: len(data)] = data
        return len(data)

    def read(self, n: int = -1) -> bytes:  # used by the sniffing read(SNIFF_BYTES)
        data = self._rd(n if n is not None and n >= 0 else self._limit + 1)
        self._n += len(data)
        if self._n > self._limit:
            raise SourceError("zip member expands beyond the allowed size")
        return data

    def close(self) -> None:
        try:
            self._f.close()
        finally:
            super().close()


class ZipSource(Source):
    kind, origin = "csv", "zip"

    def __init__(self, path: str | os.PathLike | None = None, *, data: bytes | None = None, name: str | None = None,
                 limits: SourceLimits | None = None) -> None:
        super().__init__()
        self.limits = lim = limits or SourceLimits()
        if (path is None) == (data is None):
            raise SourceError("give either a path or data")
        if path is not None:
            self.sha256, self.bytes = sha256_file(path, lim.max_bytes)
            self._zf = self._open(lambda: zipfile.ZipFile(path))
        else:
            if len(data) > lim.max_bytes:
                raise SourceError("data exceeds the byte limit")
            self.sha256, self.bytes = hashlib.sha256(data).hexdigest(), len(data)
            self._zf = self._open(lambda: zipfile.ZipFile(io.BytesIO(data)))
        self.name = clean_text(name or (Path(path).name if path else "upload.zip"), 64) or "upload.zip"
        infos = self._zf.infolist()
        if len(infos) > lim.max_zip_members:
            self._zf.close()
            raise SourceError(f"zip has too many members ({len(infos)} > {lim.max_zip_members})")
        total = 0
        picked: list[zipfile.ZipInfo] = []
        for info in infos:
            n = info.filename
            if _unsafe_name(n):
                self._zf.close()
                raise SourceError("zip contains an unsafe member path")
            if info.is_dir():
                continue
            total += info.file_size
            if total > lim.max_total_bytes or info.file_size > lim.max_bytes:
                self._zf.close()
                raise SourceError("zip expands beyond the allowed total size")
            if info.compress_size > 0 and info.file_size / info.compress_size > lim.max_zip_ratio:
                self._zf.close()
                raise SourceError(f"zip compression ratio above {lim.max_zip_ratio} (possible zip bomb)")
            if info.compress_size == 0 and info.file_size > 1024:
                self._zf.close()
                raise SourceError("zip member has an inconsistent size")
            low = n.lower()
            if low.endswith((".zip", ".gz", ".tar", ".7z")):
                self.warnings.append("a nested archive was ignored")
                continue
            base = PurePosixPath(n).name
            if base.startswith(".") or "__MACOSX" in PurePosixPath(n).parts:
                continue
            if not low.endswith((".csv", ".tsv")):
                self.warnings.append("a non-csv member was ignored")
                continue
            if info.flag_bits & 0x1:
                self._zf.close()
                raise SourceError("encrypted zip members are not supported")
            picked.append(info)
        if not picked:
            self._zf.close()
            raise SourceError("no .csv or .tsv files in the zip")
        picked.sort(key=lambda i: i.filename)
        if len(picked) > lim.max_tables:
            self.warnings.append(f"only the first {lim.max_tables} members are read")
            picked = picked[: lim.max_tables]
        names = unique_table_names([_table_name_from_file(i.filename) for i in picked])
        self._tables: list[SourceTable] = []
        try:
            for info, nm in zip(picked, names):
                hint = "\t" if info.filename.lower().endswith(".tsv") else None
                opener = lambda info=info: io.BufferedReader(_CappedMember(self._zf, info, min(lim.max_bytes, info.file_size + 1024)))
                self._tables.append(CsvTable(nm, opener, lim, hint_delim=hint, warnings=self.warnings))
        except Exception:
            self._zf.close()
            raise

    @staticmethod
    def _open(factory):
        try:
            return factory()
        except zipfile.BadZipFile:
            raise SourceError("not a valid zip file") from None

    def tables(self) -> list[SourceTable]:
        return self._tables

    def close(self) -> None:
        self._zf.close()
