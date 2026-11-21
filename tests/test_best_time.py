"""Best posting and release clocks from rows the page already loaded. No new D1 reads."""

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from best_time import BASIS, SLOT_MIN_EXACT, best_time, export_fields
from crawl import reset_pool
from db import SqliteDatabase, migrate
from export_csv import render_export_csv
from history import empty_history
from timing import HOUR_NOTE, MODAL_MIN_EXACT, timing_csv_columns
from timezones import shift


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _posts(moments, source="instagram", **extra):
    rows = []
    for index, moment in enumerate(moments):
        row = {
            "source": source,
            "title": f"Post {index}",
            "kind": "post",
            "published_at": _stamp(moment),
            "precision": "time",
        }
        row.update(extra)
        rows.append(row)
    return rows


def _weeks(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(weeks=index) for index in range(count)]


def _history(posts=None, releases=None, stamps=None):
    history = {"sightings": posts or [], "releases": releases or []}
    if stamps is not None:
        history["posting"] = {"exact_stamps": stamps}
    return history


def test_timing_exports_keep_the_local_modal_columns():
    columns = timing_csv_columns()
    assert "modal_hour_local" in columns
    assert "modal_weekday_local" in columns
    assert "latest_exact_local" in columns


def test_insufficient_sample_has_no_slot():
    start = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)
    two = best_time(_history(_posts(_weeks(start, 2))), "America/New_York")
    posts = two["posts"]
    assert posts["status"] == "insufficient"
    assert posts["n"] == 2
    assert posts["slot"] is None
    assert posts["hour"] is None
    assert posts["engagement"] is None
    assert posts["message"] == "Not enough exact timestamps (n=2) to suggest a time."

    four = best_time(_history(_posts(_weeks(start, 4))), "UTC")
    assert four["posts"]["status"] == "insufficient"
    assert four["posts"]["n"] == 4
    assert four["posts"]["slot"] is None
    assert four["posts"]["hour"]["local_hour"] == 23
    assert four["posts"]["hour"]["utc_hour"] == 23
    assert "weekday and hour" in four["posts"]["message"]
    assert MODAL_MIN_EXACT == 3
    assert SLOT_MIN_EXACT == 5

    three = best_time(_history(_posts(_weeks(start, 3))), "UTC")
    assert three["posts"]["hour"] is not None
    assert three["posts"]["slot"] is None


def test_date_only_releases_are_excluded_and_not_given_an_hour():
    rows = [
        {"title": "Day", "released_at": "2021-06-16", "precision": "day"},
        {"title": "Month", "released_at": "2021-06", "precision": "month"},
        {"title": "Year", "released_at": "2021", "precision": "year"},
        {"title": "Clock hidden by precision", "released_at": "2026-07-15T23:30:00Z", "precision": "day"},
        {"title": "Time label on a date", "released_at": "2024-01-02", "precision": "time"},
        {"title": "Exact", "released_at": "2026-03-06T16:00:00Z", "precision": "time"},
    ]
    releases = best_time(_history(releases=rows), "America/New_York")["releases"]
    assert releases["n"] == 1
    assert releases["excluded"] == 5
    assert releases["status"] == "insufficient"
    assert releases["slot"] is None
    assert releases["hour"] is None
    assert "n=1" in releases["message"]


def test_dst_shifts_each_event_and_does_not_use_one_offset():
    july = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)
    january = datetime(2026, 1, 14, 23, 30, tzinfo=timezone.utc)
    assert shift(july, "America/New_York").hour == 19
    assert shift(january, "America/New_York").hour == 18

    summer = best_time(_history(_posts(_weeks(july, 5))), "America/New_York")
    summer_posts = summer["posts"]
    assert summer_posts["status"] == "ok"
    assert summer_posts["n"] == 5
    assert summer_posts["slot"]["local_hour"] == 19
    assert summer_posts["slot"]["utc_hour"] == 23
    assert summer_posts["hour"]["local_hour"] == 19
    assert summer_posts["hour"]["utc_hour"] == 23
    assert "19:00-20:00 America/New_York = 23:00-24:00 UTC" in summer_posts["hour"]["display"]
    assert "5 of 5 exact posts" in summer_posts["slot"]["display"]
    assert summer_posts["slot"]["tied"] is False
    assert "America/New_York, DST-aware" == summer["timezone_label"]
    assert HOUR_NOTE in summer["hour_note"]
    assert "not on engagement" in summer["basis"]

    winter = best_time(_history(_posts(_weeks(january, 5))), "America/New_York")
    assert winter["posts"]["hour"]["local_hour"] == 18
    assert winter["posts"]["hour"]["utc_hour"] == 23
    assert "18:00-19:00" in winter["posts"]["hour"]["display"]

    mixed = best_time(
        _history(_posts(_weeks(july, 4) + _weeks(january, 4))),
        "America/New_York",
    )
    mixed_hour = mixed["posts"]["hour"]
    assert mixed["posts"]["n"] == 8
    assert mixed_hour["utc_hour"] == 23
    assert mixed_hour["utc_count"] == 8
    assert mixed_hour["local_hour"] == 18
    assert mixed_hour["count"] == 4
    assert mixed_hour["tied"] is True
    assert "tied" in mixed_hour["display"]
    assert "=" not in mixed_hour["display"]


