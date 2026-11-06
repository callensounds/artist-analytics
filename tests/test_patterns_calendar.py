"""UTC posting hours and the release calendar. Partial dates are not given a clock or a day."""

import asyncio

from fastapi.testclient import TestClient

from analytics import (
    CALENDAR_EMPTY,
    POSTING_NONE,
    build_posting_patterns,
    build_release_calendar,
)
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import EXACT_TIME_SQL, RELEASE_SIGHTING_SQL, find_artist, load_history_for_artist

import main


def test_exact_time_sql_reads_source_clock_not_first_seen():
    assert "precision = 'time'" in EXACT_TIME_SQL
    assert "published_at" in EXACT_TIME_SQL
    assert "first_seen_at" not in EXACT_TIME_SQL
    assert "apple" in RELEASE_SIGHTING_SQL
    assert "spotify" in RELEASE_SIGHTING_SQL
    assert "first_seen_at" not in RELEASE_SIGHTING_SQL


def test_posting_buckets_are_utc_and_skip_partial_dates():
    rows = [
        {"source": "instagram", "published_at": "2026-11-05T23:43:44", "precision": "time"},
        {"source": "instagram", "published_at": "2026-11-05T23:10:00Z", "precision": "time"},
        {"source": "tiktok", "published_at": "2026-11-04T00:05:00Z", "precision": "time"},
        {"source": "apple", "published_at": "2019-08-23T07:00:00Z", "precision": "time"},
        {"source": "youtube", "published_at": "2026-11-02T15:00:00Z", "precision": "time"},
        {"source": "instagram", "published_at": "2026-11-01", "precision": "day"},
        {"source": "apple", "published_at": "2020-06", "precision": "month"},
        {"source": "apple", "published_at": "2014", "precision": "year"},
        {"source": "instagram", "published_at": "2024-06-16", "precision": "time"},
        {"source": "spotify", "published_at": "2026-11-05T23:43:44Z", "precision": "time"},
        {"source": "musicbrainz", "published_at": "2021-06-16", "precision": "day"},
        {"source": "youtube", "published_at": "not-a-time", "precision": "time"},
    ]
    patterns = build_posting_patterns(rows)
    assert patterns["available"] is True
    assert patterns["timezone"] == "UTC"
    assert patterns["exact_count"] == 5
    assert patterns["excluded_count"] == 5
    assert patterns["hours"][23] == 2
    assert patterns["hours"][0] == 1
    assert patterns["hours"][7] == 1
    assert patterns["hours"][15] == 1
    assert sum(patterns["hours"]) == 5
    assert patterns["weekdays"][0] == 1  # Monday 2026-11-02
    assert patterns["weekdays"][3] == 2  # Thursday 2026-11-05
    assert patterns["weekdays"][2] == 1  # Wednesday 2026-11-04
    assert patterns["weekdays"][4] == 1  # Friday 2019-08-23
    assert patterns["nonzero_hours"][0]["label"].endswith("UTC")
    labels = {row["source"]: row["count"] for row in patterns["sources"]}
    assert labels == {"instagram": 2, "tiktok": 1, "youtube": 1, "apple": 1}
    assert "spotify" not in labels
    assert "23:00 UTC" in patterns["summary"]
    assert all(bar["count"] == patterns["hours"][index] for index, bar in enumerate(patterns["hour_bars"]))


def test_offset_timestamp_uses_the_utc_hour_and_weekday():
    patterns = build_posting_patterns(
        [
            {"source": "instagram", "published_at": "2026-11-05T20:00:00-08:00", "precision": "time"},
            {"source": "instagram", "published_at": "2026-11-06T04:00:00Z", "precision": "time"},
            {"source": "tiktok", "published_at": "2026-11-06T04:30:00Z", "precision": "time"},
        ]
    )
    assert patterns["available"] is True
    assert patterns["hours"][4] == 3
    assert patterns["hours"][20] == 0
    assert patterns["weekdays"][4] == 3  # Friday 2026-11-06 UTC
    assert patterns["weekdays"][3] == 0


def test_fewer_than_three_exact_times_stays_empty():
    patterns = build_posting_patterns(
        [
            {"source": "instagram", "published_at": "2026-11-05T23:43:44Z", "precision": "time"},
            {"source": "tiktok", "published_at": "2026-11-05T01:00:00Z", "precision": "time"},
            {"source": "apple", "published_at": "2014", "precision": "year"},
        ]
    )
    assert patterns["available"] is False
    assert patterns["exact_count"] == 2
    assert patterns["excluded_count"] == 1
    assert patterns["hours"] == [0] * 24
    assert patterns["weekdays"] == [0] * 7
    assert patterns["nonzero_hours"] == []
    assert patterns["sources"] == []
    assert patterns["summary"] == ""
    assert "at least 3" in patterns["reason"]
    assert "excluded" in patterns["reason"]
    assert "not filled" in patterns["reason"]

    none = build_posting_patterns([])
    assert none["available"] is False
    assert none["exact_count"] == 0
    assert none["hours"] == [0] * 24
    assert none["reason"] == POSTING_NONE


