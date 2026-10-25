import json
import re

from fastapi.testclient import TestClient

import main

EMPTY = {
    "sources": [
        {
            "platform": "youtube",
            "label": "YouTube",
            "status": "missing_credentials",
            "reason": "Set the Worker secret YOUTUBE_API_KEY.",
            "profile_name": None,
            "profile_url": None,
            "metrics": [],
            "items": [],
            "notices": [],
        }
    ],
    "patterns": {
        "hour_labels": [],
        "weekday_labels": [],
        "release_hours": [],
        "social_hours": [],
        "weekdays": [],
    },
    "insights": ["No day-level dates or exact timestamps were returned for this artist yet."],
}


def setup_function():
    main.USERS.clear()
    main.USERS["artist@demo.com"] = {
        "id": 1,
        "email": "artist@demo.com",
        "password": "demo123",
        "artist_name": "Demo Artist",
    }
    main.NEXT_ID = 2


def test_demo_login_and_dashboard_do_not_fabricate_metrics(monkeypatch):
    async def fake_collect(name, env=None, http=None, cache=None):
        return EMPTY

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    login = client.post(
        "/login",
        data={"email": "Artist@Demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "1.2M" not in page.text
    assert "Analytics Score" not in page.text
    assert "Needs secret" in page.text
    assert "YOUTUBE_API_KEY" in page.text
    match = re.search(r'<script id="pattern-data" type="application/json">(.*?)</script>', page.text)
    assert json.loads(match.group(1))["release_hours"] == []


def test_artist_query_renders_exact_timestamps(monkeypatch):
    async def fake_collect(name, env=None, http=None, cache=None):
        assert name == "Taylor Swift"
        return {
            "sources": [
                {
                    "platform": "apple",
                    "label": "Apple Music",
                    "status": "ok",
                    "reason": None,
                    "profile_name": "Taylor Swift",
                    "profile_url": "https://music.apple.com/us/artist/taylor-swift/159260351",
                    "metrics": [{"label": "Exact timestamps", "available": True, "value": "1", "reason": None}],
                    "items": [
                        {
                            "title": "Lover",
                            "kind": "release",
                            "precision": "time",
                            "precision_label": "Exact time",
                            "display": "2019-08-23 07:00 UTC",
                            "weekday": "Friday",
                            "hour_utc": 7,
                            "timezone": "UTC",
                            "url": "https://music.apple.com/album/9",
                            "detail": "Album",
                            "sort_key": "2019-08-23T07:00:00",
                        }
                    ],
                    "notices": ["US storefront midnight."],
                }
            ],
            "patterns": {
                "hour_labels": [f"{hour:02d}" for hour in range(24)],
                "weekday_labels": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "release_hours": [
                    {"platform": "apple", "label": "Apple Music", "data": [0] * 7 + [1] + [0] * 16, "color": "#fb7185"}
                ],
                "social_hours": [],
                "weekdays": [
                    {"platform": "apple", "label": "Apple Music", "data": [0, 0, 0, 0, 1, 0, 0], "color": "#fb7185"}
                ],
            },
            "insights": ["Apple Music peaks at 07:00 UTC (1 of 1 exact timestamps)."],
        }

    monkeypatch.setattr(main, "collect_artist", fake_collect)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"})
    page = client.get("/dashboard", params={"artist": "Taylor Swift"})
    assert page.status_code == 200
    assert "2019-08-23 07:00 UTC" in page.text
    assert "Exact time" in page.text
    assert "releaseHourChart" in page.text
    assert "07:00 UTC" in page.text


def test_bad_login_and_short_password():
    client = TestClient(main.app)
    denied = client.post("/login", data={"email": "artist@demo.com", "password": "nope"})
    assert denied.status_code == 400
    assert "Invalid email or password" in denied.text
    short = client.post(
        "/register",
        data={"email": "new@example.com", "password": "123", "artist_name": "New"},
    )
    assert short.status_code == 400
    assert "at least 6" in short.text
    created = client.post(
        "/register",
        data={"email": "new@example.com", "password": "secret1", "artist_name": "New"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert "new@example.com" in main.USERS
