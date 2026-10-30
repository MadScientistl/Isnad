"""Minimal additive-only ontology versioning (Phase 5, S10).

All functions run on the caller's connection and never commit. Registration is idempotent against the
LATEST version (hash compare); `hash` is deliberately not UNIQUE because a rollback re-registers an older
definition. Applying a proposal registers a version only: the running API keeps the old model until the
YAML file is updated and the API restarted ("restart required"). Files are written only when the caller
passes `write_path`.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from psycopg.rows import dict_row

from engine.compiler.model import OntologyError  # noqa: F401  (re-export)

from .diff import diff, is_empty, parse  # noqa: F401

SCHEMA_SQL = Path(__file__).with_name("schema.sql")
RESTART_NOTE = "restart required: update the YAML file and restart the API to apply this version"


class VersioningError(Exception):
    """status: 403 forbidden, 404 missing, 409 conflict, 422 invalid/breaking."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def apply(conn) -> None:
    conn.execute(SCHEMA_SQL.read_text())


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _require_admin(principal) -> None:
    if getattr(principal, "role", None) != "admin":
        raise VersioningError(403, "ontology versioning is admin-only")


def _q(conn, query: str, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def latest(conn) -> dict | None:
    r = _q(conn, "SELECT * FROM ontology_version ORDER BY id DESC LIMIT 1")
    return r[0] if r else None


def get_version(conn, version_id: int) -> dict | None:
    r = _q(conn, "SELECT * FROM ontology_version WHERE id = %s", (version_id,))
    return r[0] if r else None


def _insert_version(conn, yaml_text, parent_hash, by, source, note, parent_id=None) -> dict:
    return _q(conn, "INSERT INTO ontology_version (hash, parent_hash, yaml, created_by, source, note, parent_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
              (_hash(yaml_text), parent_hash, yaml_text, by, source, note, parent_id))[0]


def register_current(conn, yaml_text: str, by: str = "startup") -> dict:
    """Register the running definition (source 'startup'); no-op when it equals the latest version."""
    parse(yaml_text)  # validate
    cur = latest(conn)
    if cur is not None and cur["hash"] == _hash(yaml_text):
        return cur
    return _insert_version(conn, yaml_text, cur["hash"] if cur else None, by, "startup", None)


def list_versions(conn, principal) -> list[dict]:
    _require_admin(principal)
    return _q(conn, "SELECT id, hash, parent_hash, created_by, created_at, source, note "
                    "FROM ontology_version ORDER BY id DESC")


def version_detail(conn, principal, version_id: int) -> dict:
    _require_admin(principal)
    v = get_version(conn, version_id)
    if v is None:
        raise VersioningError(404, f"unknown version {version_id}")
    return v


def diff_versions(conn, principal, from_id: int, to_id: int) -> dict:
    _require_admin(principal)
    a, b = get_version(conn, from_id), get_version(conn, to_id)
    if a is None or b is None:
        raise VersioningError(404, "unknown version")
    return diff(a["yaml"], b["yaml"])


def propose(conn, principal, yaml_text: str, rationale: str = "") -> dict:
    """Store a change proposal against the latest version. Breaking proposals are stored (and diffable)."""
    _require_admin(principal)
    if len(rationale or "") > 4000:
        raise VersioningError(422, "rationale too long (max 4000 characters)")
    cur = latest(conn)
    if cur is None:
        raise VersioningError(409, "no registered ontology version; start the API first")
    try:
        d = diff(cur["yaml"], yaml_text)
    except OntologyError as e:
        raise VersioningError(422, str(e)) from e
    if is_empty(d):
        raise VersioningError(422, "proposal makes no change")
    return _q(conn, "INSERT INTO ontology_change_proposal (id, base_hash, yaml, diff, breaking, rationale, proposer) "
                    "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING *",
              (uuid.uuid4(), cur["hash"], yaml_text, json.dumps(d), bool(d["breaking"]), rationale,
               principal.user))[0]


def get_proposal(conn, principal, proposal_id) -> dict:
    _require_admin(principal)
    r = _q(conn, "SELECT p.*, d.decision, d.approver, d.comment AS decision_comment, d.at AS decided_at, "
                 "d.version_id FROM ontology_change_proposal p LEFT JOIN ontology_change_decision d "
                 "ON d.proposal_id = p.id WHERE p.id = %s", (proposal_id,))
    if not r:
        raise VersioningError(404, f"unknown proposal {proposal_id}")
    return r[0]


def list_proposals(conn, principal) -> list[dict]:
    _require_admin(principal)
    return _q(conn, "SELECT p.id, p.base_hash, p.breaking, p.rationale, p.proposer, p.created_at, p.diff, "
                    "d.decision, d.approver, d.version_id FROM ontology_change_proposal p "
                    "LEFT JOIN ontology_change_decision d ON d.proposal_id = p.id ORDER BY p.created_at DESC")


def decide(conn, principal, proposal_id, approve: bool, comment: str = "", *, write_path=None) -> dict:
    """Approve or reject. Approval of a breaking or stale proposal fails (nothing is recorded).

    Returns {decision, version, yaml, notes}. The YAML file is written only when `write_path` is given.
    Review L5: the proposer cannot approve their own proposal (dev override: ISNAD_ONTOLOGY_SELF_APPROVE=1);
    approving a proposal whose diff widens permissions of existing roles/actions (`sensitive`) needs a comment.
    """
    _require_admin(principal)
    if len(comment or "") > 4000:
        raise VersioningError(422, "comment too long (max 4000 characters)")
    p = get_proposal(conn, principal, proposal_id)
    if p["decision"] is not None:
        raise VersioningError(409, f"proposal already {p['decision']}")
    if approve and p["proposer"] == principal.user and os.environ.get("ISNAD_ONTOLOGY_SELF_APPROVE") != "1":
        raise VersioningError(403, "the proposer cannot approve their own ontology proposal")
    if not approve:
        _q(conn, "INSERT INTO ontology_change_decision (proposal_id, decision, approver, comment) "
                 "VALUES (%s,'rejected',%s,%s) RETURNING id", (p["id"], principal.user, comment))
        return {"decision": "rejected", "version": None, "yaml": None, "notes": []}
    cur = latest(conn)
    if cur is None or cur["hash"] != p["base_hash"]:
        raise VersioningError(409, "stale proposal: the ontology changed since it was proposed")
    d = diff(cur["yaml"], p["yaml"])
    if d["breaking"]:
        raise VersioningError(422, "breaking change cannot be applied (additive-only): " + "; ".join(d["breaking"]))
    if d.get("sensitive") and not (comment or "").strip():
        raise VersioningError(422, "this change widens permissions (" + "; ".join(d["sensitive"][:5]) +
                              "); approval needs a comment")
    try:
        with conn.transaction():
            v = _insert_version(conn, p["yaml"], cur["hash"], principal.user, "proposal", p["rationale"], cur["id"])
    except Exception as e:  # noqa: BLE001 - unique parent index: a concurrent approval won
        if type(e).__name__ == "UniqueViolation":
            raise VersioningError(409, "stale proposal: the ontology changed since it was proposed") from e
        raise
    _q(conn, "INSERT INTO ontology_change_decision (proposal_id, decision, approver, comment, version_id) "
             "VALUES (%s,'approved',%s,%s,%s) RETURNING id", (p["id"], principal.user, comment, v["id"]))
    notes = [RESTART_NOTE]
    if write_path is not None:
        Path(write_path).write_text(p["yaml"])
        notes = ["YAML written to " + str(write_path), "restart required: restart the API to load it"]
    return {"decision": "approved", "version": v, "yaml": p["yaml"], "notes": notes}


def rollback(conn, principal, version_id: int, *, write_path=None) -> dict:
    """Roll back `version_id`: register a new version (source 'rollback') that re-uses its parent's YAML."""
    _require_admin(principal)
    v = get_version(conn, version_id)
    if v is None:
        raise VersioningError(404, f"unknown version {version_id}")
    if not v["parent_hash"]:
        raise VersioningError(422, "version has no parent to roll back to")
    parent = _q(conn, "SELECT * FROM ontology_version WHERE hash = %s AND id < %s ORDER BY id DESC LIMIT 1",
                (v["parent_hash"], v["id"]))
    if not parent:
        raise VersioningError(422, "parent version not found")
    cur = latest(conn)
    new = _insert_version(conn, parent[0]["yaml"], cur["hash"], principal.user, "rollback",
                          f"rollback of version {version_id}")
    notes = [RESTART_NOTE, "non-destructive: columns and data added by the rolled-back version remain"]
    if write_path is not None:
        Path(write_path).write_text(parent[0]["yaml"])
    return {"version": new, "yaml": parent[0]["yaml"], "notes": notes}
