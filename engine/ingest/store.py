"""Persistence for the ingest flow: stored upload bytes (by sha256) and the append-only ingest_* tables.

DB functions take a connection whose caller owns the transaction and the identity (`scoped(conn, principal)`: the
tables are admin-only under FORCE RLS). Nothing here commits. Only this module writes ingest_session / ingest_proposal /
ingest_decision. Stored bytes live in ``ISNAD_INGEST_DIR`` (default ``var/ingest``) as ``<session>/<sha256>``; the file
name given by the client is only used (cleaned) for type detection and as the table name of a single CSV.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .names import clean_text
from .sources import open_source
from .sources.base import Source, SourceError, SourceLimits

ROOT = Path(__file__).resolve().parents[2]
SQLITE_MAGIC = b"SQLite format 3\x00"
EXTS = {".csv": "csv", ".tsv": "csv", ".zip": "zip", ".sqlite": "sqlite", ".sqlite3": "sqlite", ".db": "sqlite"}
NAMED_PREFIX = "named:"
_FN_BAD = re.compile(r"[^A-Za-z0-9._ -]+")


class IngestError(Exception):
    """An error with an HTTP-ish status and a stable code. The message never contains untrusted data."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def max_upload_bytes() -> int:
    try:
        v = int(os.environ.get("ISNAD_MAX_UPLOAD_BYTES", 20 * 1024 * 1024))
        return v if v > 0 else 20 * 1024 * 1024
    except ValueError:
        return 20 * 1024 * 1024


def ingest_dir(override: str | os.PathLike | None = None) -> Path:
    return Path(override) if override else Path(os.environ.get("ISNAD_INGEST_DIR") or ROOT / "var" / "ingest")


# ---------------------------------------------------------------- files
def safe_filename(name: str | None) -> str:
    """Basename only, control/bidi characters stripped, restricted alphabet, <= 64 chars. Never used as a path."""
    n = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    n = clean_text(n, 200)
    n = _FN_BAD.sub("_", n).strip(" ._") or "upload"
    if len(n) > 64:
        stem, dot, ext = n.rpartition(".")
        n = (stem[: 63 - len(ext)] + "." + ext) if dot and len(ext) <= 8 else n[:64]
    return n


def detect_kind(filename: str, head: bytes) -> str:
    """'csv' | 'zip' | 'sqlite' from the extension AND the magic bytes; they must agree."""
    ext = os.path.splitext(filename.lower())[1]
    want = EXTS.get(ext)
    if want is None:
        raise IngestError(415, "unsupported_type", "only csv, tsv, zip and sqlite files are supported")
    is_sqlite, is_zip = head.startswith(SQLITE_MAGIC), head[:2] == b"PK"
    if want == "sqlite" and not is_sqlite or want == "zip" and not is_zip:
        raise IngestError(415, "unsupported_type", "the file content does not match its extension")
    if want == "csv" and (is_sqlite or is_zip or head[:1024].count(b"\x00") > 8 and not head.startswith((b"\xff\xfe", b"\xfe\xff"))):
        raise IngestError(415, "unsupported_type", "the file content does not match its extension")
    return want


def tmp_path(root: Path | None = None) -> Path:
    d = ingest_dir(root) / "_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / uuid.uuid4().hex


def session_dir(session_id, root: Path | None = None) -> Path:
    return ingest_dir(root) / str(uuid.UUID(str(session_id)))


