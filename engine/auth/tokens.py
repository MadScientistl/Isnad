"""Signed bearer tokens: `v1.<b64url payload>.<b64url hmac-sha256>`, payload `{u, r, iat, exp, jti}`."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

PREFIX = "v1"
MAX_TOKEN_LEN = 2048


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in s):
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(secret: bytes, signing_input: str) -> bytes:
    return hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()


def issue(secret: bytes, user: str, role: str, ttl_s: int, *, now: float | None = None) -> tuple[str, dict]:
    t = int(now if now is not None else time.time())
    payload = {"u": user, "r": role, "iat": t, "exp": t + int(ttl_s), "jti": secrets.token_urlsafe(16)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    si = f"{PREFIX}.{body}"
    return f"{si}.{_b64(_sig(secret, si))}", payload


def verify(secret: bytes, token: str, *, now: float | None = None) -> dict | None:
    """Payload dict if the signature is valid and the token is not expired, else None (no reason is given)."""
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LEN:
            return None
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != PREFIX:
            return None
        si = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(_sig(secret, si), _unb64(parts[2])):
            return None
        # canonical encoding only: trailing base64 bits are otherwise malleable (same bytes, different token text)
        if _b64(_unb64(parts[2])) != parts[2] or _b64(_unb64(parts[1])) != parts[1]:
            return None
        d = json.loads(_unb64(parts[1]))
        if not (isinstance(d, dict) and isinstance(d.get("u"), str) and isinstance(d.get("r"), str)
                and isinstance(d.get("jti"), str) and type(d.get("exp")) is int and type(d.get("iat")) is int):
            return None
        if (now if now is not None else time.time()) >= d["exp"]:
            return None
        return d
    except Exception:  # noqa: BLE001
        return None
