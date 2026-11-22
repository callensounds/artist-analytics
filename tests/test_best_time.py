"""Best posting and release clocks from rows the page already loaded. No new D1 reads."""

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from best_time import BASIS, MIN_WINNER_COUNT, SLOT_MIN_EXACT, best_time, export_fields
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
    assert MIN_WINNER_COUNT == 2

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

    mixed_history = _history(_posts(_weeks(july, 4) + _weeks(january, 4)))
    mixed = best_time(mixed_history, "America/New_York")
    mixed_posts = mixed["posts"]
    # Summer is 19:00 local and winter is 18:00 local, four each. UTC is a
    # single 23:00 peak, but the viewer clock is tied, so nothing is named.
    assert mixed_posts["n"] == 8
    assert mixed_posts["hour"] is None
    assert mixed_posts["slot"] is None
    assert mixed_posts["engagement"] is None
    assert mixed_posts["hour_message"] == "Not enough signal: 2 hours tied at 4 of 8 exact posts."
    assert mixed_posts["slot_message"] == (
        "Not enough signal: 2 weekday-and-hour slots tied at 4 of 8 exact posts."
    )
    assert "23:00" not in mixed_posts["hour_message"]
    assert "18:00" not in mixed_posts["hour_message"]
    mixed_fields = export_fields(mixed_history, "America/New_York")
    assert mixed_fields["best_post_slot_local"] == ""
    assert mixed_fields["best_post_slot_utc"] == ""
    assert mixed_fields["best_post_n"] == "8"


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


def test_tied_clocks_are_not_announced():
    monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
    early = _weeks(monday.replace(hour=2), 3)
    late = _weeks(monday.replace(hour=5), 3)
    rows = _posts(early + late)
    for row in rows[:3]:
        row["views"] = 30
    history = _history(rows)
    posts = best_time(history, "UTC")["posts"]
    assert posts["n"] == 6
    assert posts["slot"] is None
    assert posts["hour"] is None
    assert posts["engagement"] is None
    assert posts["hour_message"] == "Not enough signal: 2 hours tied at 3 of 6 exact posts."
    assert posts["slot_message"] == (
        "Not enough signal: 2 weekday-and-hour slots tied at 3 of 6 exact posts."
    )
    assert "02:00" not in posts["hour_message"]
    assert "05:00" not in posts["slot_message"]
    fields = export_fields(history, "UTC")
    assert fields["best_post_slot_local"] == ""
    assert fields["best_post_slot_utc"] == ""
    assert fields["best_post_n"] == "6"
    assert "tied" not in fields["best_post_slot_local"]


def test_distinct_hours_have_no_signal_and_empty_csv_cells():
    start = datetime(2026, 1, 5, 0, 30, tzinfo=timezone.utc)
    moments = [start + timedelta(days=index, hours=index) for index in range(8)]
    rows = _posts(moments)
    for row in rows:
        row["views"] = 40
    history = _history(rows)
    posts = best_time(history, "UTC")["posts"]
    assert posts["n"] == 8
    assert posts["status"] == "ok"
    assert posts["message"] == ""
    assert posts["hour"] is None
    assert posts["slot"] is None
    assert posts["engagement"] is None
    assert posts["hour_message"] == "Not enough signal: no hour repeats across 8 exact posts."
    assert posts["slot_message"] == (
        "Not enough signal: no weekday-and-hour slot repeats across 8 exact posts."
    )
    assert "00:00" not in posts["hour_message"]
    assert "Mon" not in posts["slot_message"]
    fields = export_fields(history, "UTC")
    assert fields["best_post_slot_local"] == ""
    assert fields["best_post_slot_utc"] == ""
    assert fields["best_post_n"] == "8"
    assert "tied" not in "".join(fields.values())
    release_rows = [
        {"title": f"Release {index}", "released_at": _stamp(moment), "precision": "time"}
        for index, moment in enumerate(moments)
    ]
    releases = best_time(_history(releases=release_rows), "UTC")["releases"]
    assert releases["hour"] is None
    assert releases["slot"] is None
    assert releases["hour_message"] == "Not enough signal: no hour repeats across 8 exact releases."
    assert releases["slot_message"] == (
        "Not enough signal: no weekday-and-hour slot repeats across 8 exact releases."
    )
    release_fields = export_fields({"sightings": [], "releases": release_rows}, "UTC")
    assert release_fields["best_release_slot_local"] == ""
    assert release_fields["best_release_slot_utc"] == ""
    assert release_fields["best_release_n"] == "8"


def test_tie_at_two_suppresses_the_hour_and_the_slot():
    monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
    moments = (
        _weeks(monday.replace(hour=2), 2)
        + _weeks(monday.replace(hour=9), 2)
        + [monday.replace(hour=14) + timedelta(weeks=4)]
    )
    posts = best_time(_history(_posts(moments)), "UTC")["posts"]
    assert posts["n"] == 5
    assert posts["hour"] is None
    assert posts["slot"] is None
    assert posts["engagement"] is None
    assert posts["message"] == ""
    assert posts["hour_message"] == "Not enough signal: 2 hours tied at 2 of 5 exact posts."
    assert posts["slot_message"] == (
        "Not enough signal: 2 weekday-and-hour slots tied at 2 of 5 exact posts."
    )
    assert "02:00" not in posts["hour_message"]
    assert "09:00" not in posts["slot_message"]


