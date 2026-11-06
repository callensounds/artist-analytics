import hashlib

import pytest

WORKER_SECRETS = ("SECRET_KEY", "YOUTUBE_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")


@pytest.fixture(autouse=True)
def _pyodide_hashlib(monkeypatch):
    """Cloudflare Pyodide hashlib has SHA-256 and no pbkdf2_hmac."""
    monkeypatch.delattr(hashlib, "pbkdf2_hmac", raising=False)


@pytest.fixture(autouse=True)
def _no_host_secrets(monkeypatch):
    """env_get falls back to os.environ. A secret on the test machine must not change results."""
    for name in WORKER_SECRETS:
        monkeypatch.delenv(name, raising=False)
