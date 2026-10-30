"""Shared constants and helpers for tests/auth."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.auth.passwords import hash_password  # noqa: E402

ONTOLOGY = str(ROOT / "ontology/supply_chain.yaml")
SECRET = "s" * 48
PASSWORDS = {"alice": "alice-pw-1", "bob": "bob-pw-2", "carol": "carol-pw-3", "erin": "erin-pw-4", "dave": "dave-pw-5"}
ROLES = {"alice": "planner", "bob": "admin", "carol": "finance_analyst", "erin": "admin", "dave": "regional_ops_lead"}
_HASHES = {u: hash_password(p, n=2**10) for u, p in PASSWORDS.items()}   # cheap scrypt params for tests


def write_users(path, roles=None):
    roles = roles or ROLES
    path.write_text(json.dumps({"users": [{"user": u, "role": r, "password_hash": _HASHES[u]}
                                          for u, r in roles.items()]}))


def login(c, user, password=None):
    r = c.post("/auth/login", json={"user": user, "password": password or PASSWORDS[user]})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(c, user):
    return {"Authorization": f"Bearer {login(c, user)['token']}"}
