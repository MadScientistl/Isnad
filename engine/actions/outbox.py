"""Webhook delivery worker (transactional outbox, Phase 3 / D2).

Rows are enqueued by `execute` in the action's own transaction (webhook_outbox + a `pending`
webhook_delivery row). This module delivers them after commit, as the owner/loader role. A delivery
failure NEVER touches data or the graph: it is retried with exponential backoff, then dead-lettered.
State of an outbox id = its latest webhook_delivery row (view `webhook_state`).

Request: POST JSON body, `Idempotency-Key: <action_id>:<hook>` (stable across retries),
`X-Amarh-Timestamp: <unix s>` and `X-Amarh-Signature: sha256=<hex HMAC of "<ts>.<body>">` keyed with
AMARH_WEBHOOK_SECRET (required: without it deliveries are dead-lettered with error 'no secret'). Redirects are
not followed; only http/https URLs are used.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .core import _load

TIMEOUT_S = 5.0

_PICK = (
    "SELECT s.seq, s.outbox_id, s.attempt, o.hook, o.url, o.payload, o.idempotency_key "
    "FROM webhook_state s JOIN webhook_outbox o ON o.id = s.outbox_id "
    "WHERE s.status IN ('pending','retry') AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= %s) "
    "AND o.id <> ALL(%s) ORDER BY s.seq LIMIT 1 FOR UPDATE OF s SKIP LOCKED"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect is returned as a plain 3xx response (not followed)
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(url: str, method: str, body: bytes, key: str) -> tuple[int | None, str | None]:
    """One delivery attempt. Returns (http_status, sanitized error). Errors never carry raw exception
    text (it can contain internal hosts / paths): only a fixed class name or 'HTTP <code>'."""
    secret = os.environ.get("AMARH_WEBHOOK_SECRET", "")
    if not secret:
        return None, "no secret"
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return None, "bad url scheme"
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json", "Idempotency-Key": key, "X-Amarh-Timestamp": ts,
        "X-Amarh-Signature": f"sha256={sig}"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code}"
    except Exception as e:  # connection refused, timeout, DNS...
        return None, type(e).__name__


def deliver_due(conn_factory, now: datetime | None = None, ontology=None) -> int:
    """Attempt every due delivery once (one attempt per outbox id per call). Returns attempts made."""
    model = _load(ontology)
    now = now or datetime.now(UTC)
    conn = conn_factory()
    done: list[UUID] = []
    try:
        while True:
            with conn.transaction():
                row = conn.execute(_PICK, [now, done]).fetchone()
                if row is None:
                    break
                _seq, oid, attempt, hook, url, payload, key = row
                done.append(oid)
                wd = model.webhooks.get(hook)
                url = url or (os.environ.get(wd.url_env) if wd else None)
                attempt += 1
                if not url:
                    status, http, err, nxt = "dead", None, "no url", None
                elif not os.environ.get("AMARH_WEBHOOK_SECRET"):
                    status, http, err, nxt = "dead", None, "no secret", None
                else:
                    http, err = _send(url, wd.method if wd else "POST",
                                      json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), key)
                    if http is not None and 200 <= http < 300:
                        status, err, nxt = "delivered", None, None
                    elif attempt >= (wd.max_attempts if wd else 5):
                        status, nxt = "dead", None
                    else:
                        status = "retry"
                        nxt = now + timedelta(seconds=(2 ** attempt) * random.uniform(0.5, 1.5))
                conn.execute(
                    "INSERT INTO webhook_delivery (outbox_id, attempt, status, http_status, error, next_attempt_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", [oid, attempt, status, http, err, nxt])
            conn.commit()
        conn.commit()
        return len(done)
    finally:
        conn.close()


def run_worker(stop_event: threading.Event, interval_s: float = 2.0, conn_factory=None, ontology=None) -> None:
    """Poll `deliver_due` until `stop_event` is set."""
    if conn_factory is None:
        from engine.db import connect
        conn_factory = connect
    while not stop_event.is_set():
        try:
            deliver_due(conn_factory, ontology=ontology)
        except Exception:  # keep the worker alive; the next tick retries
            pass
        stop_event.wait(interval_s)


def retry(conn, outbox_id: UUID) -> None:
    """Manual retry of a dead (or waiting) delivery: appends a new `pending` row (attempt reset)."""
    with conn.transaction():
        row = conn.execute("SELECT status FROM webhook_state WHERE outbox_id=%s", [outbox_id]).fetchone()
        if row is None:
            raise ValueError(f"webhook {outbox_id} not found")
        if row[0] != "dead":
            raise ValueError("webhook was already delivered" if row[0] == "delivered"
                             else "webhook is not dead (retry only applies to dead deliveries)")
        conn.execute("INSERT INTO webhook_delivery (outbox_id, attempt, status, next_attempt_at) "
                     "VALUES (%s,0,'pending',NULL)", [outbox_id])
    conn.commit()
