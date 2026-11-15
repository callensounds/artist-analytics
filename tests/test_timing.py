"""Cross-artist timing. Stored rows only: precision is never refined, and the page does not write."""

import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from export_csv import formula_safe
from timing import (
    ARTIST_CAP,
    COMPOUND_TERM_LIMIT,
    EMPTY_WATCHLIST,
    HOUR_EMPTY,
    MODAL_MIN_EXACT,
    NOT_ENOUGH_EXACT,
    ROW_CAP,
    WEEKDAY_EMPTY,
    assemble_timing,
    event_statements,
    render_timing_csv,
    render_timing_json,
    timing_csv_columns,
    timing_sql_catalog,
    tracked_statement,
    watch_statement,
)
from compare import compound_select_terms, like_glob_patterns

NOW = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
FRIDAY = "2026-11-13"
THURSDAY = "2026-11-12"
MONDAY = "2026-11-09"


def _artists(*rows):
    return [{"id": artist_id, "name": name} for artist_id, name in rows]


def _sighting(artist_id, source, external_id, title, kind, published_at, precision):
    return {
        "artist_id": artist_id,
        "source": source,
        "external_id": external_id,
        "title": title,
        "kind": kind,
        "published_at": published_at,
        "precision": precision,
    }


def _release(artist_id, mbid, title, released_at, precision):
    return {
        "artist_id": artist_id,
        "source": "musicbrainz",
        "external_id": mbid,
        "title": title,
        "kind": "album",
        "published_at": released_at,
        "precision": precision,
    }


def _view(sightings, releases=None, *, artists=None, days="365", kind="all", scope="watchlist"):
    return assemble_timing(
        artists or _artists((1, "Ada")),
        sightings,
        releases or [],
        scope=scope,
        days=days,
        kind=kind,
        now=NOW,
    )


def _heatmap_total(view) -> int:
    return sum(count for row in view["heatmap"] for count in row)


def test_formula_safe_prefixes_spreadsheet_formulas():
    assert formula_safe("=1+1").startswith("'")
    assert formula_safe("=1+1") == "'=1+1"
    assert formula_safe("+Ada") == "'+Ada"
    assert formula_safe("-Ada") == "'-Ada"
    assert formula_safe("@SUM(A1)") == "'@SUM(A1)"
    assert formula_safe(" =1+1") == "' =1+1"
    assert formula_safe("\t=1+1").startswith("'")
    assert formula_safe("Ada") == "Ada"
    assert formula_safe(None) == ""
    assert formula_safe("16:00") == "16:00"


