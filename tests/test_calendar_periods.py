"""Release-calendar years. A period never upgrades a stored date."""

import asyncio

from fastapi.testclient import TestClient

from analytics import (
    build_release_calendar,
    calendar_navigation,
    parse_calendar_period,
)
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import (
    CALENDAR_PERIOD_LIMIT,
    CALENDAR_YEAR_LIMIT,
    MB_CALENDAR_PERIOD_SQL,
    MB_CALENDAR_YEARS_SQL,
    SIGHTING_CALENDAR_DATES_SQL,
    SIGHTING_CALENDAR_PERIOD_SQL,
    SIGHTING_CALENDAR_YEARS_SQL,
    calendar_period_bounds,
    find_artist,
    load_release_calendar,
)

import main


def _row(title, source, published, precision, **extra):
    row = {"title": title, "source": source, "published_at": published, "precision": precision}
    row.update(extra)
    return row


def test_parse_calendar_period_reads_a_year_and_ignores_a_day():
    assert parse_calendar_period("2014", None) == 2014
    assert parse_calendar_period(None, "2006-06") == 2006
    assert parse_calendar_period("2021", "2006-06") == 2021
    assert parse_calendar_period("2006-06-16", None) is None
    assert parse_calendar_period(None, "2006-13") is None
    assert parse_calendar_period("nope", "2019-12") == 2019
    assert parse_calendar_period("", "") is None
    assert parse_calendar_period("999", None) is None
    assert calendar_period_bounds(2014) == ("2013-12", "2015-02")
    assert calendar_period_bounds(9999)[1] == ":"
    # '10000' sorts before '2000', so it cannot be the end of a text range.
    assert "1000" <= "2006-06" < ":"
    assert not ("2006-06" < "10000")


def test_year_filter_keeps_precision_and_leaves_other_years_out():
    rows = [
        _row("Sling", "musicbrainz", "2021-06-16", "day", released_at="2021-06-16", mbid="rg-sling"),
        _row("Sling", "apple", "2021-06-16T07:00:00Z", "time"),
        _row("Bag", "musicbrainz", "2006-06", "month", released_at="2006-06"),
        _row("Charm", "spotify", "2024-06-16", "year"),
        _row("New Year", "apple", "2019-12-31T20:00:00-08:00", "time"),
        _row("Post", "instagram", "2021-06-16T07:00:00Z", "time"),
    ]
    june = build_release_calendar(rows, year=2021)
    assert june["available"] is True
    assert june["empty_period"] is False
    assert june["period_year"] == 2021
    assert june["omitted_months"] == 0
    assert [month["key"] for month in june["months"]] == ["2021-06"]
    day = next(cell for cell in june["months"][0]["cells"] if cell["day"] == 16)
    apple = next(item for item in day["items"] if item["source"] == "apple")
    musicbrainz = next(item for item in day["items"] if item["source"] == "musicbrainz")
    assert apple["precision"] == "time"
    assert apple["clock"] == "07:00 UTC"
    assert musicbrainz["precision"] == "day"
    assert musicbrainz["clock"] == ""
    assert musicbrainz["mbid"] == "rg-sling"
    assert all(cell["day"] != 1 or not cell["items"] for cell in june["months"][0]["cells"])
    assert june["year_only"] == []
    assert "Bag" not in str(june)
    assert "Charm" not in str(june)
    assert "Post" not in str(june)

    bag = build_release_calendar(rows, year=2006)
    assert bag["months"][0]["key"] == "2006-06"
    assert bag["months"][0]["show_grid"] is False
    assert bag["months"][0]["cells"] == []
    assert bag["months"][0]["month_only"][0]["title"] == "Bag"
    assert bag["months"][0]["month_only"][0]["precision"] == "month"
    assert bag["day_count"] == 0

    charm = build_release_calendar(rows, year=2024)
    assert charm["months"] == []
    assert charm["year_only"][0]["title"] == "Charm"
    assert charm["year_only"][0]["precision"] == "year"
    assert charm["year_only"][0]["year"] == "2024"
    assert charm["day_count"] == 0

    crossed = build_release_calendar(rows, year=2020)
    assert crossed["months"][0]["key"] == "2020-01"
    new_year = next(cell for cell in crossed["months"][0]["cells"] if cell["day"] == 1)
    assert new_year["items"][0]["title"] == "New Year"
    assert new_year["items"][0]["clock"] == "04:00 UTC"
    assert new_year["items"][0]["precision"] == "time"
    assert build_release_calendar(rows, year=2019)["empty_period"] is True
    assert build_release_calendar(rows, year=2019)["months"] == []

    missing = build_release_calendar(rows, year=1990)
    assert missing["available"] is False
    assert missing["empty_period"] is True
    assert missing["months"] == []
    assert missing["year_only"] == []
    assert "1990" in missing["reason"]
    assert "not given a day" in missing["reason"]


