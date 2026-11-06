"""Momentum deltas from stored exact counts."""

from datetime import datetime, timedelta, timezone

from momentum import MOMENTUM_EMPTY, build_momentum, percent_text, window_delta


NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _at(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def test_percent_uses_exact_integers_and_skips_a_zero_baseline():
    assert percent_text(100, 1000) == "+10%"
    assert percent_text(1, 3) == "+33.3%"
    assert percent_text(-100, 1000) == "-10%"
    assert percent_text(0, 1000) == "0%"
    assert percent_text(5, 0) is None
    assert percent_text(5, -2) is None


def test_one_point_nulls_and_gaps():
    points = [
        {"at": _at(20), "value": 1000},
        {"at": _at(2), "value": 1100},
        {"at": _at(2), "value": None},
        {"at": _at(30), "value": 500},
    ]
    assert window_delta([{"at": _at(2), "value": 1100}], NOW, 24 * 3600) is None
    same_time = [
        {"at": _at(2), "value": 1000},
        {"at": _at(2), "value": 1100},
    ]
    assert window_delta(same_time, NOW, 24 * 3600) is None
    day = window_delta(points, NOW, 24 * 3600)
    assert day["change"] == 100
    assert day["span"] == "18h"
    assert day["percent"] == "+10%"
    assert day["earliest"] == 1000
    assert day["latest"] == 1100
    week = window_delta(points, NOW, 7 * 24 * 3600)
    assert week["earliest"] == 500
    assert week["latest"] == 1100
    assert week["change"] == 600
    outside = [{"at": _at(24 * 8), "value": 1}, {"at": _at(1), "value": 2}]
    assert window_delta(outside, NOW, 24 * 3600) is None
    assert window_delta(outside, NOW, 7 * 24 * 3600) is None


def test_build_momentum_omits_short_series_and_ignores_other_metrics():
    rows = [
        {"source": "spotify", "metric": "Followers", "value": 800, "captured_at": "2026-11-02T12:00:00Z"},
        {"source": "spotify", "metric": "Followers", "value": 1000, "captured_at": "2026-11-05T16:00:00Z"},
        {"source": "spotify", "metric": "Followers", "value": None, "captured_at": "2026-11-06T00:00:00Z"},
        {"source": "spotify", "metric": "Followers", "value": "1.2M", "captured_at": "2026-11-06T01:00:00Z"},
        {"source": "spotify", "metric": "Followers", "value": 1100, "captured_at": "2026-11-06T10:00:00Z"},
        {"source": "spotify", "metric": "Monthly listeners", "value": 5000, "captured_at": "2026-11-06T10:00:00Z"},
        {"source": "instagram", "metric": "Followers", "value": 10, "captured_at": "2026-11-03T12:00:00Z"},
        {"source": "instagram", "metric": "Followers", "value": 12, "captured_at": "2026-11-06T11:00:00Z"},
        {"source": "tiktok", "metric": "Likes", "value": 80, "captured_at": "2026-11-06T08:00:00Z"},
        {"source": "tiktok", "metric": "Likes", "value": 90, "captured_at": "2026-11-06T11:00:00Z"},
        {"source": "apple", "metric": "Albums returned", "value": 4, "captured_at": "2026-11-05T12:00:00Z"},
        {"source": "apple", "metric": "Albums returned", "value": 5, "captured_at": "2026-11-06T12:00:00Z"},
    ]
    series = {item["label"]: item for item in build_momentum(rows, NOW)}
    assert "Spotify monthly listeners" not in series
    assert "Apple" not in " ".join(series)
    followers = series["Spotify followers"]
    assert [window["name"] for window in followers["windows"]] == ["24h", "7d"]
    assert followers["windows"][0]["change"] == 100
    assert followers["windows"][0]["span"] == "18h"
    assert followers["windows"][1]["change"] == 300
    assert followers["windows"][1]["earliest"] == 800
    instagram = series["Instagram followers"]
    assert [window["name"] for window in instagram["windows"]] == ["7d"]
    assert instagram["windows"][0]["change"] == 2
    assert instagram["windows"][0]["percent"] == "+20%"
    likes = series["TikTok likes"]
    assert likes["windows"][0]["name"] == "24h"
    assert likes["windows"][0]["change"] == 10
    assert likes["bars"]
    assert MOMENTUM_EMPTY.startswith("Not enough stored readings")