def test_utc_viewer_does_not_invent_a_local_offset():
    start = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)
    result = best_time(_history(_posts(_weeks(start, 5))), "UTC")
    slot = result["posts"]["slot"]
    assert result["timezone"] == "UTC"
    assert slot["local_hour"] == 23
    assert slot["utc_hour"] == 23
    assert slot["local_label"] == slot["utc_label"]
    assert "America/New_York" not in slot["display"]
    assert "23:00-24:00 UTC" in slot["display"]
    assert best_time(_history(_posts(_weeks(start, 5))), "not-a-zone")["timezone"] == "UTC"


def test_ties_pick_the_earliest_hour_and_say_so():
    monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
    early = _weeks(monday.replace(hour=2), 3)
    late = _weeks(monday.replace(hour=5), 3)
    result = best_time(_history(_posts(early + late)), "UTC")
    slot = result["posts"]["slot"]
    hour = result["posts"]["hour"]
    assert result["posts"]["n"] == 6
    assert slot["local_hour"] == 2
    assert slot["utc_hour"] == 2
    assert slot["local_weekday"] == "Monday"
    assert slot["tied"] is True
    assert hour["tied"] is True
    assert "tied" in slot["display"]
    assert "02:00-03:00" in slot["display"]
    assert "05:00-06:00" not in slot["local_label"]


def test_posts_ignore_apple_and_reuse_exact_stamps_without_double_counting():
    start = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)
    moments = _weeks(start, 5)
    posts = _posts(moments)
    apple = _posts(moments, source="apple")
    day = {
        "source": "instagram",
        "title": "Date only",
        "published_at": "2026-07-01",
        "precision": "day",
    }
    stamps = [{"source": "instagram", "published_at": _stamp(moment)} for moment in moments]
    stamps.append({"source": "apple", "published_at": _stamp(start)})
    result = best_time(_history(posts + apple + [day], stamps=stamps), "UTC")
    assert result["posts"]["n"] == 5
    assert result["posts"]["excluded"] == 1
    assert result["posts"]["engagement"] is None

    from_stamps = best_time(_history(stamps=stamps), "UTC")
    assert from_stamps["posts"]["n"] == 5
    apple_only = best_time(_history(apple), "UTC")
    assert apple_only["posts"]["n"] == 0
    assert apple_only["posts"]["excluded"] == 0


def test_engagement_median_uses_only_stored_numbers_in_the_winning_hour():
    hour_two = _weeks(datetime(2026, 1, 5, 2, tzinfo=timezone.utc), 4)
    hour_five = _weeks(datetime(2026, 1, 5, 5, tzinfo=timezone.utc), 3)
    rows = _posts(hour_two)
    for row, views in zip(rows, (10, 40, 20, 30)):
        row["views"] = views
    hotter = _posts(hour_five)
    for row in hotter:
        row["views"] = 100
    result = best_time(_history(rows + hotter), "UTC")
    assert result["posts"]["hour"]["local_hour"] == 2
    engagement = result["posts"]["engagement"]
    assert engagement["metric"] == "views"
    assert engagement["n"] == 4
    assert engagement["median"] == 25
    assert "Median views in 02:00-03:00 UTC: 25, from 4 posts in that hour." == engagement["text"]

    strings = _posts(_weeks(datetime(2026, 1, 5, 2, tzinfo=timezone.utc), 4))
    for row in strings:
        row["views"] = "1.2K"
    assert best_time(_history(strings), "UTC")["posts"]["engagement"] is None

    few = _posts(_weeks(datetime(2026, 1, 5, 2, tzinfo=timezone.utc), 5))
    few[0]["likes"] = 4
    few[1]["likes"] = 8
    assert best_time(_history(few), "UTC")["posts"]["engagement"] is None

    liked = _posts(_weeks(datetime(2026, 1, 5, 2, tzinfo=timezone.utc), 3))
    for row, likes in zip(liked, (5, 9, 7)):
        row["likes"] = likes
    likes = best_time(_history(liked), "UTC")["posts"]["engagement"]
    assert likes["metric"] == "likes"
    assert likes["median"] == 7


