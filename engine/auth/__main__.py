"""`python -m engine.auth hash` : read a password (prompt, or stdin when not a TTY) and print its scrypt hash."""
import getpass
import sys

from engine.auth.passwords import hash_password


def main(argv: list[str]) -> int:
    if argv[:1] != ["hash"]:
        print("usage: python -m engine.auth hash   (password is read from the prompt or stdin, never argv)", file=sys.stderr)
        return 2
    pw = getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    if not pw:
        print("empty password", file=sys.stderr)
        return 1
    print(hash_password(pw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
