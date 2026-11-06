"""Password hashes for the Workers Free plan.

Pyodide's ``hashlib`` has SHA-256 and no ``pbkdf2_hmac``. This is PBKDF2-HMAC-SHA256
(RFC 8018) written with ``hashlib.sha256`` only. New hashes use 2,000 iterations,
about 2ms here, which matches the CPU of the old 10,000-iteration C call.

The stored form stays ``pbkdf2_sha256$rounds$salt$digest``. Rows written by
``hashlib.pbkdf2_hmac`` still verify, including 10,000-iteration ones. A stored
row cannot ask for more than 20,000 iterations. ``secrets.token_bytes`` runs
only when a hash is created, never at import.
"""

from __future__ import annotations

import hashlib
import secrets

ITERATIONS = 2_000
_MAX_ITERATIONS = 20_000
_PREFIX = "pbkdf2_sha256"
_BLOCK = 64


def _pbkdf2_sha256(password: bytes, salt: bytes, iterations: int) -> bytes:
    """One SHA-256 block (32 bytes). The loop reuses the HMAC pads."""
    if len(password) > _BLOCK:
        password = hashlib.sha256(password).digest()
    key = password.ljust(_BLOCK, b"\x00")
    outer = bytes(b ^ 0x5C for b in key)
    inner = bytes(b ^ 0x36 for b in key)

    def prf(message: bytes) -> bytes:
        return hashlib.sha256(outer + hashlib.sha256(inner + message).digest()).digest()

    block = prf(salt + (1).to_bytes(4, "big"))
    mixed = int.from_bytes(block, "big")
    for _ in range(iterations - 1):
        block = prf(block)
        mixed ^= int.from_bytes(block, "big")
    return mixed.to_bytes(32, "big")


def supported_hash(stored: str) -> bool:
    try:
        prefix, rounds_text, salt_hex, digest_hex = (stored or "").split("$", 3)
        rounds = int(rounds_text)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    return prefix == _PREFIX and 1 <= rounds <= _MAX_ITERATIONS and len(salt) >= 8 and len(digest) == 32


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    rounds = min(max(int(iterations), 1), _MAX_ITERATIONS)
    raw_salt = salt or secrets.token_bytes(16)
    digest = _pbkdf2_sha256(password.encode("utf-8"), raw_salt, rounds)
    return f"{_PREFIX}${rounds}${raw_salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not supported_hash(stored):
        return False
    _, rounds_text, salt_hex, digest_hex = stored.split("$", 3)
    check = _pbkdf2_sha256(password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_text))
    return secrets.compare_digest(check, bytes.fromhex(digest_hex))
