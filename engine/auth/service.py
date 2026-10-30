"""AuthService: login, token verification, logout revocation, stream tickets. State is per process."""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

from engine.auth import passwords, tokens
from engine.auth.ratelimit import Throttle
from engine.auth.users import AuthConfigError, UsersFile

MIN_SECRET_BYTES = 32
TICKET_TTL_S = 60
TICKET_SCOPE = "/agent/runs"
MAX_TICKETS_PER_USER = 20


class LoginFailed(Exception):
    """Generic: wrong user or wrong password, indistinguishable."""


class Throttled(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


@dataclass(frozen=True)
class Identity:
    user: str
    role: str


def enabled(env=None) -> bool:
    return (env or os.environ).get("ISNAD_AUTH", "0") == "1"


def _int(env, key, default):
    try:
        return int(env.get(key, default))
    except ValueError:
        raise AuthConfigError(f"{key} must be an integer") from None


class AuthService:
    def __init__(self, valid_roles, env=None):
        env = env if env is not None else os.environ
        secret = env.get("ISNAD_AUTH_SECRET", "").encode()
        if len(secret) < MIN_SECRET_BYTES:
            raise AuthConfigError(f"ISNAD_AUTH_SECRET is required with ISNAD_AUTH=1 (at least {MIN_SECRET_BYTES} bytes)")
        path = env.get("ISNAD_USERS_FILE", "")
        if not path:
            raise AuthConfigError("ISNAD_USERS_FILE is required with ISNAD_AUTH=1")
        self._secret = secret
        self.users = UsersFile(path, valid_roles)
        try:
            self.ttl_s = int(float(env.get("ISNAD_AUTH_TTL_H", "12")) * 3600)
        except ValueError:
            raise AuthConfigError("ISNAD_AUTH_TTL_H must be a number") from None
        if self.ttl_s <= 0:
            raise AuthConfigError("ISNAD_AUTH_TTL_H must be positive")
        win = float(_int(env, "ISNAD_AUTH_WINDOW_S", 900))
        base, cap = float(_int(env, "ISNAD_AUTH_LOCK_BASE_S", 30)), float(_int(env, "ISNAD_AUTH_LOCK_MAX_S", 900))
        self.by_user = Throttle(_int(env, "ISNAD_AUTH_MAX_FAILS_USER", 5), win, base, cap)
        self.by_ip = Throttle(_int(env, "ISNAD_AUTH_MAX_FAILS_IP", 20), win, base, cap)
        self._dummies: dict[tuple, str] = {}   # scrypt params -> dummy hash: unknown users still pay one scrypt
        self._revoked: dict[str, int] = {}                                   # jti -> exp
        self._tickets: dict[str, tuple[str, int, float]] = {}                # ticket -> (user, jti-less exp, expiry)
        self._lock = threading.Lock()

    def _dummy(self) -> str:
        """A dummy hash with the same cost parameters as the users' real hashes (so timing matches)."""
        sample = self.users.sample_hash()
        try:
            n, r, p = passwords.parse_hash(sample)[:3] if sample else (passwords.N, passwords.R, passwords.P)
        except ValueError:
            n, r, p = passwords.N, passwords.R, passwords.P
        if (n, r, p) not in self._dummies:
            self._dummies[(n, r, p)] = passwords.hash_password("dummy-password-for-timing", n=n, r=r, p=p)
        return self._dummies[(n, r, p)]

    # ----------------------------------------------------------------- login
    def login(self, user: str, password: str, ip: str) -> dict:
        ukey = "u:" + str(user).lower()[:64]
        ikey = "i:" + ip
        wait = max(self.by_ip.retry_after(ikey), self.by_user.retry_after(ukey))
        if wait:
            raise Throttled(wait)
        rec = self.users.get(user) if isinstance(user, str) else None
        ok = passwords.verify_password(password if isinstance(password, str) else "",
                                       rec["password_hash"] if rec else self._dummy())
        if not (ok and rec):
            self.by_user.fail(ukey)
            self.by_ip.fail(ikey)
            raise LoginFailed()
        self.by_user.success(ukey)
        tok, pl = tokens.issue(self._secret, rec["user"], rec["role"], self.ttl_s)
        return {"token": tok, "user": rec["user"], "role": rec["role"], "expires_at": pl["exp"]}

    # ---------------------------------------------------------------- verify
    def _purge(self, now: float):
        for j in [j for j, e in self._revoked.items() if e <= now]:
            del self._revoked[j]
        for t in [t for t, v in self._tickets.items() if v[2] <= now]:
            del self._tickets[t]

    def authenticate(self, token: str) -> tuple[Identity, dict] | None:
        pl = tokens.verify(self._secret, token)
        if pl is None:
            return None
        with self._lock:
            if pl["jti"] in self._revoked:
                return None
        rec = self.users.get(pl["u"])
        # the role is whatever the users file says NOW: a changed role or a removed user revokes the token
        if rec is None or rec["role"] != pl["r"] or rec["user"] != pl["u"]:
            return None
        return Identity(rec["user"], rec["role"]), pl

    def logout(self, payload: dict) -> None:
        now = time.time()
        with self._lock:
            self._purge(now)
            self._revoked[payload["jti"]] = payload["exp"]

    # --------------------------------------------------------- stream tickets
    def issue_ticket(self, ident: Identity) -> str:
        now = time.time()
        with self._lock:
            self._purge(now)
            if sum(1 for v in self._tickets.values() if v[0] == ident.user) >= MAX_TICKETS_PER_USER:
                raise Throttled(5)
            t = secrets.token_urlsafe(32)
            self._tickets[t] = (ident.user, 0, now + TICKET_TTL_S)
            return t

    def redeem_ticket(self, ticket: str, path: str) -> Identity | None:
        """One-time; only for GET paths under /agent/runs. A ticket presented for another path is not consumed."""
        if not (path == TICKET_SCOPE or path.startswith(TICKET_SCOPE + "/")):
            return None
        now = time.time()
        with self._lock:
            self._purge(now)
            v = self._tickets.pop(ticket, None) if isinstance(ticket, str) else None
        if v is None or v[2] <= now:
            return None
        rec = self.users.get(v[0])
        return Identity(rec["user"], rec["role"]) if rec else None
