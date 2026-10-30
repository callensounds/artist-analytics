import pytest

WORKER_SECRETS = ("SECRET_KEY", "YOUTUBE_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")


@pytest.fixture(autouse=True)
def _no_host_secrets(monkeypatch):
    """env_get falls back to os.environ. A secret on the test machine must not change results."""
    for name in WORKER_SECRETS:
        monkeypatch.delenv(name, raising=False)
