"""scrypt password hashing. Format: `scrypt$<N>$<r>$<p>$<salt b64url>$<hash b64url>` (parameters live in the string)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

N, R, P, DKLEN, SALT_LEN = 2**14, 8, 1, 32, 16
_MAXMEM = 128 * 1024 * 1024
_MAX_N = 2**18   # a users file is trusted, but bound the work anyway


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, n: int = N, r: int = R, p: int = P) -> str:
    salt = os.urandom(SALT_LEN)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=_MAXMEM)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(h)}"


def parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes]:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("unsupported password hash format")
    n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    if not (2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 8):
        raise ValueError("password hash parameters out of range")
    salt, h = _unb64(parts[4]), _unb64(parts[5])
    if not (8 <= len(salt) <= 64 and 16 <= len(h) <= 64):
        raise ValueError("password hash lengths out of range")
    return n, r, p, salt, h


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare of the derived key. Malformed `stored` -> False (never raises)."""
    try:
        n, r, p, salt, h = parse_hash(stored)
        got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(h), maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, h)
