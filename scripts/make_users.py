"""Write a sample users file (one user per role) with generated passwords.

Usage: python scripts/make_users.py <users.json> [--secret-out <file>]
Prints the generated passwords to stdout once; never commit the output. The hash file is written 0600."""
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.auth.passwords import hash_password  # noqa: E402

USERS = [("alice", "planner"), ("bob", "admin"), ("carol", "finance_analyst"), ("dave", "regional_ops_lead")]


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[0])
    rows, shown = [], []
    for name, role in USERS:
        pw = secrets.token_urlsafe(12)
        rows.append({"user": name, "role": role, "password_hash": hash_password(pw)})
        shown.append((name, role, pw))
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"users": rows}, f, indent=2)
    for name, role, pw in shown:
        print(f"{name:8} {role:20} {pw}")
    if "--secret-out" in argv:
        sp = Path(argv[argv.index("--secret-out") + 1])
        fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
        print(f"ISNAD_AUTH_SECRET written to {sp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
