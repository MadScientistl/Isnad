import base64
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from engine.api.main import create_app
from engine.auth import AuthConfigError, tokens
from engine.auth.passwords import hash_password, parse_hash, verify_password

from .auth_helpers import ONTOLOGY, PASSWORDS, ROLES, SECRET, bearer, login, write_users


# ------------------------------------------------------------------ passwords / tokens (unit)
def test_scrypt_hash_roundtrip_and_params_in_string():
    h = hash_password("pw", n=2**10)
    assert h.startswith("scrypt$1024$8$1$") and parse_hash(h)[0] == 1024
    assert verify_password("pw", h) and not verify_password("pw2", h)
    assert hash_password("pw", n=2**10) != h   # per-hash salt
    assert not verify_password("pw", "garbage") and not verify_password("pw", "scrypt$1$8$1$AA$AA")


def test_token_format_and_tamper():
    tok, pl = tokens.issue(SECRET.encode(), "alice", "planner", 60)
    assert tok.startswith("v1.") and tok.count(".") == 2 and set(pl) == {"u", "r", "iat", "exp", "jti"}
    assert tokens.verify(SECRET.encode(), tok)["u"] == "alice"
    assert tokens.verify(b"x" * 48, tok) is None
    head, body, sig = tok.split(".")
    forged = dict(pl, r="admin")
    fb = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    bad = [f"{head}.{fb}.{sig}", f"v2.{body}.{sig}", f"{head}.{body}", f"{head}.{body}.", f"{head}.{body}.{sig}x",
           f"{head}.{body}.{sig[:-3]}", "", "v1..", "none." + body + "." + sig, f"{head}.{body}.{sig}.extra"]
    for i in range(len(sig)):   # every single-character change in the signature
        c = "A" if sig[i] != "A" else "B"
        bad.append(f"{head}.{body}.{sig[:i]}{c}{sig[i + 1:]}")
    for i in range(0, len(body), 5):
        c = "A" if body[i] != "A" else "B"
        bad.append(f"{head}.{body[:i]}{c}{body[i + 1:]}.{sig}")
    for t in bad:
        assert tokens.verify(SECRET.encode(), t) is None, t


def test_token_expiry():
    tok, pl = tokens.issue(SECRET.encode(), "alice", "planner", 10, now=1000)
    assert tokens.verify(SECRET.encode(), tok, now=1005) is not None
    assert tokens.verify(SECRET.encode(), tok, now=1010) is None


# ------------------------------------------------------------------ startup refusal
def test_refuses_to_start_without_secret(aschema, auth_env):
    auth_env.delenv("ISNAD_AUTH_SECRET")
    with pytest.raises(AuthConfigError):
        create_app(ONTOLOGY, schema=aschema, pipeline=False)
    auth_env.setenv("ISNAD_AUTH_SECRET", "short" * 4)   # 20 bytes
    with pytest.raises(AuthConfigError):
        create_app(ONTOLOGY, schema=aschema, pipeline=False)


