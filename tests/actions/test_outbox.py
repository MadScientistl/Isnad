import hashlib
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from act_helpers import PLANNER, T0
from engine.actions import execute
from engine.actions import outbox
from engine.db import bitemporal as bt

T1 = T0 + timedelta(hours=1)
NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Receiver:
    def __init__(self):
        self.script: list[int] = []   # status codes to answer with; empty => 200
        self.calls: list[dict] = []
        rcv = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                rcv.calls.append({"headers": dict(self.headers), "body": body})
                code = rcv.script.pop(0) if rcv.script else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def rcv(monkeypatch):
    r = Receiver()
    monkeypatch.setenv("AMARH_WEBHOOK_OPS_URL", r.url)
    monkeypatch.setenv("AMARH_WEBHOOK_SECRET", "s3cret")
    yield r
    r.close()


def fire(conn, world):
    r = execute(conn, "close_port", {"port": world["emea_port"]}, PLANNER, valid_at=T1)
    (oid,) = conn.execute("SELECT id FROM webhook_outbox WHERE action_id=%s", [r.action_id]).fetchone()
    return r, oid


def state(conn, oid):
    conn.rollback()
    return conn.execute("SELECT status, attempt, http_status, error FROM webhook_state WHERE outbox_id=%s",
                        [oid]).fetchone()


def snapshot(conn):
    conn.rollback()
    return (conn.execute("SELECT count(*) FROM audit_event").fetchone()[0],
            conn.execute("SELECT count(*) FROM graph_outbox").fetchone()[0],
            conn.execute("SELECT count(*) FROM obj_port WHERE action_id IS NOT NULL").fetchone()[0])


def test_success_headers_and_signature(conn, world, factory, rcv):
    r, oid = fire(conn, world)
    assert outbox.deliver_due(factory, NOW) == 1
    assert state(conn, oid) == ("delivered", 1, 200, None)
    (call,) = rcv.calls
    assert call["headers"]["Idempotency-Key"] == f"{r.action_id}:notify_ops"
    ts = call["headers"]["X-Amarh-Timestamp"]
    assert abs(int(ts) - NOW.timestamp()) < 3600 or abs(int(ts) - __import__("time").time()) < 60
    want = "sha256=" + hmac.new(b"s3cret", ts.encode() + b"." + call["body"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Amarh-Signature"] == want
    body = json.loads(call["body"])
    assert body["action"] == "close_port" and body["action_id"] == str(r.action_id)
    assert outbox.deliver_due(factory, NOW) == 0 and len(rcv.calls) == 1   # delivered is final


def test_retry_after_500_keeps_idempotency_key(conn, world, factory, rcv):
    _, oid = fire(conn, world)
    rcv.script = [500]
    assert outbox.deliver_due(factory, NOW) == 1
    st = state(conn, oid)
    assert st[0] == "retry" and st[1] == 1 and st[2] == 500
    nxt = conn.execute("SELECT next_attempt_at FROM webhook_state WHERE outbox_id=%s", [oid]).fetchone()[0]
    assert NOW + timedelta(seconds=1) <= nxt <= NOW + timedelta(seconds=3)      # 2^1 s * jitter(0.5..1.5)
    assert outbox.deliver_due(factory, NOW) == 0                                 # not due yet
    assert outbox.deliver_due(factory, NOW + timedelta(minutes=1)) == 1
    assert state(conn, oid) == ("delivered", 2, 200, None)
    k = {c["headers"]["Idempotency-Key"] for c in rcv.calls}
    assert len(rcv.calls) == 2 and len(k) == 1


def test_permanent_failure_dead_and_data_graph_unaffected(conn, world, factory, rcv):
    r, oid = fire(conn, world)
    before = snapshot(conn)
    rcv.script = [503] * 10
    t = NOW
    for _ in range(5):
        assert outbox.deliver_due(factory, t) == 1
        t += timedelta(hours=1)
    assert state(conn, oid)[:3] == ("dead", 5, 503)
    assert outbox.deliver_due(factory, t) == 0
    assert snapshot(conn) == before                       # D2: audit, data and graph outbox untouched
    assert bt.get_object(conn, "Port", world["emea_port"], as_of_valid=T1)["status"] == "closed"
    assert conn.execute("SELECT status FROM audit_event WHERE action_id=%s", [r.action_id]).fetchone()[0] == "executed"
    # delivery history is append-only: one row per attempt plus the initial pending
    assert conn.execute("SELECT count(*) FROM webhook_delivery WHERE outbox_id=%s", [oid]).fetchone()[0] == 6


def test_manual_retry_after_dead(conn, world, factory, rcv):
    _, oid = fire(conn, world)
    rcv.script = [500] * 5
    t = NOW
    for _ in range(5):
        outbox.deliver_due(factory, t)
        t += timedelta(hours=1)
    assert state(conn, oid)[0] == "dead"
    outbox.retry(conn, oid)
    assert state(conn, oid) == ("pending", 0, None, None)
    assert outbox.deliver_due(factory, t) == 1
    assert state(conn, oid)[:2] == ("delivered", 1)
    with pytest.raises(ValueError):
        outbox.retry(conn, oid)
    with pytest.raises(ValueError):
        outbox.retry(conn, __import__("uuid").uuid4())


def test_missing_url_is_dead_with_error(conn, world, factory, monkeypatch):
    monkeypatch.delenv("AMARH_WEBHOOK_OPS_URL", raising=False)
    _, oid = fire(conn, world)
    assert outbox.deliver_due(factory, NOW) == 1
    assert state(conn, oid) == ("dead", 1, None, "no url")


def test_unreachable_receiver_retries(conn, world, factory, monkeypatch):
    monkeypatch.setenv("AMARH_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("AMARH_WEBHOOK_OPS_URL", "http://127.0.0.1:9/none")
    _, oid = fire(conn, world)
    outbox.deliver_due(factory, NOW)
    st = state(conn, oid)
    assert st[0] == "retry" and st[2] is None and "Error" in st[3]


def test_worker_thread_delivers_and_stops(conn, world, factory, rcv):
    _, oid = fire(conn, world)
    stop = threading.Event()
    th = threading.Thread(target=outbox.run_worker, args=(stop, 0.05, factory), daemon=True)
    th.start()
    for _ in range(100):
        if state(conn, oid)[0] == "delivered":
            break
        stop.wait(0.05)
    stop.set()
    th.join(2)
    assert state(conn, oid)[0] == "delivered" and not th.is_alive()


def test_no_secret_means_dead_and_no_request(conn, world, factory, rcv, monkeypatch):
    monkeypatch.delenv("AMARH_WEBHOOK_SECRET", raising=False)
    _, oid = fire(conn, world)
    outbox.deliver_due(factory, NOW)
    assert state(conn, oid) == ("dead", 1, None, "no secret")
    assert rcv.calls == []


def test_redirects_not_followed_and_scheme_restricted():
    import http.server, threading
    hits = []

    class R(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(302 if self.path == "/a" else 200)
            self.send_header("Location", "/b")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import os
    os.environ["AMARH_WEBHOOK_SECRET"] = "x"
    try:
        code, err = outbox._send(f"http://127.0.0.1:{srv.server_port}/a", "POST", b"{}", "k")
    finally:
        srv.shutdown()
    assert code == 302 and hits == ["/a"]
    assert outbox._send("file:///etc/passwd", "POST", b"{}", "k") == (None, "bad url scheme")


def test_retry_only_when_dead(conn, world, factory, rcv):
    _, oid = fire(conn, world)
    with pytest.raises(ValueError):
        outbox.retry(conn, oid)  # still pending
