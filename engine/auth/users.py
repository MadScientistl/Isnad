"""Users file: JSON `{"users":[{"user":"alice","role":"planner","password_hash":"scrypt$..."}]}`.
Reloaded when its mtime changes (so a role change or removal revokes outstanding tokens). A file that cannot be
read or parsed on reload yields NO users (fail closed)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading

from engine.auth.passwords import parse_hash

log = logging.getLogger("isnad.auth")
USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")


class AuthConfigError(RuntimeError):
    pass


def parse_users(text: str, valid_roles) -> dict[str, dict]:
    d = json.loads(text)
    out: dict[str, dict] = {}
    for u in d["users"]:
        name, role, h = u["user"], u["role"], u["password_hash"]
        if not (isinstance(name, str) and USER_RE.fullmatch(name)):
            raise AuthConfigError("invalid user name in users file")
        if role == "sim" or role not in valid_roles:
            raise AuthConfigError(f"user {name!r}: role not allowed")
        parse_hash(h)
        if name.lower() in out:
            raise AuthConfigError(f"duplicate user {name!r}")
        out[name.lower()] = {"user": name, "role": role, "password_hash": h}
    return out


class UsersFile:
    def __init__(self, path: str, valid_roles):
        self.path, self.valid_roles = path, set(valid_roles)
        self._lock = threading.Lock()
        self._sig = None
        self._users: dict[str, dict] = {}
        self._load(strict=True)

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self, strict: bool):
        sig = self._stat()
        try:
            if sig is None:
                raise AuthConfigError("users file not readable")
            with open(self.path, encoding="utf-8") as f:
                self._users = parse_users(f.read(), self.valid_roles)
        except Exception as e:  # noqa: BLE001
            if strict:
                if isinstance(e, AuthConfigError):
                    raise
                raise AuthConfigError("users file invalid") from None
            log.warning("users file reload failed (%s); no users until fixed", type(e).__name__)
            self._users = {}
        self._sig = sig

    def get(self, name: str) -> dict | None:
        with self._lock:
            if self._stat() != self._sig:
                self._load(strict=False)
            return self._users.get(str(name).lower())

    def sample_hash(self) -> str | None:
        """Any stored hash (the dummy hash for unknown users copies its cost parameters)."""
        with self._lock:
            return next((u["password_hash"] for u in self._users.values()), None)