def test_unique_winner_with_three_of_twelve_is_shown():
    winners = _weeks(datetime(2026, 1, 5, 13, 15, tzinfo=timezone.utc), 3)
    others = [
        datetime(2026, 3, 2, hour, 10, tzinfo=timezone.utc) + timedelta(days=hour)
        for hour in range(9)
    ]
    history = _history(_posts(winners + others))
    posts = best_time(history, "UTC")["posts"]
    assert posts["n"] == 12
    assert posts["hour_message"] == ""
    assert posts["slot_message"] == ""
    hour = posts["hour"]
    slot = posts["slot"]
    assert hour["local_hour"] == 13
    assert hour["utc_hour"] == 13
    assert hour["count"] == 3
    assert hour["total"] == 12
    assert hour["tied"] is False
    assert hour["utc_tied"] is False
    assert hour["display"] == "13:00-14:00 UTC · 3 of 12 exact posts"
    assert slot["local_weekday"] == "Monday"
    assert slot["local_label"] == "Mon 13:00-14:00"
    assert slot["utc_label"] == "Mon 13:00-14:00"
    assert slot["tied"] is False
    assert slot["display"] == "Mon 13:00-14:00 UTC · 3 of 12 exact posts"
    fields = export_fields(history, "UTC")
    assert fields["best_post_slot_local"] == "Mon 13:00-14:00"
    assert fields["best_post_slot_utc"] == "Mon 13:00-14:00"
    assert fields["best_post_n"] == "12"
    assert "tied" not in fields["best_post_slot_local"]

    pair = _weeks(datetime(2026, 1, 5, 4, 15, tzinfo=timezone.utc), 2)
    pair.append(datetime(2026, 1, 5, 11, 15, tzinfo=timezone.utc) + timedelta(weeks=2))
    shown = best_time(_history(_posts(pair)), "UTC")["posts"]
    assert shown["n"] == 3
    assert shown["hour"]["count"] == 2
    assert shown["hour"]["local_hour"] == 4
    assert shown["hour"]["display"] == "04:00-05:00 UTC · 2 of 3 exact posts"
    assert shown["hour_message"] == ""
    assert shown["slot"] is None


def test_local_peak_is_kept_when_utc_has_no_single_peak():
    summer = _weeks(datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc), 3)
    winter = _weeks(datetime(2026, 1, 14, 1, 30, tzinfo=timezone.utc), 3)
    history = _history(_posts(summer + winter))
    posts = best_time(history, "America/New_York")["posts"]
    assert posts["n"] == 6
    hour = posts["hour"]
    slot = posts["slot"]
    assert hour["local_hour"] == 20
    assert hour["count"] == 6
    assert hour["utc_hour"] is None
    assert hour["utc_label"] == ""
    assert hour["utc_count"] is None
    assert hour["tied"] is False
    assert hour["utc_tied"] is True
    assert hour["aligned"] is False
    assert hour["display"] == (
        "20:00-21:00 America/New_York (6 of 6 exact posts). The UTC clock has no single peak."
    )
    assert "00:00-01:00" not in hour["display"]
    assert "01:00-02:00" not in hour["display"]
    assert "tied" not in hour["display"]
    assert slot["local_weekday"] == "Tuesday"
    assert slot["local_label"] == "Tue 20:00-21:00"
    assert slot["utc_label"] == ""
    assert slot["utc_hour"] is None
    assert slot["utc_tied"] is True
    assert slot["display"] == (
        "Tue 20:00-21:00 America/New_York (6 of 6 exact posts). The UTC clock has no single peak."
    )
    assert "Wed" not in slot["display"]
    fields = export_fields(history, "America/New_York")
    assert fields["best_post_slot_local"] == "Tue 20:00-21:00"
    assert fields["best_post_slot_utc"] == ""
    assert fields["best_post_n"] == "6"
    assert "tied" not in fields["best_post_slot_local"]


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
    assert "Not enough signal" not in posts
    assert "tied, 1 of" not in plain_card
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


def test_artist_page_says_not_enough_signal_when_every_hour_is_distinct(monkeypatch, tmp_path):
    path = tmp_path / "best-time-signal.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    start = datetime(2026, 1, 5, 0, 15, tzinfo=timezone.utc)

    async def seed():
        await migrate(database)
        await database.execute(
            """
            INSERT INTO artists (name, normalized_name, origin, tracked, created_at)
            VALUES ('Cara', 'cara', 'search', 1, '2026-01-01T00:00:00Z')
            """
        )
        cara = int((await database.query_one("SELECT id FROM artists WHERE normalized_name = 'cara'"))["id"])
        for index in range(8):
            moment = start + timedelta(days=index, hours=index)
            await database.execute(
                """
                INSERT INTO content_sightings (
                    artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
                ) VALUES (?, 'instagram', ?, 'Clip', 'post', ?, 'time', '2026-01-01T00:00:00Z')
                """,
                (cara, f"ig-{index}", _stamp(moment)),
            )
        return cara

    cara = asyncio.run(seed())
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    page = client.get(f"/artist/{cara}")
    assert page.status_code == 200
    card = _card(page.text)
    posts = card.split('id="best-time-posts"', 1)[1].split('id="best-time-releases"', 1)[0]
    assert "Not enough signal: no hour repeats across 8 exact posts." in posts
    assert "Not enough signal: no weekday-and-hour slot repeats across 8 exact posts." in posts
    assert "Most common hour" not in posts
    assert "Most common weekday and hour" not in posts
    assert "00:00-01:00" not in posts
    assert "tied, 1 of" not in card
    assert "Median" not in posts

    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