def test_empty_and_non_dict_history_stay_insufficient():
    blank = best_time(empty_history("Data temporarily unavailable. Stored history could not be read on this request."))
    assert blank["posts"]["status"] == "insufficient"
    assert blank["posts"]["slot"] is None
    assert blank["releases"]["slot"] is None
    assert blank["posts"]["message"] == "Not enough exact timestamps (n=0) to suggest a time."
    assert best_time(None)["posts"]["n"] == 0
    assert best_time([])["releases"]["excluded"] == 0
    assert BASIS.startswith("Based on when this artist has historically posted or released")


def test_csv_columns_come_from_the_rows_already_passed_in():
    start = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)
    sightings = _posts(_weeks(start, 6))
    releases = [
        {"title": "Exact", "released_at": "2026-03-06T16:00:00Z", "precision": "time"},
        {"title": "Exact 2", "released_at": "2026-03-13T16:00:00Z", "precision": "time"},
        {"title": "Day", "released_at": "2021-06-16", "precision": "day"},
    ]
    fields = export_fields({"sightings": sightings, "releases": releases}, "America/New_York")
    assert "19:00-20:00" in fields["best_post_slot_local"]
    assert "23:00-24:00" in fields["best_post_slot_utc"]
    assert fields["best_post_n"] == "6"
    assert fields["best_release_slot_local"] == ""
    assert fields["best_release_slot_utc"] == ""
    assert fields["best_release_n"] == "2"
    text = render_export_csv([], sightings, [], releases, tz="America/New_York")
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["best_post_n"] == "6"
    assert parsed[0]["best_release_n"] == "2"
    assert "19:00-20:00" in parsed[0]["best_post_slot_local"]
    assert "best_post_slot_utc" in parsed[0]


def _card(page: str) -> str:
    return page.split('id="best-time"', 1)[1].split("</section>", 1)[0]