def test_navigation_uses_only_years_that_have_releases():
    nav = calendar_navigation([2024, 2006, 2021, 2021, True, "2010"], 1990, 4)
    assert nav["years"] == [2024, 2021, 2006]
    assert nav["prev_year"] is None
    assert nav["next_year"] == 2006
    assert nav["next_href"] == "/artist/4/calendar?year=2006"
    assert [(decade["label"], [item["year"] for item in decade["years"]]) for decade in nav["decades"]] == [
        ("2020s", [2024, 2021]),
        ("2000s", [2006]),
    ]
    current = calendar_navigation([2024, 2021, 2006], 2021, 4)
    assert current["prev_year"] == 2006
    assert current["next_year"] == 2024
    assert current["prev_href"] == "/artist/4/calendar?year=2006"
    selected = next(item for decade in current["decades"] for item in decade["years"] if item["current"])
    assert selected["year"] == 2021
    assert calendar_navigation([], None, 4)["decades"] == []


def test_calendar_year_queries_stay_on_the_artist_index():
    assert "artist_id = ?" in MB_CALENDAR_YEARS_SQL
    assert "released_at >= ?" in MB_CALENDAR_PERIOD_SQL
    assert "LIMIT ?" in MB_CALENDAR_PERIOD_SQL
    assert "source = ?" in SIGHTING_CALENDAR_YEARS_SQL
    assert "published_at >= ?" in SIGHTING_CALENDAR_PERIOD_SQL
    assert "LIMIT ?" in SIGHTING_CALENDAR_YEARS_SQL
    database = SqliteDatabase(":memory:")

    async def plans():
        await migrate(database)
        period = await database.query(
            "EXPLAIN QUERY PLAN " + MB_CALENDAR_PERIOD_SQL,
            (1, "2013-12", "2015-02", CALENDAR_PERIOD_LIMIT),
        )
        years = await database.query(
            "EXPLAIN QUERY PLAN " + MB_CALENDAR_YEARS_SQL,
            (1, CALENDAR_YEAR_LIMIT),
        )
        sightings = await database.query(
            "EXPLAIN QUERY PLAN " + SIGHTING_CALENDAR_PERIOD_SQL,
            (1, "apple", "2013-12", "2015-02", CALENDAR_PERIOD_LIMIT),
        )
        dates = await database.query(
            "EXPLAIN QUERY PLAN " + SIGHTING_CALENDAR_DATES_SQL,
            (1, "apple", 500),
        )
        return period, years, sightings, dates

    period, years, sightings, dates = asyncio.run(plans())
    assert "idx_mb_releases_artist" in " ".join(str(row) for row in period)
    assert "idx_mb_releases_artist" in " ".join(str(row) for row in years)
    assert "sqlite_autoindex_content_sightings" in " ".join(str(row) for row in sightings)
    assert "sqlite_autoindex_content_sightings" in " ".join(str(row) for row in dates)


