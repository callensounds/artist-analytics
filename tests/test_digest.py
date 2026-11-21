"""Daily digest. Stored rows only: watchlist or top tracked artists, no writes."""

import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from compare import METRICS, compound_select_terms, like_glob_patterns
from crawl import _POOL, reset_pool
from db import SqliteDatabase, migrate, utc_now
from digest import (
    COMPOUND_TERM_LIMIT,
    DIGEST_READ_ERROR,
    _playlist_legacy_statement,
    _window,
    build_digest,
    digest_sql_catalog,
    fallback_statement,
    parse_threshold,
    public_count_is_rounded,
    render_digest_csv,
    render_digest_json,
    report_statements,
    watch_statement,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _stamp(**delta) -> str:
    return utc_now(NOW - timedelta(**delta))


def _reset_memory_users():
    main.USERS.clear()
    main.USERS["artist@demo.com"] = {
        "id": 1,
        "email": "artist@demo.com",
        "password_hash": "",
        "artist_name": "Demo Artist",
    }
    main.NEXT_ID = 2


def _plan(database: SqliteDatabase, sql: str, params: tuple = ()) -> str:
    async def run():
        rows = await database.query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(row.get("detail") or "") for row in rows)

    return asyncio.run(run())


class _ReadOnly:
    def __init__(self, database):
        self.database = database
        self.writes = 0

    async def execute(self, *args, **kwargs):
        self.writes += 1
        raise RuntimeError("digest write")

    async def batch(self, *args, **kwargs):
        self.writes += 1
        raise RuntimeError("digest write")

    async def query(self, sql, params=()):
        return await self.database.query(sql, params)

    async def query_one(self, sql, params=()):
        return await self.database.query_one(sql, params)

    async def query_batch(self, statements):
        return await self.database.query_batch(statements)


def test_parse_threshold_defaults_when_the_value_is_unusable():
    assert parse_threshold(None) == 5
    assert parse_threshold("") == 5
    assert parse_threshold("5") == 5
    assert parse_threshold("5%") == 5
    assert parse_threshold("4.5") == 4.5
    assert parse_threshold("0") == 5
    assert parse_threshold("0.0") == 5
    assert parse_threshold("101") == 5
    assert parse_threshold("-3") == 5
    assert parse_threshold("nope") == 5
    assert parse_threshold("5.54") == 5


def test_digest_sql_stays_within_d1_limits():
    """Compound SELECTs stay at or under 5 terms, and no artist list exceeds the cap."""
    sqls = digest_sql_catalog(list(range(1, 40)))
    assert len(sqls) > 10
    too_many = ",".join("?" * 13)
    for sql in sqls:
        assert compound_select_terms(sql) <= COMPOUND_TERM_LIMIT
        assert compound_select_terms(sql) <= 5
        folded = sql.upper()
        assert "UNION" not in folded
        assert "INTERSECT" not in folded
        assert "EXCEPT" not in folded
        assert "GLOB" not in folded
        assert " LIKE " not in f" {folded} "
        for pattern in like_glob_patterns(sql):
            assert len(pattern) <= 50
        assert too_many not in sql.replace(" ", "")
    statements = report_statements(list(range(1, 40)), NOW)
    assert len(statements) == len(METRICS) * 2 + 4
    for sql, params in statements:
        assert sql.count("?") == len(params)
        assert 12 in params
        assert 13 not in params
    assert report_statements([], NOW) == []


