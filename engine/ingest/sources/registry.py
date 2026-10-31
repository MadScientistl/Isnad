"""Named server-side SQL sources from ``ISNAD_IMPORT_SOURCES``.

Format: entries separated by newlines or ``;``, each ``name=postgresql://...`` or ``name=sqlite:/abs/path``.
The DSN / path never appears in listings, reprs or error messages."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .base import Source, SourceError, SourceLimits

log = logging.getLogger(__name__)
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")


@dataclass(frozen=True)
class NamedSource:
    name: str
    kind: str                                   # "postgres" | "sqlite"
    _target: str = field(repr=False, compare=False, default="")

    def listing(self) -> dict:
        return {"name": self.name, "kind": self.kind}

    def open(self, limits: SourceLimits | None = None, schema: str = "public") -> Source:
        from . import open_source
        if self.kind == "sqlite":
            return open_source("sqlite", self._target, name=self.name, limits=limits)
        return open_source("postgres", dsn=self._target, name=self.name, schema=schema, limits=limits)


def load_named_sources(env: dict | None = None) -> dict[str, NamedSource]:
    env = os.environ if env is None else env
    raw = env.get("ISNAD_IMPORT_SOURCES", "") or ""
    out: dict[str, NamedSource] = {}
    for i, entry in enumerate(re.split(r"[\n;]+", raw)):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, target = entry.partition("=")
        name, target = name.strip(), target.strip()
        if not sep or not _NAME.match(name) or name in out:
            log.warning("ISNAD_IMPORT_SOURCES entry %d ignored (bad or duplicate name)", i + 1)
            continue
        if target.startswith(("postgresql://", "postgres://")):
            out[name] = NamedSource(name, "postgres", target)
        elif target.startswith("sqlite:"):
            path = target[len("sqlite:"):]
            if path.startswith("//"):
                path = path[2:]
            if not os.path.isabs(path):
                log.warning("ISNAD_IMPORT_SOURCES entry %r ignored (sqlite path must be absolute)", name)
                continue
            out[name] = NamedSource(name, "sqlite", path)
        else:
            log.warning("ISNAD_IMPORT_SOURCES entry %r ignored (unknown scheme)", name)
    return out
