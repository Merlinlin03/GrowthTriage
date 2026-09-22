from __future__ import annotations

import getpass
import hashlib
import secrets


ITERATIONS = 600_000


def create_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, count, salt, digest = (encoded or "").split("$")
        if algorithm != "pbkdf2_sha256" or not 300_000 <= int(count) <= 1_000_000:
            return False
        expected = bytes.fromhex(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(count))
        return secrets.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    entered = getpass.getpass("Admin password: ")
    if len(entered) < 12:
        raise SystemExit("Use at least 12 characters.")
    print(create_hash(entered))