def test_release_calendar_places_days_and_keeps_partial_dates_off_the_cells():
    calendar = build_release_calendar(
        [
            {
                "title": "Sling",
                "source": "musicbrainz",
                "released_at": "2021-06-16",
                "precision": "day",
                "kind": "Album",
                "mbid": "rg-sling",
            },
            {
                "title": "Immunity",
                "source": "musicbrainz",
                "released_at": "2018-08",
                "precision": "month",
                "kind": "Album",
            },
            {
                "title": "Diary 001",
                "source": "musicbrainz",
                "released_at": "2018",
                "precision": "year",
            },
            {
                "title": "Charm",
                "source": "spotify",
                "published_at": "2024-06-16",
                "precision": "year",
            },
            {
                "title": "Sling",
                "source": "apple",
                "published_at": "2021-06-16T07:00:00Z",
                "precision": "time",
                "kind": "release",
            },
            {
                "title": "Late",
                "source": "apple",
                "published_at": "2026-11-05T20:00:00-08:00",
                "precision": "time",
            },
            {
                "title": "Day clock",
                "source": "apple",
                "published_at": "2021-06-16T07:00:00Z",
                "precision": "day",
            },
            {
                "title": "Bad day",
                "source": "musicbrainz",
                "released_at": "2021-02-31",
                "precision": "day",
            },
            {
                "title": "Sling",
                "source": "musicbrainz",
                "released_at": "2021-06-16",
                "precision": "day",
                "mbid": "rg-sling",
            },
            {"title": "Post", "source": "instagram", "published_at": "2026-11-05T23:43:44Z", "precision": "time"},
        ]
    )
    assert calendar["available"] is True
    assert calendar["day_count"] == 4
    assert calendar["month_count"] == 1
    by_month = {month["key"]: month for month in calendar["months"]}
    june = by_month["2021-06"]
    assert june["show_grid"] is True
    day_16 = next(cell for cell in june["cells"] if cell["day"] == 16)
    titles = [item["title"] for item in day_16["items"]]
    assert titles == ["Day clock", "Sling", "Sling"]
    apple = next(item for item in day_16["items"] if item["source"] == "apple" and item["title"] == "Sling")
    assert apple["clock"] == "07:00 UTC"
    assert apple["precision"] == "time"
    mb = next(item for item in day_16["items"] if item["source"] == "musicbrainz")
    assert mb["clock"] == ""
    assert mb["mbid"] == "rg-sling"
    assert mb["precision"] == "day"
    day_clock = next(item for item in day_16["items"] if item["title"] == "Day clock")
    assert day_clock["clock"] == ""
    assert day_clock["precision"] == "day"
    assert all(cell["day"] != 1 or not cell["items"] for cell in june["cells"])
    august = by_month["2018-08"]
    assert august["show_grid"] is False
    assert august["cells"] == []
    assert [item["title"] for item in august["month_only"]] == ["Immunity"]
    assert "2018-06" not in by_month
    november = by_month["2026-11"]
    day_6 = next(cell for cell in november["cells"] if cell["day"] == 6)
    day_5 = next(cell for cell in november["cells"] if cell["day"] == 5)
    assert [item["title"] for item in day_6["items"]] == ["Late"]
    assert day_6["items"][0]["clock"] == "04:00 UTC"
    assert day_5["items"] == []
    years = [(item["year"], item["title"], item["source"]) for item in calendar["year_only"]]
    assert ("2024", "Charm", "spotify") in years
    assert ("2018", "Diary 001", "musicbrainz") in years
    assert all(item["title"] != "Charm" for month in calendar["months"] for cell in month["cells"] for item in cell["items"])
    assert all(item["title"] != "Diary 001" for month in calendar["months"] for cell in month["cells"] for item in cell["items"])
    assert "Post" not in str(calendar["months"])


def test_month_only_does_not_invent_the_first_of_the_month():
    calendar = build_release_calendar(
        [{"title": "Bag", "source": "musicbrainz", "released_at": "2006-06", "precision": "month"}]
    )
    month = calendar["months"][0]
    assert month["key"] == "2006-06"
    assert month["show_grid"] is False
    assert month["month_only"][0]["title"] == "Bag"
    assert calendar["year_only"] == []


def test_empty_calendar_has_no_days():
    calendar = build_release_calendar([])
    assert calendar["available"] is False
    assert calendar["months"] == []
    assert calendar["year_only"] == []
    assert calendar["day_count"] == 0
    assert calendar["reason"] == CALENDAR_EMPTY


def test_calendar_caps_months_without_dropping_year_only_rows():
    rows = [
        {"title": f"Album {month}", "source": "apple", "published_at": f"20{year:02d}-{month:02d}-02", "precision": "day"}
        for year in range(10, 16)
        for month in (1, 6)
    ]
    rows.append({"title": "Early", "source": "spotify", "published_at": "2001", "precision": "year"})
    calendar = build_release_calendar(rows, month_cap=4)
    assert len(calendar["months"]) == 4
    assert calendar["months"][0]["key"] == "2015-06"
    assert calendar["omitted_months"] == 8
    assert calendar["year_only"][0]["title"] == "Early"
    assert calendar["year_only"][0]["year"] == "2001"


