"""Workspace directories: ``workspaces/<slug>/{ontology.yaml, mapping.json, import_report.json, build/}``.

A workspace is one Postgres schema ``ws_<slug>`` plus these files. The slug is derived server side
(`session_slug`) from a sanitised source name and a short id; it is validated by `valid_slug` before it is ever
used in a path or an identifier. ``api_base`` (where the workspace's own API process listens) comes from the env var
``ISNAD_WORKSPACE_<SLUG>_URL`` or from ``workspaces/<slug>/api.json`` (``{"api_base": "http://127.0.0.1:8101"}``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .names import safe_prop

ROOT = Path(__file__).resolve().parents[2]
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
BASE_MAX = 24


def workspaces_root(override: str | os.PathLike | None = None) -> Path:
    return Path(override) if override else Path(os.environ.get("ISNAD_WORKSPACES_DIR") or ROOT / "workspaces")


def valid_slug(slug: object) -> bool:
    return isinstance(slug, str) and bool(SLUG_RE.match(slug))


def schema_for(slug: str) -> str:
    if not valid_slug(slug):
        raise ValueError("invalid workspace slug")
    return f"ws_{slug}"


def dir_for(slug: str, root: str | os.PathLike | None = None) -> Path:
    if not valid_slug(slug):
        raise ValueError("invalid workspace slug")
    return workspaces_root(root) / slug


def session_slug(source_name: str, session_id: uuid.UUID | str) -> str:
    """Deterministic slug: sanitised source name (no extension, no ``named:`` prefix) + 6 hex chars of the session id."""
    name = str(source_name or "")
    if name.startswith("named:"):
        name = name[len("named:"):]
    stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name)
    base = safe_prop(stem)[:BASE_MAX].rstrip("_") or "ws"
    tail = uuid.UUID(str(session_id)).hex[:6]
    slug = f"{base}_{tail}"
    assert valid_slug(slug)
    return slug


def _api_base(slug: str, d: Path) -> str | None:
    cand = os.environ.get(f"ISNAD_WORKSPACE_{slug.upper()}_URL")
    if not cand:
        try:
            cand = json.loads((d / "api.json").read_text()).get("api_base")
        except (OSError, ValueError, AttributeError):
            cand = None
    if isinstance(cand, str) and re.match(r"^https?://[A-Za-z0-9._:\[\]-]{1,200}(/[A-Za-z0-9._~/-]{0,100})?$", cand):
        return cand.rstrip("/")
    return None


def read_report(slug: str, root: str | os.PathLike | None = None) -> dict | None:
    try:
        return json.loads((dir_for(slug, root) / "import_report.json").read_text())
    except (OSError, ValueError):
        return None


def list_workspaces(root: str | os.PathLike | None = None) -> list[dict]:
    base = workspaces_root(root)
    out: list[dict] = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.is_symlink() or not valid_slug(d.name):
            continue
        y = d / "ontology.yaml"
        if not y.is_file():
            continue
        rep = read_report(d.name, root) or {}
        out.append({"slug": d.name, "schema": rep.get("schema") or schema_for(d.name),
                    "ontology_hash": rep.get("ontology_hash") or hashlib.sha256(y.read_bytes()).hexdigest(),
                    "imported_at": rep.get("imported_at"),
                    "rows": rep.get("rows"), "api_base": _api_base(d.name, d)})
    return out
