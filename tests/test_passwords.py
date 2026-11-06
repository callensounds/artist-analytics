"""PBKDF2 via hashlib.sha256. These vectors were produced by hashlib.pbkdf2_hmac once."""

from passwords import hash_password, supported_hash, verify_password

_SALT = bytes.fromhex("00112233445566778899aabbccddeeff")
_NEW = (
    "pbkdf2_sha256$2000$00112233445566778899aabbccddeeff$"
    "7359c919b0da8d5e11589860a4fe62fde13c020be964ed95155624c5807c6701"
)
_OLD = (
    "pbkdf2_sha256$10000$00112233445566778899aabbccddeeff$"
    "ad55b3b4f93f779aa44ac05c112dc26490fc53142935a4fc24d2629b04a4637b"
)


def test_new_and_old_hashes_verify_without_pbkdf2_hmac():
    assert hash_password("demo123", salt=_SALT, iterations=2000) == _NEW
    assert verify_password("demo123", _NEW)
    assert verify_password("demo123", _OLD)
    assert not verify_password("wrong-password", _NEW)
    assert not verify_password("wrong-password", _OLD)
    assert "demo123" not in _NEW
    assert supported_hash(_OLD)
    assert not supported_hash("demo123")
    assert not supported_hash("")