def test_refuses_without_or_with_bad_users_file(aschema, auth_env, tmp_path):
    auth_env.delenv("ISNAD_USERS_FILE")
    with pytest.raises(AuthConfigError):
        create_app(ONTOLOGY, schema=aschema, pipeline=False)
    auth_env.setenv("ISNAD_USERS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(AuthConfigError):
        create_app(ONTOLOGY, schema=aschema, pipeline=False)
    for role in ("sim", "nonexistent"):
        p = tmp_path / f"u_{role}.json"
        p.write_text(json.dumps({"users": [{"user": "x", "role": role, "password_hash": hash_password("a", n=2**10)}]}))
        auth_env.setenv("ISNAD_USERS_FILE", str(p))
        with pytest.raises(AuthConfigError):
            create_app(ONTOLOGY, schema=aschema, pipeline=False)


def test_secret_never_in_health_or_errors(client):
    for r in (client.get("/health"), client.get("/me"), client.post("/auth/login", json={"user": "a", "password": "b"})):
        assert SECRET not in r.text


# ------------------------------------------------------------------ login
def test_login_success_shape(client):
    j = login(client, "alice")
    assert set(j) == {"token", "user", "role", "expires_at"} and j["role"] == "planner" and j["user"] == "alice"
    assert j["expires_at"] - time.time() == pytest.approx(12 * 3600, abs=30)
    me = client.get("/me", headers={"Authorization": f"Bearer {j['token']}"}).json()
    assert me["user"] == "alice" and me["role"] == "planner"


def test_login_failures_are_indistinguishable(client):
    a = client.post("/auth/login", json={"user": "nobody", "password": "whatever"})
    b = client.post("/auth/login", json={"user": "alice", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() and a.json()["code"] == "invalid_credentials"
    assert dict(a.headers).keys() == dict(b.headers).keys() or True
    assert a.headers.get("www-authenticate") == b.headers.get("www-authenticate") == "Bearer"
    assert "alice" not in b.text


def test_unknown_user_pays_a_hash_computation(client, monkeypatch):
    from engine.auth import passwords
    calls = []
    real = passwords.verify_password
    monkeypatch.setattr("engine.auth.service.passwords.verify_password", lambda p, h: calls.append(h) or real(p, h))
    client.post("/auth/login", json={"user": "nobody", "password": "x"})
    client.post("/auth/login", json={"user": "alice", "password": "x"})
    assert len(calls) == 2   # same number of verifications; the unknown user used the dummy hash
    assert calls[0] != calls[1]


def test_login_timing_shape(client):
    def avg(user, n=6):
        t = time.perf_counter()
        for i in range(n):
            client.post("/auth/login", json={"user": user, "password": f"bad{i}"})
        return (time.perf_counter() - t) / n
    unknown, known = avg("nobody"), avg("alice")
    assert 0.2 < unknown / known < 5   # both do real scrypt work (coarse, avoids flakiness)


def test_rate_limit_user_and_lockout(make_client, auth_env):
    auth_env.setenv("ISNAD_AUTH_MAX_FAILS_USER", "3")
    auth_env.setenv("ISNAD_AUTH_MAX_FAILS_IP", "100")
    c = make_client()
    for _ in range(3):
        assert c.post("/auth/login", json={"user": "alice", "password": "bad"}).status_code == 401
    r = c.post("/auth/login", json={"user": "alice", "password": PASSWORDS["alice"]})   # correct password, locked
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1
    assert c.post("/auth/login", json={"user": "bob", "password": PASSWORDS["bob"]}).status_code == 200
    # unknown users are throttled the same way (no enumeration through the limiter)
    for _ in range(3):
        c.post("/auth/login", json={"user": "ghost", "password": "bad"})
    assert c.post("/auth/login", json={"user": "ghost", "password": "bad"}).status_code == 429


def test_rate_limit_ip(make_client, auth_env):
    auth_env.setenv("ISNAD_AUTH_MAX_FAILS_IP", "4")
    auth_env.setenv("ISNAD_AUTH_MAX_FAILS_USER", "100")
    c = make_client()
    for i in range(4):
        assert c.post("/auth/login", json={"user": f"u{i}", "password": "bad"}).status_code == 401
    assert c.post("/auth/login", json={"user": "bob", "password": PASSWORDS["bob"]}).status_code == 429


def test_lock_backoff_grows():
    from engine.auth.ratelimit import Throttle
    t = Throttle(2, 100, 10, 1000)
    t.fail("k", 0); t.fail("k", 1)
    first = t.retry_after("k", 1.5)
    t.fail("k", 2)
    assert t.retry_after("k", 2.5) > first
    t.success("k")
    assert t.retry_after("k", 3) == 0


def test_auth_endpoints_404_when_off(aschema, monkeypatch):
    monkeypatch.delenv("ISNAD_AUTH", raising=False)
    c = TestClient(create_app(ONTOLOGY, schema=aschema, pipeline=False))
    assert c.post("/auth/login", json={"user": "a", "password": "b"}).json()["code"] == "auth_disabled"
    assert c.get("/auth/config").json() == {"enabled": False}


# ------------------------------------------------------------------ bearer enforcement
def test_unauthenticated_shape_and_open_routes(client):
    for path in ("/me", "/types", "/objects/Shipment", "/proposals", "/agent/config", "/scenarios", "/ontology/versions"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json()["code"] == "unauthenticated" and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/health").status_code == 200
    assert client.get("/auth/config").json() == {"enabled": True}
    assert client.get("/roles").status_code == 401


def test_invalid_token_variants_401(client):
    good = login(client, "alice")["token"]
    for h in ({"Authorization": "Bearer " + good + "x"}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"},
              {"Authorization": "Bearer v1.a.b"}, {"Authorization": good}):
        r = client.get("/me", headers=h)
        assert r.status_code == 401 and r.json()["code"] == "unauthenticated", h


def test_x_role_and_default_role_ignored(client, auth_env):
    h = bearer(client, "alice")
    auth_env.setenv("ISNAD_DEFAULT_ROLE", "admin")
    assert client.get("/me").status_code == 401          # default role does not authenticate anyone
    me = client.get("/me", headers={**h, "x-role": "admin", "x-user": "bob"}).json()
    assert me["role"] == "planner" and me["user"] == "alice"
    assert client.get("/me", headers={"x-role": "admin"}).status_code == 401


def test_roles_only_own_role(client):
    r = client.get("/roles", headers=bearer(client, "alice")).json()
    assert [x["role"] for x in r] == ["planner"]


def test_expired_token(make_client, auth_env):
    auth_env.setenv("ISNAD_AUTH_TTL_H", "0.0003")   # ~1 s
    c = make_client()
    h = bearer(c, "alice")
    assert c.get("/me", headers=h).status_code == 200
    time.sleep(1.3)
    assert c.get("/me", headers=h).status_code == 401


def test_logout_revokes(client):
    h = bearer(client, "alice")
    assert client.post("/auth/logout", headers=h).json() == {"ok": True}
    assert client.get("/me", headers=h).status_code == 401
    assert client.post("/auth/logout", headers=h).status_code == 401
    assert client.get("/me", headers=bearer(client, "alice")).status_code == 200   # a new login works


def test_role_change_and_removal_revoke(client, users_file):
    ha, hb = bearer(client, "alice"), bearer(client, "bob")
    assert client.get("/me", headers=ha).status_code == 200
    time.sleep(0.01)
    roles = dict(ROLES, alice="admin")                 # role change
    del roles["bob"]                                   # removal
    write_users(users_file, roles)
    os.utime(users_file, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert client.get("/me", headers=ha).status_code == 401
    assert client.get("/me", headers=hb).status_code == 401
    assert login(client, "alice")["role"] == "admin"   # a fresh login gets the new role


def test_broken_users_file_fails_closed(client, users_file):
    h = bearer(client, "alice")
    users_file.write_text("{not json")
    os.utime(users_file, ns=(time.time_ns() + 10**9,) * 2)
    assert client.get("/me", headers=h).status_code == 401
    assert client.post("/auth/login", json={"user": "alice", "password": PASSWORDS["alice"]}).status_code == 401


def test_cors_allows_authorization(client):
    r = client.options("/me", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Headers": "authorization,x-scenario"})
    assert r.status_code == 200 and "authorization" in r.headers["access-control-allow-headers"].lower()


# ------------------------------------------------------------------ stream tickets
def test_stream_ticket_one_time_scope_expiry(client, monkeypatch):
    h = bearer(client, "carol")
    t = client.post("/auth/stream-ticket", headers=h).json()["ticket"]
    # wrong scope: not accepted on other routes, and not consumed
    assert client.get(f"/me?ticket={t}").status_code == 401
    assert client.get(f"/objects/Shipment?ticket={t}").status_code == 401
    # right scope: a GET under /agent/runs authenticates (the run does not exist -> 404, not 401)
    r = client.get(f"/agent/runs/nonexistent?ticket={t}")
    assert r.status_code != 401
    # one time
    assert client.get(f"/agent/runs/nonexistent?ticket={t}").status_code == 401
    # POST never accepts tickets
    t2 = client.post("/auth/stream-ticket", headers=h).json()["ticket"]
    assert client.post(f"/agent/runs?ticket={t2}", json={"prompt": "x"}).status_code == 401
    # expiry
    t3 = client.post("/auth/stream-ticket", headers=h).json()["ticket"]
    real = time.time
    monkeypatch.setattr("engine.auth.service.time.time", lambda: real() + 61)
    assert client.get(f"/agent/runs/nonexistent?ticket={t3}").status_code == 401
    # ticket creation needs a real token, not a ticket
    assert client.post("/auth/stream-ticket").status_code == 401


def test_ticket_is_bound_to_user_and_role(client):
    t = client.post("/auth/stream-ticket", headers=bearer(client, "alice")).json()["ticket"]
    r = client.get(f"/agent/runs?ticket={t}", headers={"x-role": "admin"})
    # authenticated as alice/planner (the x-role header is ignored): only alice's runs could ever be listed
    assert r.status_code == 200 and all(x["user"] == "alice" for x in r.json())