def test_digest_query_plans_seek_indexes(tmp_path):
    database = SqliteDatabase(str(tmp_path / "plans.sqlite"))
    asyncio.run(migrate(database))
    window = _window(NOW)
    watch_sql, watch_params = watch_statement(1)
    assert "idx_user_artists_user" in _plan(database, watch_sql, watch_params)
    fallback_sql, fallback_params = fallback_statement()
    fallback_plan = _plan(database, fallback_sql, fallback_params)
    assert "idx_metric_latest_value" in fallback_plan
    assert "SCAN metric_latest" not in fallback_plan
    assert "metric_snapshots" not in fallback_plan
    snapshot_plans = 0
    for sql, params in report_statements([4, 6], NOW):
        plan = _plan(database, sql, params)
        if "metric_snapshots" in sql:
            assert "idx_snapshots_lookup" in plan
            assert "SCAN metric_snapshots" not in plan
            snapshot_plans += 1
        elif "musicbrainz_releases" in sql:
            assert "idx_mb_releases_artist" in plan
        elif "chart_entries" in sql:
            assert "idx_chart_entries_title_date" in plan
            assert "sqlite_autoindex_chart_entries_1" not in plan
            assert "SCAN chart_entries" not in plan
        elif "playlist_appearances" in sql:
            assert "idx_playlist_artist_first" in plan
            assert "SCAN playlist_appearances" not in plan
        elif "metric_alerts" in sql:
            assert "idx_metric_alerts_artist" in plan
    assert snapshot_plans == len(METRICS) * 2
    legacy_sql, legacy_params = _playlist_legacy_statement([4], window["start_stamp"], window["now_stamp"])
    assert "idx_playlist_artist_first" in _plan(database, legacy_sql, legacy_params)
    assert "SCAN playlist_appearances" not in _plan(database, legacy_sql, legacy_params)


def _metric(doc: dict, source: str, name: str) -> dict:
    for artist in doc["artists"]:
        for item in artist["metrics"]:
            if item["source"] == source and item["metric"] == name:
                return item
    raise AssertionError(f"{source} {name} missing")


async def _seed_history(database: SqliteDatabase) -> dict:
    await migrate(database)
    ten = _stamp(days=10)
    eight = _stamp(days=8)
    six = _stamp(days=6, hours=1)
    almost = _stamp(days=5, hours=23)
    three = _stamp(days=3)
    latest = _stamp()

    async def artist(name: str) -> int:
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', 1, ?)",
            (name, name.casefold(), ten),
        )
        row = await database.query_one("SELECT id FROM artists WHERE name = ?", (name,))
        return int(row["id"])

    ada = await artist("Ada")
    bea = await artist("Bea")
    series = [
        (ada, "spotify", "Monthly listeners", 100, ten),
        (ada, "spotify", "Monthly listeners", 999, three),
        (ada, "spotify", "Monthly listeners", 110, latest),
        (ada, "spotify", "Followers", 1000, eight),
        (ada, "spotify", "Followers", 1040, latest),
        (ada, "instagram", "Followers", 50, three),
        (ada, "instagram", "Followers", 80, latest),
        (ada, "instagram", "Followers", None, _stamp(days=1)),
        (ada, "tiktok", "Followers", 1000, eight),
        (ada, "tiktok", "Followers", 900, latest),
        (ada, "tiktok", "Likes", 100, ten),
        (ada, "tiktok", "Likes", 200, six),
        (ada, "tiktok", "Likes", 250, latest),
        (ada, "youtube", "Subscribers", 300, almost),
        (ada, "youtube", "Subscribers", 330, latest),
        (ada, "deezer", "Fans", 0, eight),
        (ada, "deezer", "Fans", 10, latest),
    ]
    for artist_id, source, metric, value, captured in series:
        text = "1.2M" if value is None else str(value)
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artist_id, source, metric, value, text, captured),
        )
    releases = [
        ("timed", "Timed EP", "ep", "2026-11-13T07:00:00Z", "time"),
        ("day", "Day Single", "single", "2026-11-12", "day"),
        ("boundary", "Boundary Day", "single", "2026-11-08", "day"),
        ("dawn", "Dawn Single", "single", "2026-11-08T07:00:00Z", "time"),
        ("later", "Later Clock", "single", "2026-11-15T18:00:00Z", "time"),
        ("old", "Old Album", "album", "2026-11-01", "day"),
        ("month", "Month Album", "album", "2026-11", "month"),
    ]
    for mbid, title, kind, released, precision in releases:
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ada, mbid, title, kind, released, precision, latest, latest),
        )
    charts = [
        (ada, "New Single", "2026-11-14", 4, latest),
        (ada, "Old Single", "2026-11-01", 9, ten),
        (ada, "Old Single", "2026-11-14", 9, latest),
        (bea, "Bea Hit", "2026-11-14", 2, latest),
    ]
    for artist_id, title, day, position, collected in charts:
        await database.execute(
            """
            INSERT INTO chart_entries (
                chart_source, territory, chart_date, position, track_or_album_name,
                artist_name, artist_id, collected_at
            ) VALUES ('itunes_topsongs', 'us', ?, ?, ?, 'Ada', ?, ?)
            """,
            (day, position, title, artist_id, collected),
        )
    await database.execute(
        """
        INSERT INTO playlist_appearances (
            artist_id, playlist_id, playlist_name, owner_name, position, first_seen_at, last_seen_at, source
        ) VALUES (?, '998877', 'Fresh Hits', 'Editor', 3, ?, ?, 'deezer'),
                 (?, '111', 'Old Hits', 'Editor', 1, ?, ?, 'deezer')
        """,
        (ada, _stamp(days=2), latest, ada, _stamp(days=30), latest),
    )
    await database.execute(
        """
        INSERT INTO metric_alerts (
            artist_id, provider, metric, window, delta, delta_pct,
            baseline_value, latest_value, baseline_at, latest_at, detected_at, dedupe_key
        ) VALUES (?, 'spotify', 'Followers', '7d', 40, 40, 1000, 1040, ?, ?, ?, 'ada-recent'),
                 (?, 'spotify', 'Followers', '7d', 5, 5, 100, 105, ?, ?, ?, 'ada-old'),
                 (?, 'spotify', 'Followers', '24h', 9, 9, 50, 59, ?, ?, ?, 'bea-recent')
        """,
        (
            ada, eight, latest, _stamp(days=1),
            ada, ten, eight, _stamp(days=20),
            bea, eight, latest, _stamp(days=1),
        ),
    )
    await database.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES ('fan@example.com', 'x', ?)",
        (ten,),
    )
    user = await database.query_one("SELECT id FROM users WHERE email = 'fan@example.com'")
    await database.execute(
        "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, ?)",
        (int(user["id"]), ada, latest),
    )
    return {
        "ada": ada,
        "bea": bea,
        "user_id": int(user["id"]),
        "ten": ten,
        "six": six,
        "eight": eight,
        "latest": latest,
    }


