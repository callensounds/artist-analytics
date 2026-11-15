"""Artist comparison. Stored rows only: no estimates and no writes on the page."""

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from compare import (
    D1_COMPOUND_TERM_LIMIT,
    D1_PATTERN_LIMIT,
    NOT_ENOUGH,
    UNAVAILABLE,
    _baseline_statements,
    compare_sql_catalog,
    compound_select_terms,
    growth_delta,
    like_glob_patterns,
    normalise_growth,
    timing_role,
)
from crawl import reset_pool
from db import SqliteDatabase, migrate, utc_now
from scores import score_from_counts


def setup_function():
    main.USERS.clear()
    main.USERS["artist@demo.com"] = {
        "id": 1,
        "email": "artist@demo.com",
        "password_hash": "",
        "artist_name": "Demo Artist",
    }
    main.NEXT_ID = 2


def _at(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def test_timing_role_does_not_invent_an_hour():
    assert timing_role("time", "2026-08-23T07:00:00Z") == "time"
    assert timing_role("time", "2026-08-23") == "day"
    assert timing_role("day", "2026-08-23T07:00:00Z") == "day"
    assert timing_role("day", "2026-08-23") == "day"
    assert timing_role("month", "2026-08") == "skip"
    assert timing_role("year", "2026") == "skip"
    assert timing_role("month", "2026-08-23") == "skip"
    assert timing_role("time", "2026-02-31T07:00:00Z") == "skip"
    assert timing_role("day", "2026-02-31") == "skip"
    assert timing_role("time", "") == "skip"


def test_growth_delta_needs_both_stored_endpoints():
    later = {"value": 150, "captured_at": "2026-11-15T00:00:00Z"}
    earlier = {"value": 100, "captured_at": "2026-11-01T00:00:00Z"}
    found = growth_delta(later, earlier)
    assert found["status"] == "ok"
    assert found["text"] == "+50 (+50%)"
    assert found["from"] == earlier["captured_at"]
    assert found["to"] == later["captured_at"]
    assert growth_delta(later, None)["text"] == NOT_ENOUGH
    assert growth_delta(None, earlier)["text"] == NOT_ENOUGH
    assert growth_delta(later, {"value": 100, "captured_at": later["captured_at"]})["text"] == NOT_ENOUGH
    assert growth_delta(earlier, later)["text"] == NOT_ENOUGH
    zero = growth_delta(
        {"value": 5, "captured_at": "2026-11-15T00:00:00Z"},
        {"value": 0, "captured_at": "2026-11-01T00:00:00Z"},
    )
    assert zero["text"] == "+5"
    assert "(" not in zero["text"]
    assert growth_delta({"value": True, "captured_at": "2026-11-15T00:00:00Z"}, earlier)["text"] == NOT_ENOUGH
    assert growth_delta({"value": "1,200", "captured_at": "2026-11-15T00:00:00Z"}, earlier)["text"] == NOT_ENOUGH


def test_normalise_growth_starts_at_the_first_common_snapshot():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t10 = datetime(2026, 1, 11, tzinfo=timezone.utc)
    t20 = datetime(2026, 1, 21, tzinfo=timezone.utc)
    plotted = normalise_growth(
        {
            1: [
                {"at": t20, "value": 180},
                {"at": t0, "value": 100},
                {"at": t10, "value": 150},
            ],
            2: [{"at": t10, "value": 200}, {"at": t20, "value": 220}],
        }
    )
    assert [point["at"] for point in plotted[1]] == [t10, t20]
    assert [point["value"] for point in plotted[1]] == [150, 180]
    assert [point["tenths"] for point in plotted[1]] == [0, 200]
    assert plotted[1][0]["baseline"] == 150
    assert [point["tenths"] for point in plotted[2]] == [0, 100]
    assert t0 not in {point["at"] for point in plotted[1]}
    # Artist 1 has no stored point invented at a time they were not observed.
    assert all(point["value"] in {150, 180} for point in plotted[1])

    gap = normalise_growth(
        {
            1: [{"at": t0, "value": 100}, {"at": t20, "value": 180}],
            2: [{"at": t10, "value": 200}, {"at": t20, "value": 250}],
        }
    )
    assert [point["at"] for point in gap[1]] == [t20]
    assert gap[1][0]["value"] == 180
    assert gap[1][0]["tenths"] == 0
    assert [point["tenths"] for point in gap[2]] == [0, 250]

    assert normalise_growth({1: [{"at": t0, "value": 0}, {"at": t10, "value": 5}]}) == {}
    single = normalise_growth({1: [{"at": t0, "value": 40}]})
    assert len(single[1]) == 1
    assert single[1][0]["tenths"] == 0
    skipped = normalise_growth({1: [{"at": t0, "value": True}, {"at": t10, "value": 12}]})
    assert [point["value"] for point in skipped[1]] == [12]
    naive = datetime(2026, 1, 1)
    deduped = normalise_growth(
        {1: [{"at": naive, "value": 5, "id": 1}, {"at": t0, "value": 10, "id": 2}]}
    )
    assert len(deduped[1]) == 1
    assert deduped[1][0]["value"] == 10


def test_compare_sql_stays_within_d1_limits():
    """D1 rejects a long UNION chain and a long GLOB character class."""
    statements = compare_sql_catalog([4, 6, 8, 9])
    assert len(statements) > 10
    for sql in statements:
        assert compound_select_terms(sql) <= D1_COMPOUND_TERM_LIMIT
        assert "UNION" not in sql.upper()
        for pattern in like_glob_patterns(sql):
            assert len(pattern) <= D1_PATTERN_LIMIT
        assert "GLOB" not in sql.upper()
        assert " LIKE " not in f" {sql.upper()} "


def test_compare_requires_login():
    client = TestClient(main.app)
    page = client.get("/compare", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    downloaded = client.get("/compare.csv", follow_redirects=False)
    assert downloaded.status_code == 303
    assert downloaded.headers["location"] == "/login"


def _seed(database: SqliteDatabase):
    now = datetime.now(timezone.utc)

    def ago(**delta) -> str:
        return utc_now(now - timedelta(**delta))

    async def ready():
        await migrate(database)
        names = ("Ada", "Bea", "Cy", "Nia")
        for name in names:
            await database.execute(
                "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', 1, ?)",
                (name, name.casefold(), ago(days=40)),
            )
        rows = await database.query("SELECT id, normalized_name FROM artists ORDER BY id")
        ids = {row["normalized_name"]: int(row["id"]) for row in rows}
        ada = ids["ada"]
        bea = ids["bea"]
        stamps = {
            "d30": ago(days=30),
            "d10": ago(days=10),
            "d3": ago(days=3),
            "h1": ago(hours=1),
        }
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'spotify', 'Monthly listeners', 100, '100', ?),
                   (?, 'spotify', 'Monthly listeners', 150, '150', ?),
                   (?, 'spotify', 'Monthly listeners', 180, '180', ?),
                   (?, 'spotify', 'Monthly listeners', 200, '200', ?),
                   (?, 'spotify', 'Followers', 50, '50', ?),
                   (?, 'deezer', 'Fans', 40, '40', ?),
                   (?, 'instagram', 'Followers', NULL, '1.2M', ?),
                   (?, 'spotify', 'Followers', 5, '5', ?)
            """,
            (
                ada, stamps["d30"],
                ada, stamps["d10"],
                ada, stamps["d3"],
                ada, stamps["h1"],
                ada, stamps["h1"],
                ada, stamps["h1"],
                ada, stamps["h1"],
                bea, stamps["h1"],
            ),
        )
        inside_90 = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        inside_365 = (now - timedelta(days=100)).strftime("%Y-%m-%d")
        outside = (now - timedelta(days=400)).strftime("%Y-%m-%d")
        month = (now - timedelta(days=10)).strftime("%Y-%m")
        clock_21 = ago(days=5)[:11] + "21:00:00Z"
        clock_07 = ago(days=2)[:11] + "07:00:00Z"
        clock_15 = ago(days=2)[:11] + "15:00:00Z"
        date_only = (now - timedelta(days=4)).strftime("%Y-%m-%d")
        for mbid, title, released, precision in (
            ("mb-90", "Recent", inside_90, "day"),
            ("mb-365", "Season", inside_365, "day"),
            ("mb-old", "Old", outside, "day"),
            ("mb-month", "Month", month, "month"),
            ("mb-time", "Clock", clock_21, "time"),
        ):
            await database.execute(
                """
                INSERT INTO musicbrainz_releases (
                    artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, 'Single', ?, ?, ?, ?)
                """,
                (ada, mbid, title, released, precision, stamps["h1"], stamps["h1"]),
            )
        await database.execute(
            """
            INSERT INTO content_sightings (
                artist_id, source, external_id, title, kind, published_at, precision, first_seen_at
            ) VALUES (?, 'apple', 'exact7', 'Drop', 'release', ?, 'time', ?),
                   (?, 'instagram', 'post15', 'Post', 'post', ?, 'time', ?),
                   (?, 'apple', 'dateonly', 'Day label', 'release', ?, 'time', ?)
            """,
            (ada, clock_07, stamps["h1"], ada, clock_15, stamps["h1"], ada, date_only, stamps["h1"]),
        )
        for playlist_id, removed in (("10", None), ("11", None), ("12", stamps["h1"])):
            await database.execute(
                """
                INSERT INTO playlist_appearances (
                    artist_id, playlist_id, playlist_name, owner_name, follower_count, position,
                    first_seen_at, last_seen_at, source, editorial, removed_at
                ) VALUES (?, ?, ?, 'Deezer', NULL, NULL, ?, ?, 'deezer', 1, ?)
                """,
                (ada, playlist_id, f"List {playlist_id}", stamps["d30"], stamps["h1"], removed),
            )
        await database.execute(
            """
            INSERT INTO playlist_crawls (artist_id, source, crawled_at, absences_marked_at)
            VALUES (?, 'deezer', ?, ?),
                   (?, 'spotify_tracks', ?, ?),
                   (?, 'deezer_tracks', ?, ?)
            """,
            (ada, stamps["h1"], stamps["h1"], ada, stamps["h1"], stamps["h1"], ada, stamps["h1"], stamps["h1"]),
        )
        await database.execute(
            """
            INSERT INTO artist_tracks (
                artist_id, source, external_id, track_name, popularity, album_name, rank,
                first_seen_at, last_seen_at, removed_at
            ) VALUES (?, 'spotify', 'gone', 'Gone', NULL, NULL, 1, ?, ?, ?),
                   (?, 'spotify', 'north', 'North', NULL, NULL, 2, ?, ?, NULL),
                   (?, 'deezer', 'south', 'South', NULL, NULL, 1, ?, ?, NULL)
            """,
            (
                ada, stamps["d30"], stamps["h1"], stamps["h1"],
                ada, stamps["d30"], stamps["h1"],
                ada, stamps["d30"], stamps["h1"],
            ),
        )
        await database.execute(
            """
            INSERT INTO track_stats (artist_id, source, external_id, metric, value, captured_at)
            VALUES (?, 'spotify', 'gone', 'spotify_popularity', 999991, ?),
                   (?, 'spotify', 'gone', 'spotify_playcount', 111111, ?),
                   (?, 'spotify', 'north', 'spotify_popularity', 73, ?),
                   (?, 'spotify', 'north', 'spotify_playcount', 4567, ?),
                   (?, 'deezer', 'south', 'deezer_rank', 8888, ?)
            """,
            (ada, stamps["h1"], ada, stamps["h1"], ada, stamps["h1"], ada, stamps["h1"], ada, stamps["h1"]),
        )
        listeners = []
        for sql, params in _baseline_statements([ada], ago(days=7)):
            prior = await database.query(sql, params)
            listeners.extend(row for row in prior if row["metric"] == "Monthly listeners")
        assert len(listeners) == 1
        assert int(listeners[0]["value"]) == 150
        assert listeners[0]["captured_at"] == stamps["d10"]
        return ids, stamps

    return asyncio.run(ready())


def test_compare_page_reads_stored_values_and_does_not_write(monkeypatch, tmp_path):
    path = tmp_path / "compare.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    try:
        ids, stamps = _seed(database)
        ada, bea, cy, nia = ids["ada"], ids["bea"], ids["cy"], ids["nia"]
        calls = []

        async def fan_out(*args, **kwargs):
            calls.append("collect")
            raise AssertionError("compare must not call providers")

        monkeypatch.setattr(main, "collect_artist", fan_out)
        client = TestClient(main.app)
        locked = client.get("/compare", params={"a": ada, "b": bea}, follow_redirects=False)
        assert locked.status_code == 303
        assert locked.headers["location"] == "/login"
        client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
        user = asyncio.run(database.query_one("SELECT id FROM users WHERE email = ?", ("artist@demo.com",)))
        asyncio.run(
            database.execute(
                "INSERT INTO user_artists (user_id, artist_id, created_at) VALUES (?, ?, ?)",
                (int(user["id"]), bea, stamps["h1"]),
            )
        )
        writes = []
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
            head = sql.lstrip().split(None, 1)[0].lower()
            if head not in {"select", "with"}:
                writes.append(sql)
            return await original_query(self, sql, params)

        monkeypatch.setattr(SqliteDatabase, "execute", spy_execute)
        monkeypatch.setattr(SqliteDatabase, "batch", spy_batch)
        monkeypatch.setattr(SqliteDatabase, "query", spy_query)

        missing = client.get("/compare", params={"a": ada, "b": "no-such-artist"})
        assert missing.status_code == 200
        assert "Not tracked: no-such-artist" in missing.text
        assert "does not look them up" in missing.text
        assert 'data-metric="spotify-monthly-listeners"' not in missing.text
        assert 'name="q"' in missing.text

        page = client.get("/compare", params={"a": ada, "b": bea})
        assert page.status_code == 200
        assert "Compare" in page.text
        assert "does not call providers" in page.text
        assert "Nothing is estimated" in page.text
        assert "Ada" in page.text
        assert "Bea" in page.text
        assert "Cy" not in page.text
        assert "Nia" not in page.text
        assert "1.2M" not in page.text
        assert "999991" not in page.text
        assert "111111" not in page.text
        assert "Gone" not in page.text
        assert "+20" not in page.text
        assert f'data-metric="spotify-monthly-listeners" data-artist="{ada}" data-value="200"' in page.text
        assert f'data-at="{stamps["h1"]}"' in page.text
        assert f'data-metric="spotify-monthly-listeners" data-artist="{ada}" data-delta="7" data-value="+50 (+33.3%)"' in page.text
        assert f'data-metric="spotify-monthly-listeners" data-artist="{ada}" data-delta="28" data-value="+100 (+100%)"' in page.text
        assert stamps["d10"] in page.text
        assert f'data-metric="spotify-followers" data-artist="{ada}" data-value="50"' in page.text
        assert f'data-metric="spotify-followers" data-artist="{ada}" data-delta="7" data-value="{NOT_ENOUGH}"' in page.text
        assert f'data-metric="spotify-monthly-listeners" data-artist="{bea}" data-value="{UNAVAILABLE}"' in page.text
        assert f'data-metric="instagram-followers" data-artist="{ada}" data-value="{UNAVAILABLE}"' in page.text
        assert f'data-metric="youtube-subscribers" data-artist="{ada}" data-value="{UNAVAILABLE}"' in page.text
        assert f'data-metric="deezer-fans" data-artist="{ada}" data-value="40"' in page.text
        assert f'data-metric="spotify-popularity" data-artist="{ada}" data-value="73"' in page.text
        assert "North" in page.text
        assert f'data-metric="spotify-plays" data-artist="{ada}" data-value="4,567"' in page.text
        assert f'data-metric="deezer-rank" data-artist="{ada}" data-value="8,888"' in page.text
        assert f'data-metric="playlists" data-artist="{ada}" data-value="2"' in page.text
        assert f'data-metric="playlists" data-artist="{bea}" data-value="0"' in page.text
        assert f'data-metric="releases-90" data-artist="{ada}" data-value="2"' in page.text
        assert f'data-metric="releases-365" data-artist="{ada}" data-value="3"' in page.text
        ada_score = score_from_counts(
            [
                {"source": "spotify", "metric": "Monthly listeners", "value": 200},
                {"source": "spotify", "metric": "Followers", "value": 50},
                {"source": "deezer", "metric": "Fans", "value": 40},
            ]
        )
        assert f'data-metric="score" data-artist="{ada}" data-value="{ada_score["score_text"]}"' in page.text
        assert f'data-metric="score-rank" data-artist="{ada}" data-value="1"' in page.text
        assert f'data-metric="score-rank" data-artist="{bea}" data-value="2"' in page.text
        assert f'data-timing="{ada}" data-hour-total="3" data-weekday-total="7"' in page.text
        assert f'data-artist="{ada}" data-hour="0" data-count="0"' in page.text
        assert f'data-artist="{ada}" data-hour="7" data-count="1"' in page.text
        assert f'data-artist="{ada}" data-hour="15" data-count="1"' in page.text
        assert f'data-artist="{ada}" data-hour="21" data-count="1"' in page.text
        assert f'data-timing="{bea}" data-hour-total="0"' in page.text
        assert "No exact UTC timestamp is stored" in page.text
        assert "not given an hour" in page.text
        assert 'data-tenths="0"' in page.text
        assert 'data-tenths="1000"' in page.text
        assert "<svg" in page.text
        assert "compare-cards" in page.text
        assert "compare-table" in page.text
        assert f'href="/compare.csv?a={ada}&amp;b={bea}"' in page.text or f'href="/compare.csv?a={ada}&b={bea}"' in page.text

        three = client.get("/compare", params={"a": ada, "b": bea, "c": cy})
        assert three.status_code == 200
        assert "Cy" in three.text
        assert "Nia" not in three.text
        four = client.get("/compare", params={"a": ada, "b": bea, "c": cy, "d": nia, "e": 999})
        assert four.status_code == 200
        assert "Nia" in four.text
        assert 'name="e"' not in four.text

        slug = client.get("/compare", params={"a": "Ada", "b": bea}, follow_redirects=False)
        assert slug.status_code == 303
        assert slug.headers["location"] == f"/compare?a={ada}&b={bea}"
        duplicate = client.get("/compare", params={"a": ada, "b": ada}, follow_redirects=False)
        assert duplicate.status_code == 303
        assert duplicate.headers["location"] == f"/compare?a={ada}"

        picker = client.get("/compare", params={"q": "ada"})
        assert picker.status_code == 200
        assert f'href="/compare?a={ada}"' in picker.text
        assert "Add" in picker.text
        bare = client.get("/compare")
        assert f'href="/compare?a={bea}"' in bare.text
        assert "Watchlist" in bare.text
        assert "Tracked artists" in bare.text

        artist = client.get(f"/artist/{ada}")
        assert artist.status_code == 200
        assert f'href="/compare?a={ada}"' in artist.text
        assert 'href="/compare"' in artist.text
        watch = client.get("/watch")
        assert f'href="/compare?a={bea}"' in watch.text

        downloaded = client.get("/compare.csv", params={"a": ada, "b": bea})
        assert downloaded.status_code == 200
        assert "text/csv" in downloaded.headers["content-type"]
        assert 'filename="compare.csv"' in downloaded.headers["content-disposition"]
        parsed = list(csv.DictReader(io.StringIO(downloaded.text)))
        listeners = [
            row for row in parsed if row["metric"] == "Spotify monthly listeners" and row["artist_id"] == str(ada)
        ]
        assert listeners[0]["value"] == "200"
        assert listeners[0]["captured_at"] == stamps["h1"]
        assert listeners[0]["delta_7d"] == "+50 (+33.3%)"
        assert listeners[0]["delta_28d"] == "+100 (+100%)"
        instagram = [
            row for row in parsed if row["metric"] == "Instagram followers" and row["artist_id"] == str(ada)
        ]
        assert instagram[0]["value"] == UNAVAILABLE
        followers = [row for row in parsed if row["metric"] == "Spotify followers" and row["artist_id"] == str(ada)]
        assert followers[0]["delta_7d"] == NOT_ENOUGH
        assert "1.2M" not in downloaded.text
        assert "999991" not in downloaded.text
        one = client.get("/compare.csv", params={"a": ada})
        assert one.status_code == 404
        assert "two" in one.text.lower()
        unknown = client.get("/compare.csv", params={"a": 99999, "b": "missing"})
        assert unknown.status_code == 404

        assert writes == []
        assert calls == []
    finally:
        reset_pool()


def test_one_failed_section_does_not_blank_the_page(monkeypatch, tmp_path):
    path = tmp_path / "compare-hours.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    try:
        ids, _stamps = _seed(database)
        ada, bea = ids["ada"], ids["bea"]
        client = TestClient(main.app)
        client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
        original = SqliteDatabase.query

        async def explode_hours(self, sql, params=()):
            if "hour_utc" in sql:
                raise RuntimeError("D1_ERROR: LIKE or GLOB pattern too complex: SQLITE_ERROR")
            return await original(self, sql, params)

        monkeypatch.setattr(SqliteDatabase, "query", explode_hours)
        page = client.get("/compare", params={"a": ada, "b": bea})
        assert page.status_code == 200
        assert "data-compare-banner" not in page.text
        assert f'data-metric="spotify-monthly-listeners" data-artist="{ada}" data-value="200"' in page.text
        assert 'data-delta="7" data-value="+50 (+33.3%)"' in page.text
        assert f'data-timing="{ada}" data-hour-total="0" data-weekday-total="7"' in page.text
        assert "Data temporarily unavailable. Stored history could not be read on this request." in page.text
        assert "Internal Server Error" not in page.text
    finally:
        reset_pool()


def test_compare_survives_a_failed_read(monkeypatch, tmp_path):
    path = tmp_path / "compare-fail.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    try:
        asyncio.run(migrate(SqliteDatabase(str(path))))
        client = TestClient(main.app)
        client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)

        async def explode(self, sql, params=()):
            raise RuntimeError("D1_ERROR: exceeded D1 row read limit")

        monkeypatch.setattr(SqliteDatabase, "query", explode)
        page = client.get("/compare", params={"a": "1", "b": "2"})
        assert page.status_code == 200
        assert "Data temporarily unavailable" in page.text
        assert "Internal Server Error" not in page.text
        downloaded = client.get("/compare.csv", params={"a": "1", "b": "2"})
        assert downloaded.status_code == 503
        assert "Data temporarily unavailable" in downloaded.text
    finally:
        reset_pool()
