"""Thread-safe sync + reasoning pipeline (used by the sim thread and API request threads)."""
from __future__ import annotations

import threading
import time
from uuid import UUID

import psycopg
from psycopg import sql
from pyoxigraph import NamedNode

from engine.db import connect, get_dsn
from engine.reason import materialize
from . import iri
from .store import SyncConfig, consume_outbox, get_last_seq, open_store, rebuild

_SKIP_PREDS = {iri.RDF_TYPE.value, "http://www.w3.org/2002/07/owl#sameAs"}


class SyncPipeline:
    def __init__(self, dsn=None, schema=None, store_path=None, min_reason_interval_s: float = 5.0,
                 cfg: SyncConfig | None = None):
        self.dsn = dsn or get_dsn()
        self.schema = schema
        self.cfg = cfg or SyncConfig()
        self.min_reason_interval_s = min_reason_interval_s
        self.store = open_store(store_path, self.cfg)
        self._lock = threading.RLock()
        self._dirty = True            # asserted graph changed since last reasoning
        self._last_reason = -1e18     # monotonic time of last reasoning pass
        self._reasoned_once = False

    def _conn(self) -> psycopg.Connection:
        return connect(self.dsn, schema=self.schema)

    def sync(self) -> int:
        """Consume the outbox into the store. Returns number of outbox rows applied."""
        with self._lock:
            c = self._conn()
            try:
                stats = consume_outbox(c, self.store, self.cfg)
            finally:
                c.close()
            if stats["added"] or stats["removed"]:
                self._dirty = True
            return stats["rows"]

    def maybe_reason(self, force: bool = False) -> bool:
        with self._lock:
            if not force:
                if not self._dirty:
                    return False
                if time.monotonic() - self._last_reason < self.min_reason_interval_s:
                    return False
            materialize(self.store, self.cfg.namespace)
            self._dirty = False
            self._last_reason = time.monotonic()
            return True

    def rebuild(self) -> None:
        with self._lock:
            c = self._conn()
            try:
                rebuild(c, self.store, self.cfg)
            finally:
                c.close()
            self._dirty = True

    def inferred_edges(self, ids) -> list[tuple[UUID, str, UUID]]:
        """Instance-to-instance inferred edges touching any id (excl. rdf:type, owl:sameAs)."""
        pre = f"{self.cfg.namespace}obj/"
        nsl = len(self.cfg.namespace)
        out: set = set()
        with self._lock:
            for i in ids:
                n = iri.obj_iri(self.cfg.namespace, i)
                for role in (0, 1):
                    pat = (n, None, None) if role == 0 else (None, None, n)
                    for q in self.store.quads_for_pattern(*pat, iri.G_INFERRED):
                        p = q.predicate.value
                        if p in _SKIP_PREDS or not isinstance(q.object, NamedNode) \
                                or not isinstance(q.subject, NamedNode):
                            continue
                        s, o = q.subject.value, q.object.value
                        if s.startswith(pre) and o.startswith(pre):
                            out.add((UUID(s[len(pre):]), p[nsl:] if p.startswith(self.cfg.namespace) else p,
                                     UUID(o[len(pre):])))
        return sorted(out, key=lambda t: (str(t[0]), t[1], str(t[2])))