def test_coarse_dates_are_not_placed_and_day_rows_skip_the_hour():
    """A month or a year never enters the heatmap. A day sets the weekday and not an hour."""
    view = _view(
        [
            _sighting(1, "spotify", "year", "Yearbook", "release", "2026", "year"),
            _sighting(1, "spotify", "month", "November", "release", "2026-11", "month"),
            _sighting(1, "spotify", "stated-year", "Finer than stored", "release", "2026-11-13T16:00:00Z", "month"),
            _sighting(1, "spotify", "stated-time", "Date only", "release", THURSDAY, "time"),
            _sighting(1, "apple", "day-clock", "Clock ignored", "release", f"{FRIDAY}T16:00:00Z", "day"),
            _sighting(1, "apple", "exact", "Exact", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(1, "deezer", "old-year", "Ancient", "album", "2020", "year"),
        ]
    )
    assert view["counts"]["coarse"] >= 3
    assert view["counts"]["exact"] == 1
    assert view["heatmap"][4][16] == 1
    assert _heatmap_total(view) == 1
    assert view["heatmap"][4][0] == 0
    assert view["hour_counts"][0] == 0
    assert view["hour_counts"][16] == 1
    # Thursday is the date-only row whose stored precision said time. Friday is the real clock
    # plus the day-precision row that carried a clock we refused to use.
    assert view["weekday_counts"][3] == 1
    assert view["weekday_counts"][4] == 2
    assert all(sum(row) == (1 if index == 4 else 0) for index, row in enumerate(view["heatmap"]))
    assert "2020" not in json.dumps(view["counts"])
    assert view["counts"]["coarse"] == 3
    sources = {row["source"]: row for row in view["sources"]}
    assert sources["spotify"]["coarse"] == 3
    assert sources["spotify"]["exact"] == 0
    assert sources["apple"]["exact"] == 1
    assert sources["apple"]["day"] == 1
    assert "deezer" not in sources


def test_midnight_exact_timestamp_is_hour_zero_and_a_date_is_not():
    midnight = _view([_sighting(1, "apple", "mid", "Midnight", "release", f"{FRIDAY}T00:00:00Z", "time")])
    assert midnight["heatmap"][4][0] == 1
    assert midnight["artists"][0]["latest_exact_at"] == f"{FRIDAY}T00:00:00Z"
    date_only = _view([_sighting(1, "apple", "day", "Day", "release", FRIDAY, "day")])
    assert _heatmap_total(date_only) == 0
    assert date_only["hour_counts"] == [0] * 24
    assert date_only["weekday_counts"][4] == 1
    assert date_only["hour_reason"] == HOUR_EMPTY
    assert date_only["artists"][0]["modal_note"] == NOT_ENOUGH_EXACT
    assert date_only["artists"][0]["latest_exact_at"] == ""


def test_same_release_is_counted_once_across_sources():
    view = _view(
        [
            _sighting(1, "spotify", "sp", "North", "release", FRIDAY, "day"),
            _sighting(1, "apple", "ap", "North!", "release", f"{FRIDAY}T15:00:00Z", "time"),
            _sighting(1, "deezer", "dz", "North", "album", f"{FRIDAY}T07:00:00Z", "time"),
            _sighting(1, "spotify", "other-day", "North", "release", f"{MONDAY}T09:00:00Z", "time"),
            _sighting(1, "instagram", "p1", "North", "post", f"{FRIDAY}T15:00:00Z", "time"),
            _sighting(1, "tiktok", "p2", "North", "post", f"{FRIDAY}T15:30:00Z", "time"),
        ],
        [_release(1, "mb", "North", FRIDAY, "day")],
    )
    # Friday release: day, 15:00, 07:00, and MusicBrainz collapse to the later exact time.
    # Monday is a different UTC day. The two posts are not releases, so both stay.
    assert view["counts"]["exact"] == 4
    assert view["counts"]["collapsed"] == 3
    assert view["heatmap"][4][15] == 3
    assert view["heatmap"][4][7] == 0
    assert view["heatmap"][0][9] == 1
    assert view["weekday_counts"][4] == 3
    ada = view["artists"][0]
    assert ada["exact_count"] == 4
    assert ada["latest_exact_at"] == f"{FRIDAY}T15:30:00Z"
    sources = {row["source"]: row for row in view["sources"]}
    assert sources["spotify"]["day"] == 1
    assert sources["apple"]["exact"] == 1
    assert sources["deezer"]["exact"] == 1
    assert sources["musicbrainz"]["day"] == 1
    assert sources["instagram"]["exact"] == 1
    assert "counted once" in " ".join(view["cap_notes"])
    assert view["insights"] == [
        "1 of 2 dated releases landed on Friday UTC.",
        "2 of 2 dated posts landed on Friday UTC.",
    ]


def test_identical_coarse_stamp_collapses_and_a_year_stays_beside_a_day():
    collapsed = _view(
        [
            _sighting(1, "spotify", "a", "Old", "release", "2026", "year"),
            _sighting(1, "apple", "b", "Old", "release", "2026", "year"),
        ]
    )
    assert collapsed["counts"]["coarse"] == 1
    assert collapsed["counts"]["collapsed"] == 1
    assert _heatmap_total(collapsed) == 0
    assert collapsed["insights"] == []
    beside = _view(
        [
            _sighting(1, "spotify", "year", "North", "release", "2026", "year"),
            _sighting(1, "apple", "day", "North", "release", THURSDAY, "day"),
        ]
    )
    assert beside["counts"]["coarse"] == 1
    assert beside["counts"]["day"] == 1
    assert beside["counts"]["collapsed"] == 0
    assert beside["weekday_counts"][3] == 1
    assert _heatmap_total(beside) == 0


def test_modal_weekday_and_hour_need_three_exact_timestamps():
    assert MODAL_MIN_EXACT == 3
    two = _view(
        [
            _sighting(1, "apple", "a", "One", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(1, "apple", "b", "Two", "release", f"{FRIDAY}T16:00:00Z", "time"),
        ]
    )
    assert two["artists"][0]["modal_note"] == NOT_ENOUGH_EXACT
    assert two["artists"][0]["modal_weekday"] == ""
    assert two["artists"][0]["modal_hour_utc"] == ""
    assert two["artists"][0]["exact_count"] == 2

    days_only = _view(
        [
            _sighting(1, "apple", str(index), f"Day {index}", "release", FRIDAY, "day")
            for index in range(3)
        ]
    )
    assert days_only["artists"][0]["exact_count"] == 0
    assert days_only["artists"][0]["modal_note"] == NOT_ENOUGH_EXACT
    assert days_only["artists"][0]["friday_share"] == "3 of 3"
    assert days_only["weekday_counts"][4] == 3
    assert _heatmap_total(days_only) == 0

    three = _view(
        [
            _sighting(1, "apple", "a", "One", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(1, "apple", "b", "Two", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(1, "apple", "c", "Three", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(1, "spotify", "mon", "Monday", "release", MONDAY, "day"),
            _sighting(1, "spotify", "mon2", "Monday 2", "release", MONDAY, "day"),
            _sighting(1, "spotify", "mon3", "Monday 3", "release", MONDAY, "day"),
            _sighting(1, "spotify", "mon4", "Monday 4", "release", MONDAY, "day"),
            _sighting(1, "spotify", "mon5", "Monday 5", "release", MONDAY, "day"),
        ]
    )
    ada = three["artists"][0]
    assert ada["exact_count"] == 3
    assert ada["modal_weekday"] == "Friday"
    assert ada["modal_hour_utc"] == "16:00"
    assert ada["modal_note"] == ""
    assert ada["friday_share"] == "3 of 8"
    assert ada["latest_exact_at"] == f"{FRIDAY}T16:00:00Z"
    assert three["insights"] == ["3 of 8 dated releases landed on Friday UTC."]

    tie = _view(
        [
            _sighting(1, "apple", "m1", "M1", "release", f"{MONDAY}T09:00:00Z", "time"),
            _sighting(1, "apple", "m2", "M2", "release", f"{MONDAY}T09:00:00Z", "time"),
            _sighting(1, "apple", "f1", "F1", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(1, "apple", "f2", "F2", "release", f"{FRIDAY}T16:00:00Z", "time"),
        ]
    )
    assert tie["artists"][0]["modal_weekday"] == "Monday"
    assert tie["artists"][0]["modal_hour_utc"] == "09:00"


def test_kind_filter_and_window_keep_only_matching_stored_rows():
    rows = [
        _sighting(1, "apple", "inside", "Inside", "release", f"{FRIDAY}T16:00:00Z", "time"),
        _sighting(1, "instagram", "post", "Clip", "post", f"{MONDAY}T11:00:00Z", "time"),
        _sighting(1, "apple", "august", "August", "release", "2026-08-01T12:00:00Z", "time"),
        _sighting(1, "apple", "old", "Old day", "release", "2024-06-01", "day"),
    ]
    releases = [_release(1, "mb", "Official", FRIDAY, "day")]
    posts = _view(rows, releases, kind="post")
    assert posts["counts"]["exact"] == 1
    assert posts["heatmap"][0][11] == 1
    assert posts["insights"] == ["0 of 1 dated posts landed on Friday UTC."]
    assert all(row["source"] != "musicbrainz" for row in posts["sources"])

    releases_only = _view(rows, releases, kind="release")
    assert releases_only["counts"]["exact"] == 2
    assert releases_only["counts"]["day"] == 1
    assert "dated posts" not in " ".join(releases_only["insights"])
    assert "2 of 3 dated releases landed on Friday UTC." in releases_only["insights"]

    recent = _view(rows, releases, days="90")
    assert recent["heatmap"][4][16] == 1
    assert recent["hour_counts"][12] == 0
    assert recent["counts"]["day"] == 1
    everything = _view(rows, releases, days="all")
    assert everything["counts"]["day"] == 2
    assert everything["hour_counts"][12] == 1

    year = _view([_sighting(1, "spotify", "y", "Year", "release", "2025", "year")], days="90")
    assert year["counts"]["coarse"] == 0
    year_in = _view([_sighting(1, "spotify", "y", "Year", "release", "2025", "year")], days="365")
    assert year_in["counts"]["coarse"] == 1
    assert _heatmap_total(year_in) == 0
    assert year_in["weekday_counts"] == [0] * 7


def test_empty_scope_does_not_invent_a_friday_share():
    empty = assemble_timing([], [], [], scope="watchlist", days="365", kind="all", now=NOW)
    assert empty["empty"] is True
    assert empty["empty_reason"] == EMPTY_WATCHLIST
    assert empty["insights"] == []
    assert empty["artists"] == []
    assert "landed on Friday" not in empty["empty_reason"]
    assert _heatmap_total(empty) == 0
    coarse = _view([_sighting(1, "spotify", "m", "Month", "release", "2026-11", "month")])
    assert coarse["empty"] is False
    assert coarse["counts"]["counted"] == 1
    assert coarse["insights"] == []
    assert coarse["hour_reason"] == HOUR_EMPTY
    assert coarse["weekday_reason"] == WEEKDAY_EMPTY
    assert coarse["heatmap_rows"] == []
    assert coarse["artists"][0]["friday_share"] == ""
    assert coarse["artists"][0]["modal_note"] == NOT_ENOUGH_EXACT


def test_csv_columns_match_artist_counts_and_escape_formulas():
    view = assemble_timing(
        _artists((1, "=1+1"), (2, "@SUM(A1)"), (3, "Ada, Jr")),
        [
            _sighting(1, "apple", "a", "One", "release", f"{FRIDAY}T16:00:00Z", "time"),
            _sighting(3, "apple", "b", "Two", "release", f"{MONDAY}T09:00:00Z", "time"),
        ],
        [],
        scope="all",
        days="365",
        kind="release",
        now=NOW,
    )
    text = render_timing_csv(view)
    header = timing_csv_columns()
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert list(parsed[0].keys()) == header
    assert sum(1 for column in header if column.startswith("weekday_")) == 7
    assert sum(1 for column in header if column.startswith("hour_")) == 24
    by_name = {row["artist"]: row for row in parsed}
    assert by_name["'=1+1"]["hour_16"] == "1"
    assert by_name["'=1+1"]["weekday_fri"] == "1"
    assert by_name["'=1+1"]["weekday_mon"] == "0"
    assert by_name["'=1+1"]["exact_count"] == "1"
    assert by_name["'=1+1"]["modal_note"] == NOT_ENOUGH_EXACT
    assert by_name["'@SUM(A1)"]["exact_count"] == "0"
    assert by_name["Ada, Jr"]["hour_09"] == "1"
    assert by_name["Ada, Jr"]["latest_exact_at"] == f"{MONDAY}T09:00:00Z"
    assert not any(row["artist"].startswith(("=", "+", "-", "@")) for row in parsed)
    document = json.loads(render_timing_json(view))
    assert document["timezone"] == "UTC"
    assert document["scope"] == "all"
    assert document["days"] == "365"
    assert document["kind"] == "release"
    assert len(document["heatmap"]) == 7
    assert all(len(row) == 24 for row in document["heatmap"])
    assert document["weekday_labels"][0] == "Monday"
    assert document["weekday_labels"][4] == "Friday"
    assert len(document["hour_counts"]) == 24
    artist = next(item for item in document["artists"] if item["name"] == "=1+1")
    assert len(artist["weekday_counts"]) == 7
    assert len(artist["hour_counts"]) == 24
    assert sum(artist["hour_counts"]) == artist["exact_count"]
    assert artist["hour_counts"][16] == 1
    assert "read_error" in document
    assert "insights" in document
    assert "sources" in document
    assert "counts" in document
    assert document["heatmap"][4][16] == 1


def test_timing_sql_stays_within_d1_limits():
    """No compound SELECT over the D1 limit, no LIKE or GLOB, and the IN list is capped."""
    sqls = timing_sql_catalog(list(range(1, 80)))
    assert len(sqls) > 5
    for sql in sqls:
        assert compound_select_terms(sql) <= COMPOUND_TERM_LIMIT
        assert compound_select_terms(sql) <= 5
        folded = f" {sql.upper()} "
        assert " UNION " not in folded
        assert " INTERSECT " not in folded
        assert " EXCEPT " not in folded
        assert " GLOB " not in folded
        assert " LIKE " not in folded
        for pattern in like_glob_patterns(sql):
            assert len(pattern) <= 50
        assert "LIMIT" in sql.upper()
    assert event_statements([], kind="all", days="365", now=NOW) == []
    statements = event_statements(list(range(1, 80)), kind="all", days="365", now=NOW)
    assert len(statements) == 2
    for sql, params in statements:
        assert sql.count("?") == len(params)
        assert sum(isinstance(value, int) for value in params) == ARTIST_CAP
        assert ARTIST_CAP + 1 not in params
        assert f"LIMIT {ROW_CAP + 1}" in sql
    posts = event_statements([4, 6], kind="post", days="90", now=NOW)
    assert len(posts) == 1
    assert "musicbrainz_releases" not in posts[0][0]
    assert "instagram" in posts[0][0]
    releases = event_statements([4], kind="release", days="all", now=NOW)
    assert len(releases) == 2
    assert "musicbrainz_releases" in releases[1][0]
    assert "NOT IN" in releases[0][0]
    assert "LIMIT" in watch_statement(1)[0]
    assert "LIMIT" in tracked_statement()[0]


def _plan(database: SqliteDatabase, sql: str, params: tuple = ()) -> str:
    async def run():
        rows = await database.query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(row.get("detail") or "") for row in rows)

    return asyncio.run(run())


def test_timing_query_plans_use_artist_indexes():
    database = SqliteDatabase(":memory:")
    asyncio.run(migrate(database))
    watch_sql, watch_params = watch_statement(1)
    assert "idx_user_artists_user" in _plan(database, watch_sql, watch_params)
    statements = event_statements([4, 6], kind="all", days="365", now=NOW)
    sighting_sql, sighting_params = statements[0]
    release_sql, release_params = statements[1]
    sighting_plan = _plan(database, sighting_sql, sighting_params)
    release_plan = _plan(database, release_sql, release_params)
    assert "SCAN content_sightings" not in sighting_plan
    assert "idx_mb_releases_artist" in release_plan
    assert "SCAN musicbrainz_releases" not in release_plan


def _past(now: datetime, weekday: int, hour: int) -> datetime:
    delta = (now.weekday() - weekday) % 7
    day = now - timedelta(days=delta)
    stamp = day.replace(hour=hour, minute=0, second=0, microsecond=0)
    if stamp > now:
        stamp = stamp - timedelta(days=7)
    return stamp


def test_timing_page_scopes_watchlist_and_does_not_write(monkeypatch, tmp_path):
    path = tmp_path / "timing.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    now = datetime.now(timezone.utc)
    friday = _past(now, 4, 16)
    monday = _past(now, 0, 9)
    friday_stamp = friday.strftime("%Y-%m-%dT%H:%M:%SZ")
    monday_stamp = monday.strftime("%Y-%m-%dT%H:%M:%SZ")
    month_stamp = now.strftime("%Y-%m")

    async def seed():
        await migrate(database)
        for name, tracked in (("=1+1", 1), ("Bea", 1), ("Hidden", 0)):
            await database.execute(
                "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', ?, ?)",
                (name, name.casefold(), tracked, friday_stamp),
            )
        rows = await database.query("SELECT id, name FROM artists ORDER BY id")
        ids = {row["name"]: int(row["id"]) for row in rows}
        ada, bea, hidden = ids["=1+1"], ids["Bea"], ids["Hidden"]
        await database.execute(
            """
            INSERT INTO content_sightings (
                artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
            ) VALUES (?, 'apple', 'exact', 'North', 'release', ?, 'time', ?),
                   (?, 'spotify', 'day', 'North', 'release', ?, 'day', ?),
                   (?, 'instagram', 'clip', 'Clip', 'post', ?, 'time', ?),
                   (?, 'apple', 'hidden', 'Secret', 'release', ?, 'time', ?)
            """,
            (
                ada, friday_stamp, friday_stamp,
                ada, friday.strftime("%Y-%m-%d"), friday_stamp,
                ada, monday_stamp, friday_stamp,
                hidden, friday_stamp, friday_stamp,
            ),
        )
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES (?, 'mb-bea', 'South', 'Single', ?, 'time', ?, ?),
                   (?, 'mb-month', 'Month', 'Album', ?, 'month', ?, ?)
            """,
            (bea, monday_stamp, friday_stamp, friday_stamp, ada, month_stamp, friday_stamp, friday_stamp),
        )
        return ada, bea, hidden

    ada, bea, hidden = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("timing must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    locked = client.get("/timing", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/login"
    for path in ("/timing.csv", "/timing.json"):
        denied = client.get(path, follow_redirects=False)
        assert denied.status_code == 303
        assert denied.headers["location"] == "/login"

    logged = client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    assert logged.status_code == 303
    user = asyncio.run(database.query_one("SELECT id FROM users WHERE email = ?", ("artist@demo.com",)))
    asyncio.run(
        database.execute(
            "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, ?)",
            (int(user["id"]), ada, friday_stamp),
        )
    )

    writes = []
    queries = []
    original_execute = SqliteDatabase.execute
    original_batch = SqliteDatabase.batch
    original_query = SqliteDatabase.query

    async def spy_execute(self, sql, params=()):
        writes.append(sql)
        return await original_execute(self, sql, params)

    async def spy_batch(self, statements):
        for sql, _params in statements:
            writes.append(sql)
        return await original_batch(self, statements)

    async def spy_query(self, sql, params=()):
        queries.append(sql)
        head = sql.lstrip().split(None, 1)[0].lower()
        if head not in {"select", "with", "explain"}:
            writes.append(sql)
        return await original_query(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "execute", spy_execute)
    monkeypatch.setattr(SqliteDatabase, "batch", spy_batch)
    monkeypatch.setattr(SqliteDatabase, "query", spy_query)

    page = client.get("/timing")
    assert page.status_code == 200
    assert "Timing" in page.text
    assert 'href="/timing"' in page.text
    assert "=1+1" in page.text
    assert "South" not in page.text
    assert "Bea" not in page.text
    assert "not enough exact timestamps" in page.text
    assert "1 of 1 dated releases landed on Friday UTC." in page.text
    assert "0 of 1 dated posts landed on Friday UTC." in page.text
    assert 'data-timing-exact="2"' in page.text
    assert calls == []
    assert writes == []
    timing_sql = [
        sql
        for sql in queries
        if "content_sightings" in sql or "musicbrainz_releases" in sql or "user_artists" in sql
    ]
    assert len(timing_sql) == 3
    assert sum("content_sightings" in sql for sql in timing_sql) == 1
    assert sum("musicbrainz_releases" in sql for sql in timing_sql) == 1
    assert sum("user_artists" in sql for sql in timing_sql) == 1
    for sql in timing_sql:
        assert sql.lstrip().lower().startswith("select")
        assert "LIMIT" in sql.upper()
        assert compound_select_terms(sql) <= 5
        assert "UNION" not in sql.upper()

    queries.clear()
    posts = client.get("/timing", params={"kind": "post"})
    assert posts.status_code == 200
    assert "dated releases" not in posts.text
    assert "0 of 1 dated posts landed on Friday UTC." in posts.text
    assert not any("musicbrainz_releases" in sql for sql in queries)
    assert any("content_sightings" in sql for sql in queries)

    queries.clear()
    everyone = client.get("/timing.json", params={"scope": "all", "kind": "release"})
    assert everyone.status_code == 200
    document = everyone.json()
    names = [artist["name"] for artist in document["artists"]]
    assert "=1+1" in names
    assert "Bea" in names
    assert "Hidden" not in names
    assert document["timezone"] == "UTC"
    assert len(document["heatmap"]) == 7
    assert all(len(row) == 24 for row in document["heatmap"])
    assert document["counts"]["exact"] == 2
    assert "1 of 2 dated releases landed on Friday UTC." in document["insights"]
    assert writes == []
    assert any("FROM artists" in sql and "tracked" in sql for sql in queries)

    downloaded = client.get("/timing.csv", params={"scope": "watchlist", "kind": "all"})
    assert downloaded.status_code == 200
    assert "text/csv" in downloaded.headers["content-type"]
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    assert list(parsed[0].keys()) == timing_csv_columns()
    assert parsed[0]["artist"] == "'=1+1"
    assert parsed[0]["exact_count"] == "2"
    assert writes == []
    client.get("/logout", follow_redirects=False)


def test_empty_watchlist_and_row_cap_are_reported(monkeypatch, tmp_path):
    path = tmp_path / "empty-timing.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    now = datetime.now(timezone.utc)
    newer = _past(now, 4, 16)
    older = newer - timedelta(days=7)

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Ada', 'ada', 'search', 1, ?)",
            (newer.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        await database.execute(
            """
            INSERT INTO content_sightings (
                artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
            ) VALUES (?, 'apple', 'new', 'Newer', 'release', ?, 'time', ?),
                   (?, 'apple', 'old', 'Older', 'release', ?, 'time', ?)
            """,
            (
                artist_id, newer.strftime("%Y-%m-%dT%H:%M:%SZ"), newer.strftime("%Y-%m-%dT%H:%M:%SZ"),
                artist_id, older.strftime("%Y-%m-%dT%H:%M:%SZ"), newer.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )

    asyncio.run(seed())
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    queries = []
    original_query = SqliteDatabase.query

    async def spy_query(self, sql, params=()):
        queries.append(sql)
        return await original_query(self, sql, params)

    monkeypatch.setattr(SqliteDatabase, "query", spy_query)
    empty = client.get("/timing")
    assert empty.status_code == 200
    assert EMPTY_WATCHLIST in empty.text
    assert "landed on Friday" not in empty.text
    assert not any("content_sightings" in sql or "musicbrainz_releases" in sql for sql in queries)

    monkeypatch.setattr("timing.ROW_CAP", 1)
    capped = client.get("/timing", params={"scope": "all"})
    assert capped.status_code == 200
    newer_stamp = newer.strftime("%Y-%m-%dT%H:%M:%SZ")
    older_stamp = older.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "stopped at 1" in capped.text
    assert newer_stamp in capped.text
    assert older_stamp not in capped.text
    document = client.get("/timing.json", params={"scope": "all"}).json()
    assert document["counts"]["exact"] == 1
    assert document["artists"][0]["latest_exact_at"] == newer_stamp
    assert document["empty"] is False
    assert any("stopped at 1" in note for note in document["cap_notes"])
    client.get("/logout", follow_redirects=False)