def test_digest_history_releases_charts_playlists_alerts_and_threshold(tmp_path):
    database = SqliteDatabase(str(tmp_path / "history.sqlite"))
    guard = _ReadOnly(database)
    seeded = asyncio.run(_seed_history(database))

    async def load(threshold):
        return await build_digest(guard, user_id=seeded["user_id"], threshold=threshold, now=NOW)

    view = asyncio.run(load("5"))
    assert guard.writes == 0, "digest must not write"
    assert view["failed"] is False
    assert view["source"] == "watchlist"
    assert [artist["name"] for artist in view["artists"]] == ["Ada"]
    doc = json.loads(render_digest_json(view))
    assert doc["window_days"] == 7
    assert doc["baseline_min_days"] == 6
    assert doc["threshold_percent"] == 5
    assert "999" not in render_digest_json(view)
    assert "1.2M" not in render_digest_json(view)
    assert "1200000" not in render_digest_json(view)

    listeners = _metric(doc, "spotify", "Monthly listeners")
    assert listeners["latest_value"] == 110
    assert listeners["latest_at"] == seeded["latest"]
    assert listeners["baseline_value"] == 100
    assert listeners["baseline_at"] == seeded["ten"]
    assert listeners["change"] == 10
    assert listeners["percent"] == "+10%"
    assert listeners["status"] == "ok"
    assert listeners["spike"] is True

    followers = _metric(doc, "spotify", "Followers")
    assert followers["change"] == 40
    assert followers["percent"] == "+4%"
    assert followers["spike"] is False

    instagram = _metric(doc, "instagram", "Followers")
    assert instagram["latest_value"] == 80
    assert instagram["baseline_value"] is None
    assert instagram["status"] == "missing"
    assert instagram["text"] == "not enough history"
    assert instagram["spike"] is False

    dropped = _metric(doc, "tiktok", "Followers")
    assert dropped["change"] == -100
    assert dropped["percent"] == "-10%"
    assert dropped["spike"] is True

    likes = _metric(doc, "tiktok", "Likes")
    assert likes["baseline_value"] == 200
    assert likes["baseline_at"] == seeded["six"]
    assert likes["latest_value"] == 250
    assert likes["change"] == 50
    assert likes["percent"] == "+25%"
    assert likes["spike"] is True

    youtube = _metric(doc, "youtube", "Subscribers")
    assert youtube["latest_value"] == 330
    assert youtube["baseline_value"] is None
    assert youtube["text"] == "not enough history"
    assert youtube["status"] == "missing"

    fans = _metric(doc, "deezer", "Fans")
    assert fans["baseline_value"] == 0
    assert fans["latest_value"] == 10
    assert fans["change"] == 10
    assert fans["percent"] is None
    assert fans["text"] == "+10"
    assert fans["spike"] is False

    spike_metrics = {item["metric"] + ":" + item["source"] for item in doc["overview"]["spikes"]}
    assert spike_metrics == {
        "Monthly listeners:spotify",
        "Followers:tiktok",
        "Likes:tiktok",
    }

    ada = doc["artists"][0]
    titles = {item["title"]: item for item in ada["releases"]}
    assert set(titles) == {"Timed EP", "Day Single", "Boundary Day"}
    assert titles["Timed EP"]["released_at"] == "2026-11-13T07:00:00Z"
    assert titles["Timed EP"]["date_only"] is False
    assert titles["Timed EP"]["note"] == "Exact UTC time."
    assert titles["Day Single"]["released_at"] == "2026-11-12"
    assert titles["Day Single"]["date_only"] is True
    assert "T" not in titles["Day Single"]["released_at"]
    assert titles["Day Single"]["note"] == "Date only. No clock time is stored."
    assert titles["Boundary Day"]["released_at"] == "2026-11-08"
    assert titles["Boundary Day"]["date_only"] is True

    assert [item["title"] for item in ada["charts"]] == ["New Single"]
    assert ada["charts"][0]["position"] == 4
    assert ada["charts"][0]["chart_date"] == "2026-11-14"
    assert [item["playlist_name"] for item in ada["playlists"]] == ["Fresh Hits"]
    assert ada["playlists"][0]["first_seen_at"] == _stamp(days=2)
    assert ada["playlists"][0]["url"] == "https://www.deezer.com/playlist/998877"
    assert len(ada["alerts"]) == 1
    assert ada["alerts"][0]["detected_at"] == _stamp(days=1)
    assert ada["alerts"][0]["percent"] == "+4%"

    wider = json.loads(render_digest_json(asyncio.run(load("4"))))
    assert _metric(wider, "spotify", "Followers")["spike"] is True
    quiet = json.loads(render_digest_json(asyncio.run(load("30"))))
    assert quiet["overview"]["spikes"] == []
    assert quiet["threshold_percent"] == 30
    assert guard.writes == 0

    rows = list(csv.DictReader(io.StringIO(render_digest_csv(view))))
    listener_row = next(row for row in rows if row["section"] == "metric" and row["metric"] == "Monthly listeners")
    assert listener_row["artist"] == "Ada"
    assert listener_row["artist_set"] == "watchlist"
    assert listener_row["latest_value"] == "110"
    assert listener_row["baseline_value"] == "100"
    assert listener_row["change"] == "10"
    assert listener_row["percent"] == "+10%"
    assert listener_row["spike"] == "yes"
    missing = next(row for row in rows if row["section"] == "metric" and row["provider"] == "instagram")
    assert missing["status"] == "missing"
    assert missing["detail"] == "not enough history"
    assert missing["baseline_value"] == ""
    assert any(row["section"] == "release" and row["title"] == "Timed EP" and row["occurred_at"] == "2026-11-13T07:00:00Z" for row in rows)
    assert any(row["section"] == "release" and row["title"] == "Day Single" and row["detail"] == "Date only. No clock time is stored." for row in rows)
    assert not any(row["title"] in {"Month Album", "Old Album", "Dawn Single", "Later Clock", "Old Single", "Old Hits", "Bea Hit"} for row in rows)
    assert any(row["section"] == "spike" and row["metric"] == "Likes" for row in rows)
    assert any(row["section"] == "alert" and row["occurred_at"] == _stamp(days=1) for row in rows)
    assert not any(row["section"] == "alert" and row["occurred_at"] == _stamp(days=20) for row in rows)