def stored_path(session_id, sha256: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("bad digest")
    return session_dir(session_id, root) / sha256


def commit_upload(tmp: Path, session_id, sha256: str, root: Path | None = None) -> Path:
    dest = stored_path(session_id, sha256, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    return dest


def remove_session_files(session_id, root: Path | None = None) -> None:
    shutil.rmtree(session_dir(session_id, root), ignore_errors=True)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def open_stored(path: Path, name: str, limits: SourceLimits | None = None) -> Source:
    """Open stored bytes: the kind is decided from the magic bytes; ``name`` only labels a single CSV."""
    with open(path, "rb") as f:
        head = f.read(16)
    kind = "sqlite" if head.startswith(SQLITE_MAGIC) else "zip" if head[:2] == b"PK" else "csv"
    return open_source(kind, str(path), name=name, limits=limits)


def open_session_source(sess: dict, registry, *, root: Path | None = None, limits: SourceLimits | None = None) -> Source:
    """Re-open the source of a session and verify it is unchanged (tamper check).

    Uploads: the stored file must still hash to ``source_sha256``. Named sources: re-opened by name from the
    server-side registry; its fingerprint must equal the recorded one."""
    sname = str(sess["source_name"])
    if sname.startswith(NAMED_PREFIX):
        reg = registry() if callable(registry) else (registry or {})
        ns = reg.get(sname[len(NAMED_PREFIX):])
        if ns is None:
            raise IngestError(409, "source_unavailable", "the named source is not configured any more")
        try:
            src = ns.open(limits)
        except SourceError:
            raise IngestError(409, "source_unavailable", "the named source cannot be read") from None
        if src.sha256 != sess["source_sha256"]:
            src.close()
            raise IngestError(409, "source_changed", "the source changed since it was profiled")
        return src
    path = stored_path(sess["id"], sess["source_sha256"], root)
    if not path.is_file():
        raise IngestError(409, "source_missing", "the stored source file is missing")
    if file_sha256(path) != sess["source_sha256"]:
        raise IngestError(409, "source_tampered", "the stored source file does not match its recorded hash")
    try:
        return open_stored(path, sname, limits)
    except SourceError:
        raise IngestError(409, "source_unavailable", "the stored source cannot be read") from None


# ---------------------------------------------------------------- tables
def _one(conn, query, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchone()


def _all(conn, query, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def insert_session(conn, session_id, user: str, kind: str, name: str, sha256: str, profile: dict) -> dict:
    return _one(conn, "INSERT INTO ingest_session (id, created_by, source_kind, source_name, source_sha256, profile, "
                      "profile_hash) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (session_id, user, kind, name, sha256, Jsonb(profile), profile["profile_hash"]))


def get_session(conn, session_id) -> dict | None:
    return _one(conn, "SELECT * FROM ingest_session WHERE id = %s", (session_id,))


def list_sessions(conn, limit: int = 100) -> list[dict]:
    return _all(conn, "SELECT s.id, s.created_by, s.created_at, s.source_kind, s.source_name, s.source_sha256, "
                      "s.profile_hash, (SELECT count(*) FROM ingest_proposal p WHERE p.session_id = s.id) AS proposals "
                      "FROM ingest_session s ORDER BY s.created_at DESC LIMIT %s", (limit,))


def recent_sessions(conn, user: str, hours: int = 24) -> int:
    return _one(conn, "SELECT count(*) AS n FROM ingest_session WHERE created_by = %s AND created_at > now() - "
                      "make_interval(hours => %s)", (user, hours))["n"]


def daily_cost(conn, user: str) -> float:
    return float(_one(conn, "SELECT coalesce(sum(cost_usd),0) AS usd FROM ingest_proposal WHERE created_by = %s AND "
                            "created_at > now() - interval '24 hours'", (user,))["usd"])


def insert_proposal(conn, session_id, user: str, kind: str, proposal: dict, yaml_text: str, mapping: dict,
                    validation: dict | None, *, parent_id=None, model: str | None = None, cost_usd: float = 0.0) -> dict:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("ingest-seq:" + str(session_id),))
    seq = _one(conn, "SELECT coalesce(max(seq),0) + 1 AS n FROM ingest_proposal WHERE session_id = %s", (session_id,))["n"]
    return _one(conn, "INSERT INTO ingest_proposal (id, session_id, seq, parent_id, proposal, yaml, mapping, validation, "
                      "model, cost_usd, created_by, kind) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (uuid.uuid4(), session_id, seq, parent_id, Jsonb(proposal), yaml_text, Jsonb(mapping),
                 Jsonb(validation) if validation is not None else None, model, round(float(cost_usd), 6), user, kind))


def get_proposal(conn, proposal_id) -> dict | None:
    return _one(conn, "SELECT * FROM ingest_proposal WHERE id = %s", (proposal_id,))


def list_proposals(conn, session_id) -> list[dict]:
    return _all(conn, "SELECT id, seq, parent_id, kind, model, cost_usd, created_by, created_at, "
                      "(validation->>'ok')::boolean AS ok FROM ingest_proposal WHERE session_id = %s ORDER BY seq",
                (session_id,))


def repair_depth(conn, proposal_id) -> int:
    """Number of 'repair' proposals on the parent chain of ``proposal_id`` (inclusive)."""
    r = _one(conn, "WITH RECURSIVE c AS (SELECT id, parent_id, kind FROM ingest_proposal WHERE id = %s UNION ALL "
                   "SELECT p.id, p.parent_id, p.kind FROM ingest_proposal p JOIN c ON p.id = c.parent_id) "
                   "SELECT count(*) FILTER (WHERE kind = 'repair') AS n FROM c", (proposal_id,))
    return int(r["n"])


def insert_decision(conn, proposal_id, decision: str, approver: str, approver_role: str, comment: str | None,
                    items: list) -> dict:
    return _one(conn, "INSERT INTO ingest_decision (id, proposal_id, decision, items, comment, approver, approver_role) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (uuid.uuid4(), proposal_id, decision, Jsonb(items), comment, approver, approver_role))


def get_decision(conn, decision_id) -> dict | None:
    return _one(conn, "SELECT * FROM ingest_decision WHERE id = %s", (decision_id,))


def decisions_for(conn, proposal_id) -> list[dict]:
    return _all(conn, "SELECT * FROM ingest_decision WHERE proposal_id = %s ORDER BY at", (proposal_id,))