def test_artist_page_renders_the_card_without_extra_d1_statements(monkeypatch, tmp_path):
    path = tmp_path / "best-time.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    july = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Ada', 'ada', 'search', 1, '2026-07-01T00:00:00Z')
            """
        )
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Bea', 'bea', 'search', 1, '2026-07-01T00:00:00Z')
            """
        )
        ada = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'ada'"))["id"])
        bea = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'bea'"))["id"])
        for index, moment in enumerate(_weeks(july, 6)):
            await database.execute(
                """
                INSERT INTO content_sightings (
                    artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
                ) VALUES (?, 'instagram', ?, 'Clip', 'post', ?, 'time', '2026-07-01T00:00:00Z')
                """,
                (ada, f"ig-{index}", _stamp(moment)),
            )
        await database.execute(
            """
            INSERT INTO content_sightings (
                artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
            ) VALUES
                (?, 'instagram', 'day-1', 'Date only', 'post', '2026-06-01', 'day', '2026-07-01T00:00:00Z'),
                (?, 'instagram', 'day-2', 'Month only', 'post', '2026-05', 'month', '2026-07-01T00:00:00Z'),
                (?, 'apple', 'apple-1', 'Midnight', 'release', '2026-07-15T07:00:00Z', 'time', '2026-07-01T00:00:00Z')
            """,
            (ada, ada, ada),
        )
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES
                (?, 'mb-1', 'Exact one', 'Single', '2026-03-06T16:00:00Z', 'time', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z'),
                (?, 'mb-2', 'Exact two', 'Single', '2026-03-13T16:00:00Z', 'time', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z'),
                (?, 'mb-3', 'Day', 'Album', '2021-06-16', 'day', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')
            """,
            (ada, ada, ada),
        )
        return ada, bea

    ada, bea = asyncio.run(seed())
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303

    statements = []
    batches = []
    writes = []
    original_query = SqliteDatabase.query
    original_batch = SqliteDatabase.query_batch
    original_execute = SqliteDatabase.execute
    original_write_batch = SqliteDatabase.batch

    async def spy_query(self, sql, params=()):
        head = sql.lstrip().split(None, 1)[0].lower()
        if head in {"insert", "update", "delete", "replace"}:
            writes.append(sql)
        else:
            statements.append(sql)
        return await original_query(self, sql, params)

    async def spy_query_batch(self, items):
        batches.append(len(items))
        return [await original_query(self, sql, params) for sql, params in items]

    async def spy_execute(self, sql, params=()):
        writes.append(sql)
        return await original_execute(self, sql, params)

    async def spy_write_batch(self, items):
        for sql, _params in items:
            writes.append(sql)
        return await original_write_batch(self, items)

    monkeypatch.setattr(SqliteDatabase, "query", spy_query)
    monkeypatch.setattr(SqliteDatabase, "query_batch", spy_query_batch)
    monkeypatch.setattr(SqliteDatabase, "execute", spy_execute)
    monkeypatch.setattr(SqliteDatabase, "batch", spy_write_batch)

    empty = client.get(f"/artist/{bea}")
    assert empty.status_code == 200
    empty_card = _card(empty.text)
    assert "Best time to post / release" in empty.text
    assert "Not enough exact timestamps (n=0) to suggest a time." in empty_card
    assert "Posting times" in empty.text
    assert len(statements) == 3, statements
    assert batches == [17], batches
    assert writes == []

    plain = client.get(f"/artist/{ada}")
    assert plain.status_code == 200
    assert "could not be read" not in plain.text
    plain_card = _card(plain.text)
    assert "Best time to post / release" in plain.text
    assert "not on engagement" in plain_card
    assert "23:00-24:00 UTC" in plain_card
    assert "19:00-20:00" not in plain_card
    assert "America/New_York" not in plain_card
    assert "6 of 6 exact posts" in plain_card
    assert "2 without an exact time" in plain_card
    assert "Not enough exact timestamps (n=2)" in plain_card
    assert "Median" not in plain_card
    assert "Posting times" in plain.text
    assert "Audience growth" in plain.text
    posts = plain_card.split('id="best-time-posts"', 1)[1].split('id="best-time-releases"', 1)[0]
    assert "Most common weekday and hour" in posts
    assert len(statements) == 6, statements
    assert batches == [17, 17], batches
    assert writes == []

    zoned = client.get(f"/artist/{ada}?tz=America/New_York")
    assert zoned.status_code == 200
    zoned_card = _card(zoned.text)
    assert "19:00-20:00 America/New_York = 23:00-24:00 UTC" in zoned_card
    assert "America/New_York, DST-aware" in zoned_card
    assert "6 of 6 exact posts" in zoned_card
    assert "Not enough exact timestamps (n=2)" in zoned_card
    assert len(statements) == 9, statements
    assert batches == [17, 17, 17], batches
    assert writes == []
    assert all("best_post" not in sql and "best_release" not in sql for sql in statements)

    downloaded = client.get(f"/artist/{ada}/export.csv?tz=America/New_York")
    assert downloaded.status_code == 200
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    assert "best_post_slot_local" in parsed[0]
    assert "19:00-20:00" in parsed[0]["best_post_slot_local"]
    assert "23:00-24:00" in parsed[0]["best_post_slot_utc"]
    assert parsed[0]["best_post_n"] == "6"
    assert parsed[0]["best_release_n"] == "2"
    assert parsed[0]["best_release_slot_local"] == ""
    export_sql = statements[9:]
    assert export_sql
    assert all("best_post" not in sql and "best_release" not in sql for sql in export_sql)
    assert writes == []
    assert batches == [17, 17, 17], batches

    async def failed_history(*args, **kwargs):
        return empty_history("Data temporarily unavailable. Stored history could not be read on this request.")

    monkeypatch.setattr(main, "load_history_for_artist", failed_history)
    failed = client.get(f"/artist/{ada}?tz=UTC")
    assert failed.status_code == 200
    failed_card = _card(failed.text)
    assert "Not enough exact timestamps (n=0) to suggest a time." in failed_card
    assert "Best time to post / release" in failed.text

    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