async def _add_artist(database, name: str, listeners, *, tracked: int = 1, captured: str = "2026-11-15T00:00:00Z"):
    await database.execute(
        "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', ?, ?)",
        (name, name.casefold(), tracked, captured),
    )
    row = await database.query_one("SELECT id FROM artists WHERE name = ?", (name,))
    artist_id = int(row["id"])
    if listeners is not None:
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Monthly listeners', ?, ?, ?)
            """,
            (artist_id, listeners, str(listeners), captured),
        )
    return artist_id


def test_digest_uses_the_watchlist_or_the_tracked_fallback(tmp_path):
    database = SqliteDatabase(str(tmp_path / "pick.sqlite"))

    async def seed():
        await migrate(database)
        quiet = await _add_artist(database, "Quiet", 10)
        loud = await _add_artist(database, "Loud", 9000)
        bogus = await _add_artist(database, "Bogus", None)
        hidden = await _add_artist(database, "Hidden", 999999, tracked=0)
        await database.execute(
            """
            INSERT INTO metric_latest (artist_id, source, metric, value, value_text, captured_at, snapshot_id)
            VALUES (?, 'spotify', 'Monthly listeners', 1500.5, '1500.5', '2026-11-15T00:00:00Z', NULL)
            """,
            (bogus,),
        )
        await database.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES ('fan@example.com', 'x', '2026-11-01T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES ('empty@example.com', 'x', '2026-11-01T00:00:00Z')"
        )
        fan = await database.query_one("SELECT id FROM users WHERE email = 'fan@example.com'")
        empty = await database.query_one("SELECT id FROM users WHERE email = 'empty@example.com'")
        await database.execute(
            "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, '2026-11-14T00:00:00Z')",
            (int(fan["id"]), quiet),
        )
        return int(fan["id"]), int(empty["id"]), quiet, loud, hidden

    fan_id, empty_id, quiet, loud, hidden = asyncio.run(seed())
    watched = asyncio.run(build_digest(database, user_id=fan_id, now=NOW))
    assert watched["source"] == "watchlist"
    assert [artist["name"] for artist in watched["artists"]] == ["Quiet"]
    assert "top tracked artists" not in watched["source_note"]
    assert loud not in {artist["id"] for artist in watched["artists"]}

    anonymous = asyncio.run(build_digest(database, user_id=None, now=NOW))
    assert anonymous["source"] == "tracked"
    assert anonymous["artists"][0]["name"] == "Loud"
    assert {artist["name"] for artist in anonymous["artists"]} == {"Loud", "Quiet"}
    assert "No watchlist is available" in anonymous["source_note"]
    assert "top tracked artists" in anonymous["source_note"]
    assert "Bogus" not in anonymous["source_note"]
    assert hidden not in {artist["id"] for artist in anonymous["artists"]}
    assert "Bogus" not in {artist["name"] for artist in anonymous["artists"]}
    assert "Hidden" not in {artist["name"] for artist in anonymous["artists"]}

    vacant = asyncio.run(build_digest(database, user_id=empty_id, now=NOW))
    assert vacant["source"] == "tracked"
    assert vacant["artists"][0]["name"] == "Loud"
    assert "watchlist is empty" in vacant["source_note"]
    assert "top tracked artists" in vacant["source_note"]


def test_digest_caps_the_artist_list(tmp_path):
    database = SqliteDatabase(str(tmp_path / "cap.sqlite"))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES ('fan@example.com', 'x', '2026-10-01T00:00:00Z')"
        )
        user = await database.query_one("SELECT id FROM users WHERE email = 'fan@example.com'")
        user_id = int(user["id"])
        for index in range(1, 14):
            artist_id = await _add_artist(database, f"Cap {index:02d}", index)
            await database.execute(
                "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, ?)",
                (user_id, artist_id, f"2026-10-{index:02d}T00:00:00Z"),
            )
        return user_id

    user_id = asyncio.run(seed())
    watched = asyncio.run(build_digest(database, user_id=user_id, now=NOW))
    names = [artist["name"] for artist in watched["artists"]]
    assert watched["source"] == "watchlist"
    assert len(names) == 12
    assert names[0] == "Cap 13"
    assert names[-1] == "Cap 02"
    assert "Cap 01" not in names

    ranked = asyncio.run(build_digest(database, user_id=None, now=NOW))
    ranked_names = [artist["name"] for artist in ranked["artists"]]
    assert ranked["source"] == "tracked"
    assert len(ranked_names) == 10
    assert ranked_names[0] == "Cap 13"
    assert "Cap 04" in ranked_names
    assert "Cap 03" not in ranked_names
    assert "Cap 01" not in ranked_names


def test_rounded_public_counts_follow_the_display_step():
    assert public_count_is_rounded(1_400_000) is True
    assert public_count_is_rounded(1_500_000) is True
    assert public_count_is_rounded(1_432_100) is False
    assert public_count_is_rounded(10_000) is True
    assert public_count_is_rounded(10_050) is False
    assert public_count_is_rounded(100_200) is True
    assert public_count_is_rounded(101_000) is True
    assert public_count_is_rounded(9_999) is False
    assert public_count_is_rounded(1_000) is False
    assert public_count_is_rounded(True) is False


def test_rounded_tiktok_follower_step_is_not_a_spike(tmp_path):
    """1.4M to 1.5M is a display step. An exact count above the threshold still flags."""
    database = SqliteDatabase(str(tmp_path / "rounded.sqlite"))

    async def seed():
        await migrate(database)
        ten = _stamp(days=10)
        latest = _stamp()

        async def artist(name: str) -> int:
            await database.execute(
                "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', 1, ?)",
                (name, name.casefold(), ten),
            )
            row = await database.query_one("SELECT id FROM artists WHERE name = ?", (name,))
            return int(row["id"])

        rounded = await artist("Round")
        exact = await artist("Exact")
        series = [
            (rounded, "tiktok", "Followers", 1_400_000, "1.4M", ten),
            (rounded, "tiktok", "Followers", 1_500_000, "1.5M", latest),
            (exact, "tiktok", "Followers", 10234, "10.2K", ten),
            (exact, "tiktok", "Followers", 12345, "12.3K", latest),
        ]
        for artist_id, source, metric, value, text, captured in series:
            await database.execute(
                """
                INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (artist_id, source, metric, value, text, captured),
            )
        await database.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES ('fan@example.com', 'x', ?)",
            (ten,),
        )
        user = await database.query_one("SELECT id FROM users WHERE email = 'fan@example.com'")
        user_id = int(user["id"])
        await database.execute(
            "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, ?), (?, ?, ?)",
            (user_id, exact, latest, user_id, rounded, ten),
        )
        return user_id

    user_id = asyncio.run(seed())
    guard = _ReadOnly(database)
    view = asyncio.run(build_digest(guard, user_id=user_id, threshold="5", now=NOW))
    assert guard.writes == 0
    doc = json.loads(render_digest_json(view))
    rounded = _metric(
        {"artists": [artist for artist in doc["artists"] if artist["name"] == "Round"]},
        "tiktok",
        "Followers",
    )
    exact = _metric(
        {"artists": [artist for artist in doc["artists"] if artist["name"] == "Exact"]},
        "tiktok",
        "Followers",
    )
    assert rounded["latest_value"] == 1_500_000
    assert rounded["baseline_value"] == 1_400_000
    assert rounded["change"] == 100_000
    assert rounded["approximate"] is True
    assert rounded["spike"] is False
    assert exact["latest_value"] == 12345
    assert exact["baseline_value"] == 10234
    assert exact["change"] == 2111
    assert exact["approximate"] is False
    assert exact["spike"] is True
    spike_names = {item["artist_name"] for item in doc["overview"]["spikes"] if item["source"] == "tiktok"}
    assert spike_names == {"Exact"}
    rows = list(csv.DictReader(io.StringIO(render_digest_csv(view))))
    rounded_row = next(row for row in rows if row["section"] == "metric" and row["artist"] == "Round" and row["metric"] == "Followers")
    exact_row = next(row for row in rows if row["section"] == "metric" and row["artist"] == "Exact" and row["metric"] == "Followers")
    assert rounded_row["approximate"] == "true"
    assert rounded_row["spike"] == "no"
    assert exact_row["approximate"] == "false"
    assert exact_row["spike"] == "yes"
    assert "1.4M" not in render_digest_json(view)