def test_stored_calendar_pages_one_year_and_an_empty_year_stays_empty(monkeypatch, tmp_path):
    path = tmp_path / "calendar-years.sqlite"
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
        releases = [
            ("rg-bag", "Bag", "Single", "2006-06", "month"),
            ("rg-sling", "Sling", "Album", "2021-06-16", "day"),
            ("rg-early", "Early Album", "Album", "2000-03-02", "day"),
        ]
        for mbid, title, kind, released, precision in releases:
            await database.execute(
                """
                INSERT INTO musicbrainz_releases (
                    artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
                """,
                (artist_id, mbid, title, kind, released, precision),
            )
        for year in range(2001, 2012):
            await database.execute(
                """
                INSERT INTO musicbrainz_releases (
                    artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, 'Album', ?, 'day', '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
                """,
                (artist_id, f"rg-{year}", f"Album {year}", f"{year}-06-02"),
            )
        sightings = [
            ("apple", "apple-sling", "Sling", "2021-06-16T07:00:00Z", "time"),
            ("spotify", "charm", "Charm", "2024", "year"),
            ("apple", "new-year", "New Year", "2019-12-31T20:00:00-08:00", "time"),
            ("instagram", "post-1", "Post", "2026-11-05T23:43:44Z", "time"),
            ("instagram", "post-2", "Post", "2026-11-05T23:10:00Z", "time"),
            ("tiktok", "post-3", "Clip", "2026-11-04T00:05:00Z", "time"),
        ]
        for source, external, title, published, precision in sightings:
            await database.execute(
                """
                INSERT INTO content_sightings
                    (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
                VALUES (?, ?, ?, ?, 'release', ?, ?, '2026-11-06T18:00:00Z')
                """,
                (artist_id, source, external, title, published, precision),
            )
        loaded = await load_release_calendar(database, artist_id, 2006)
        return artist_id, loaded

    artist_id, loaded = asyncio.run(seed())
    assert loaded["period_year"] == 2006
    june = loaded["months"][0]
    assert june["key"] == "2006-06"
    assert any(item["title"] == "Bag" and item["precision"] == "month" for item in june["month_only"])
    assert all(item["title"] != "Bag" for cell in june["cells"] for item in cell["items"])
    assert loaded["navigation"]["prev_year"] == 2005
    assert loaded["navigation"]["next_year"] == 2007
    assert 2000 in loaded["navigation"]["years"]
    assert 2020 in loaded["navigation"]["years"]
    assert 2019 not in loaded["navigation"]["years"]
    assert 2024 in loaded["navigation"]["years"]
    assert loaded["navigation"]["decades"][0]["label"] == "2020s"

    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    default = client.get(f"/artist/{artist_id}/calendar")
    older = client.get(f"/artist/{artist_id}/calendar?year=2006")
    crossed = client.get(f"/artist/{artist_id}/calendar?year=2020")
    empty = client.get(f"/artist/{artist_id}/calendar?year=1990")
    by_from = client.get(f"/artist/{artist_id}/calendar?from=2006-06")
    day_ignored = client.get(f"/artist/{artist_id}/calendar?from=2006-06-16")
    named = client.get("/artist/Clairo/calendar?year=2006", follow_redirects=False)
    artist = client.get(f"/artist/{artist_id}")

    assert default.status_code == 200
    assert "Charm" in default.text
    assert "Year only" in default.text
    assert "Bag" not in default.text
    assert "Album 2000" not in default.text
    assert "older months with a stored release are not shown" not in default.text
    assert f'href="/artist/{artist_id}/calendar?year=2006"' in default.text
    assert f'href="/artist/{artist_id}/calendar?year=2020"' in default.text
    assert "2020s" in default.text
    assert "2000s" in default.text
    assert 'aria-current="page"' in default.text
    assert "23:00 UTC · 2" in default.text

    assert "Bag" in older.text
    assert "Month only, no day published" in older.text
    assert "Sling" not in older.text
    assert "Charm" not in older.text
    assert "June 2006" in older.text
    assert f'href="/artist/{artist_id}/calendar?year=2005"' in older.text
    assert f'href="/artist/{artist_id}/calendar?year=2007"' in older.text

    assert "New Year" in crossed.text
    assert "04:00 UTC" in crossed.text
    assert "January 2020" in crossed.text
    assert "December 2019" not in crossed.text

    assert empty.status_code == 200
    assert "No stored MusicBrainz, Apple, Spotify, or Deezer release falls in 1990" in empty.text
    assert "Bag" not in empty.text
    assert "Sling" not in empty.text
    assert "New Year" not in empty.text
    assert f'href="/artist/{artist_id}/calendar?year=2000"' in empty.text
    assert "←" not in empty.text
    assert "2000 →" in empty.text

    assert "Bag" in by_from.text
    assert "Sling" not in by_from.text
    assert "Charm" in day_ignored.text
    assert "Bag" not in day_ignored.text

    assert named.status_code == 303
    assert named.headers["location"] == f"/artist/{artist_id}/calendar?year=2006"

    assert artist.status_code == 200
    assert f'href="/artist/{artist_id}/calendar"' in artist.text
    assert "data-scroll" in artist.text
    assert "Swipe sideways to see every column." in artist.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