def test_stored_rows_feed_the_artist_patterns_and_an_empty_artist_stays_blank():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Nobody', 'nobody', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        clairo = await find_artist(database, "Clairo")
        artist_id = int(clairo["id"])
        posts = [
            ("instagram", "post-1", "Story", "2026-11-05T23:43:44", "time"),
            ("instagram", "post-2", "Story", "2026-11-05T23:10:00Z", "time"),
            ("tiktok", "post-3", "Clip", "2026-11-04T00:05:00Z", "time"),
            ("apple", "apple-sling", "Sling", "2021-06-16T07:00:00Z", "time"),
            ("spotify", "charm", "Charm", "2024", "year"),
            ("instagram", "day-post", "Day", "2026-11-01", "day"),
        ]
        for source, external, title, published, precision in posts:
            await database.execute(
                """
                INSERT INTO content_sightings
                    (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
                VALUES (?, ?, ?, ?, 'release', ?, ?, '2026-11-06T12:00:00Z')
                """,
                (artist_id, source, external, title, published, precision),
            )
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES (?, 'rg-sling', 'Sling', 'Album', '2021-06-16', 'day', '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z'),
                     (?, 'rg-bag', 'Bag', 'Single', '2006-06', 'month', '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
            """,
            (artist_id, artist_id),
        )
        filled = await load_history_for_artist(database, clairo)
        empty = await load_history_for_artist(database, await find_artist(database, "Nobody"))
        return filled, empty

    filled, empty = asyncio.run(seed())
    assert filled["posting"]["available"] is True
    assert filled["posting"]["exact_count"] == 4
    assert filled["posting"]["hours"][23] == 2
    assert filled["posting"]["hours"][7] == 1
    assert filled["posting"]["hours"][12] == 0
    june = next(month for month in filled["calendar"]["months"] if month["key"] == "2021-06")
    day = next(cell for cell in june["cells"] if cell["day"] == 16)
    assert {item["source"] for item in day["items"]} == {"musicbrainz", "apple"}
    assert next(item["clock"] for item in day["items"] if item["source"] == "apple") == "07:00 UTC"
    assert any(item["title"] == "Charm" for item in filled["calendar"]["year_only"])
    bag = next(month for month in filled["calendar"]["months"] if month["key"] == "2006-06")
    assert bag["show_grid"] is False
    assert bag["month_only"][0]["title"] == "Bag"
    assert empty["posting"]["available"] is False
    assert empty["posting"]["hours"] == [0] * 24
    assert empty["posting"]["reason"] == POSTING_NONE
    assert empty["calendar"]["available"] is False
    assert empty["calendar"]["months"] == []
    shown = main.templates.get_template("history_cards.html").render(history=filled)
    assert "Posting times" in shown
    assert "23:00 UTC · 2" in shown
    assert "07:00 UTC" in shown
    assert "Year, month, and day-only rows are excluded" in shown
    assert "Charm" in shown
    assert "Month only, no day published" in shown
    assert "Bag" in shown
    blank = main.templates.get_template("history_cards.html").render(history=empty)
    assert POSTING_NONE in blank
    assert CALENDAR_EMPTY in blank
    assert "23:00 UTC" not in blank
    assert "Bag" not in blank


def test_artist_calendar_page_reads_stored_rows(monkeypatch, tmp_path):
    path = tmp_path / "calendar.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-11-01T00:00:00Z')"
        )
        clairo = await find_artist(database, "Clairo")
        artist_id = int(clairo["id"])
        for external, published in (
            ("a", "2026-11-05T23:43:44Z"),
            ("b", "2026-11-05T23:10:00Z"),
            ("c", "2026-11-04T00:05:00Z"),
        ):
            await database.execute(
                """
                INSERT INTO content_sightings
                    (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
                VALUES (?, 'instagram', ?, 'Post', 'post', ?, 'time', '2026-11-06T18:00:00Z')
                """,
                (artist_id, external, published),
            )
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES (?, 'rg-sling', 'Sling', 'Album', '2021-06-16', 'day', '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
            """,
            (artist_id,),
        )
        return artist_id

    artist_id = asyncio.run(seed())
    client = TestClient(main.app)
    logged_out = client.get(f"/artist/{artist_id}/calendar", follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/login"
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get(f"/artist/{artist_id}/calendar")
    artist = client.get(f"/artist/{artist_id}")
    named = client.get("/artist/Clairo/calendar", follow_redirects=False)
    assert page.status_code == 200
    assert "23:00 UTC · 2" in page.text
    assert "Sling" in page.text
    assert "Release calendar" in page.text
    assert "Year, month, and day-only rows are excluded" in page.text
    assert artist.status_code == 200
    assert f"/artist/{artist_id}/calendar" in artist.text
    assert "23:00 UTC · 2" in artist.text
    assert "Sling" in artist.text
    assert named.status_code == 303
    assert named.headers["location"] == f"/artist/{artist_id}/calendar"
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