def test_rounded_tiktok_note_is_on_the_digest_page(monkeypatch, tmp_path):
    path = tmp_path / "rounded-page.sqlite"
    database = SqliteDatabase(str(path))
    moment = datetime.now(timezone.utc)
    older = utc_now(moment - timedelta(days=10))
    latest = utc_now(moment - timedelta(hours=1))

    async def seed():
        await migrate(database)
        round_id = await _add_artist(database, "Round", 8000, captured=older)
        exact_id = await _add_artist(database, "Exact", 7000, captured=older)
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'tiktok', 'Followers', 1400000, '1.4M', ?),
                   (?, 'tiktok', 'Followers', 1500000, '1.5M', ?),
                   (?, 'tiktok', 'Followers', 10234, '10.2K', ?),
                   (?, 'tiktok', 'Followers', 12345, '12.3K', ?)
            """,
            (round_id, older, round_id, latest, exact_id, older, exact_id, latest),
        )

    asyncio.run(seed())
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    page = client.get("/digest")
    body = client.get("/digest.json").json()
    downloaded = client.get("/digest.csv")
    assert page.status_code == 200
    assert "approximate (rounded public count)" in page.text
    assert 'data-approximate="yes"' in page.text
    rounded = next(artist for artist in body["artists"] if artist["name"] == "Round")
    followers = next(item for item in rounded["metrics"] if item["source"] == "tiktok" and item["metric"] == "Followers")
    assert followers["approximate"] is True
    assert followers["spike"] is False
    exact = next(artist for artist in body["artists"] if artist["name"] == "Exact")
    exact_followers = next(item for item in exact["metrics"] if item["source"] == "tiktok" and item["metric"] == "Followers")
    assert exact_followers["approximate"] is False
    assert exact_followers["spike"] is True
    assert "not flagged" in page.text
    parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
    rounded_row = next(row for row in parsed if row["artist"] == "Round" and row["metric"] == "Followers")
    assert rounded_row["approximate"] == "true"
    assert rounded_row["spike"] == "no"
    reset_pool()


def test_digest_read_failure_is_not_a_raise():
    class Boom:
        async def query(self, *args, **kwargs):
            raise RuntimeError("nope")

        async def query_batch(self, *args, **kwargs):
            raise RuntimeError("nope")

    view = asyncio.run(build_digest(Boom(), user_id=4, now=NOW))
    assert view["failed"] is True
    assert view["read_error"] == DIGEST_READ_ERROR
    assert view["artists"] == []


def test_digest_routes_require_login_and_export_the_same_rows(monkeypatch, tmp_path):
    _reset_memory_users()
    locked = TestClient(main.app)
    for path in ("/digest", "/digest.csv", "/digest.json"):
        response = locked.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    path = tmp_path / "routes.sqlite"
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        moment = datetime.now(timezone.utc)
        public = await _add_artist(database, "Public Star", None)
        recent = await _add_artist(database, "Recent Only", None)
        ten = utc_now(moment - timedelta(days=10))
        day_ago = utc_now(moment - timedelta(days=1))
        latest = utc_now(moment - timedelta(hours=1))
        for artist_id, older, newer, previous, current in (
            (public, ten, latest, 100, 200),
            (recent, day_ago, latest, 40, 50),
        ):
            await database.execute(
                """
                INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
                VALUES (?, 'spotify', 'Monthly listeners', ?, ?, ?),
                       (?, 'spotify', 'Monthly listeners', ?, ?, ?)
                """,
                (artist_id, previous, str(previous), older, artist_id, current, str(current), newer),
            )

    asyncio.run(seed())
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("digest must not call providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    pooled = _POOL[str(path)]
    before = pooled.conn.total_changes
    page = client.get("/digest")
    downloaded = client.get("/digest.csv")
    payload = client.get("/digest.json")
    alerts = client.get("/alerts")
    assert pooled.conn.total_changes == before
    assert calls == []
    assert page.status_code == 200
    assert "Daily digest" in page.text
    assert "watchlist is empty" in page.text
    assert "top tracked artists" in page.text
    assert "Public Star" in page.text
    assert "not enough history" in page.text
    assert 'href="/digest"' in page.text
    assert "Showing artists on your watchlist" not in page.text
    assert downloaded.status_code == 200
    assert "text/csv" in downloaded.headers["content-type"]
    assert "not enough history" in downloaded.text
    assert "Public Star" in downloaded.text
    assert payload.status_code == 200
    assert payload.headers["content-type"].startswith("application/json")
    body = payload.json()
    assert body["source"] == "tracked"
    assert body["artists"][0]["name"] == "Public Star"
    listeners = _metric(body, "spotify", "Monthly listeners")
    assert listeners["change"] == 100
    assert listeners["spike"] is True
    recent = next(artist for artist in body["artists"] if artist["name"] == "Recent Only")
    assert recent["metrics"][0]["text"] == "not enough history"
    assert "Daily digest" in alerts.text
    reset_pool()


def test_memory_demo_login_uses_the_tracked_fallback(monkeypatch, tmp_path):
    """Demo can sign in with auth memory. That session has no D1 watchlist."""
    _reset_memory_users()
    path = tmp_path / "memory.sqlite"
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await _add_artist(database, "Public Star", 9000)
        secret = await _add_artist(database, "Secret Club", 5)
        await database.execute(
            "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (1, ?, '2026-11-14T00:00:00Z')",
            (secret,),
        )

    asyncio.run(seed())
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
    reset_pool()
    client = TestClient(main.app)
    logged = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    page = client.get("/digest")
    assert page.status_code == 200
    assert "No watchlist is available" in page.text
    assert "top tracked artists" in page.text
    assert "Public Star" in page.text
    assert "Showing artists on your watchlist" not in page.text
    body = client.get("/digest.json").json()
    assert body["source"] == "tracked"
    assert body["artists"][0]["name"] == "Public Star"
    reset_pool()


def test_registered_watchlist_is_what_the_page_reads(monkeypatch, tmp_path):
    _reset_memory_users()
    path = tmp_path / "watch.sqlite"
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        quiet = await _add_artist(database, "Quiet", 10)
        await _add_artist(database, "Loud", 9000)
        return quiet

    quiet = asyncio.run(seed())
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    client = TestClient(main.app)
    created = client.post(
        "/register",
        data={"email": "fan@example.com", "password": "secret1", "artist_name": "Fan"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    watched = client.post(f"/watch/{quiet}", follow_redirects=False)
    assert watched.status_code == 303
    pooled = _POOL[str(path)]
    before = pooled.conn.total_changes
    page = client.get("/digest")
    body = client.get("/digest.json").json()
    assert pooled.conn.total_changes == before
    assert page.status_code == 200
    assert "Showing artists on your watchlist" in page.text
    assert "top tracked artists" not in page.text
    assert "Quiet" in page.text
    assert "Loud" not in page.text
    assert [artist["name"] for artist in body["artists"]] == ["Quiet"]
    assert body["source"] == "watchlist"
    reset_pool()


def test_digest_timeout_degrades(monkeypatch, tmp_path):
    _reset_memory_users()
    path = tmp_path / "slow.sqlite"
    asyncio.run(migrate(SqliteDatabase(str(path))))
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.2)
        raise AssertionError("digest should have been cancelled")

    monkeypatch.setattr(main, "GROWTH_READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(main, "build_digest", slow)
    client = TestClient(main.app)
    client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    page = client.get("/digest")
    downloaded = client.get("/digest.csv")
    payload = client.get("/digest.json")
    assert page.status_code == 200
    assert "Data temporarily unavailable" in page.text
    assert downloaded.status_code == 503
    assert "Data temporarily unavailable" in downloaded.text
    assert payload.status_code == 503
    assert payload.json()["read_error"].startswith("Data temporarily unavailable")
    reset_pool()
