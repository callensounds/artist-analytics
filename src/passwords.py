"""Password hashes for the Workers Free plan.

PBKDF2-HMAC-SHA256 from the Python standard library (``hashlib``), which Pyodide
already provides. 10,000 iterations is about a millisecond here, so a login does
not spend the isolate's CPU budget on a long key-stretching loop. Stored rows
cannot ask for more than 20,000 iterations.
"""

from __future__ import annotations

import hashlib
import secrets

ITERATIONS = 10_000
_MAX_ITERATIONS = 20_000
_PREFIX = "pbkdf2_sha256"


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    rounds = min(max(int(iterations), 1), _MAX_ITERATIONS)
    raw_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, rounds)
    return f"{_PREFIX}${rounds}${raw_salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, rounds_text, salt_hex, digest_hex = (stored or "").split("$", 3)
        rounds = int(rounds_text)
        if prefix != _PREFIX or rounds < 1 or rounds > _MAX_ITERATIONS:
            return False
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return secrets.compare_digest(check, digest)
