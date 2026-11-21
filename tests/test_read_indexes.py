"""Page reads that blew the D1 row cap keep the same rows and seek the new indexes."""

import asyncio

from db import LATEST_MIGRATION, MIGRATIONS, SqliteDatabase, migrate, split_sql
from digest import _chart_statement, _playlist_statement, fallback_statement
from history import ALERTS_FEED_SQL

INDEXES = (
    "idx_chart_entries_title_date",
    "idx_metric_alerts_detected",
    "idx_metric_latest_value",
    "idx_playlist_artist_first",
)


def _plan(database: SqliteDatabase, sql: str, params: tuple = ()) -> str:
    async def run():
        rows = await database.query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(row.get("detail") or "") for row in rows)

    return asyncio.run(run())


def _table_scan(plan: str, table: str) -> bool:
    """A bare SCAN of the table. ``SCAN t USING INDEX`` is an index walk, not this."""
    needle = f"SCAN {table}"
    return needle in plan and f"{needle} USING INDEX" not in plan


async def _rows(database: SqliteDatabase, sql: str, params: tuple, keys: tuple[str, ...]) -> list[tuple]:
    found = await database.query(sql, params)
    return [tuple(row.get(key) for key in keys) for row in found]


def test_read_index_migration_is_only_create_index_statements():
    assert LATEST_MIGRATION == "0016_read_indexes.sql"
    statements = split_sql((MIGRATIONS / LATEST_MIGRATION).read_text())
    assert len(statements) == 4
    for statement in statements:
        assert statement.lstrip().upper().startswith("CREATE INDEX IF NOT EXISTS")
        assert "ANALYZE" not in statement.upper()
    for name in INDEXES:
        assert any(name in statement for statement in statements)


def test_heavy_page_queries_use_the_new_indexes_and_keep_the_same_rows():
    database = SqliteDatabase(":memory:")
    chart_sql, chart_params = _chart_statement([1, 2], "2026-11-09", "2026-11-15")
    playlist_sql, playlist_params = _playlist_statement(
        [1, 2], "2026-11-09T00:00:00Z", "2026-11-15T12:00:00Z"
    )
    latest_sql, latest_params = fallback_statement()
    alert_params = (3,)

    async def seed():
        await migrate(database)
        for name, tracked in (("Ada", 1), ("Bea", 1), ("Cy", 1), ("Hidden", 0)):
            await database.execute(
                "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'search', ?, '2026-10-01T00:00:00Z')",
                (name, name.casefold(), tracked),
            )
        charts = [
            # Ada's title already charted before the window, so the in-window row is not a debut.
            (1, "itunes_topsongs", "2026-11-01", 1, "Old Hit"),
            (1, "itunes_topsongs", "2026-11-10", 4, "Old Hit"),
            (1, "itunes_topsongs", "2026-11-11", 2, "New Hit"),
            (1, "itunes_topalbums", "2026-11-12", 1, "New Hit"),
            (2, "itunes_topsongs", "2026-11-10", 8, "Bea Debut"),
            # A different artist must not hide Ada's debut.
            (3, "itunes_topsongs", "2026-11-01", 3, "New Hit"),
        ]
        for artist_id, source, day, position, title in charts:
            await database.execute(
                """
                INSERT INTO chart_entries (
                    chart_source, territory, chart_date, position, track_or_album_name,
                    artist_name, artist_id, collected_at
                ) VALUES (?, 'us', ?, ?, ?, 'Name', ?, ?)
                """,
                (source, day, position, title, artist_id, f"{day}T12:00:00Z"),
            )
        alerts = [
            (1, "2026-11-10T00:00:00Z", "early"),
            (2, "2026-11-12T00:00:00Z", "tie-low"),
            (1, "2026-11-12T00:00:00Z", "tie-high"),
            (2, "2026-11-01T00:00:00Z", "oldest"),
        ]
        for artist_id, detected, key in alerts:
            await database.execute(
                """
                INSERT INTO metric_alerts (
                    artist_id, provider, metric, window, delta, delta_pct,
                    baseline_value, latest_value, baseline_at, latest_at, detected_at, dedupe_key
                ) VALUES (?, 'spotify', 'Followers', '24h', 10, 20, 100, 110,
                          '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', ?, ?)
                """,
                (artist_id, detected, key),
            )
        listeners = [
            (1, 500, "500"),
            (2, 900, "900"),
            (3, 900.5, "900.5"),
            (4, 5000, "5000"),
        ]
        for artist_id, value, text in listeners:
            await database.execute(
                """
                INSERT INTO metric_latest (artist_id, source, metric, value, value_text, captured_at)
                VALUES (?, 'spotify', 'Monthly listeners', ?, ?, '2026-11-20T00:00:00Z')
                """,
                (artist_id, value, text),
            )
            await database.execute(
                """
                INSERT INTO metric_latest (artist_id, source, metric, value, value_text, captured_at)
                VALUES (?, 'instagram', 'Followers', 10, '10', '2026-11-20T00:00:00Z')
                """,
                (artist_id,),
            )
        playlists = [
            (1, "11", "In window", "2026-11-10T00:00:00Z"),
            (1, "12", "Before window", "2026-11-01T00:00:00Z"),
            (1, "13", "", "2026-11-12T00:00:00Z"),
            (2, "21", "Bea window", "2026-11-14T00:00:00Z"),
            (2, "22", "Bea later same day", "2026-11-14T08:00:00Z"),
        ]
        for artist_id, playlist_id, name, first in playlists:
            await database.execute(
                """
                INSERT INTO playlist_appearances (
                    artist_id, playlist_id, playlist_name, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, '2026-11-15T00:00:00Z')
                """,
                (artist_id, playlist_id, name, first),
            )

    asyncio.run(seed())

    chart_plan = _plan(database, chart_sql, chart_params)
    assert "idx_chart_entries_title_date" in chart_plan
    assert "sqlite_autoindex_chart_entries_1" not in chart_plan
    assert not _table_scan(chart_plan, "chart_entries")

    alert_plan = _plan(database, ALERTS_FEED_SQL, alert_params)
    assert "SCAN metric_alerts USING INDEX idx_metric_alerts_detected" in alert_plan
    assert "TEMP B-TREE" not in alert_plan
    assert not _table_scan(alert_plan, "metric_alerts")

    latest_plan = _plan(database, latest_sql, latest_params)
    assert "idx_metric_latest_value" in latest_plan
    assert not _table_scan(latest_plan, "metric_latest")

    playlist_plan = _plan(database, playlist_sql, playlist_params)
    assert "idx_playlist_artist_first" in playlist_plan
    assert "first_seen_at>" in playlist_plan
    assert not _table_scan(playlist_plan, "playlist_appearances")

    async def capture():
        return {
            "charts": await _rows(
                database,
                chart_sql,
                chart_params,
                ("artist_id", "chart_source", "track_or_album_name", "chart_date", "position"),
            ),
            "alerts": await _rows(
                database,
                ALERTS_FEED_SQL,
                alert_params,
                ("artist_name", "detected_at"),
            ),
            "listeners": await _rows(database, latest_sql, latest_params, ("id", "name")),
            "playlists": await _rows(
                database,
                playlist_sql,
                playlist_params,
                ("artist_id", "playlist_id", "playlist_name", "first_seen_at"),
            ),
        }

    with_indexes = asyncio.run(capture())
    assert with_indexes["charts"] == [
        (2, "itunes_topsongs", "Bea Debut", "2026-11-10", 8),
        (1, "itunes_topsongs", "New Hit", "2026-11-11", 2),
        (1, "itunes_topalbums", "New Hit", "2026-11-12", 1),
    ]
    # Same detected_at: the later id (Ada) comes before Bea. The oldest alert is past LIMIT 3.
    assert with_indexes["alerts"] == [
        ("Ada", "2026-11-12T00:00:00Z"),
        ("Bea", "2026-11-12T00:00:00Z"),
        ("Ada", "2026-11-10T00:00:00Z"),
    ]
    assert with_indexes["listeners"] == [(2, "Bea"), (1, "Ada")]
    assert with_indexes["playlists"] == [
        (2, "22", "Bea later same day", "2026-11-14T08:00:00Z"),
        (2, "21", "Bea window", "2026-11-14T00:00:00Z"),
        (1, "11", "In window", "2026-11-10T00:00:00Z"),
    ]

    async def drop_indexes():
        for name in INDEXES:
            await database.execute(f"DROP INDEX {name}")

    asyncio.run(drop_indexes())
    without_indexes = asyncio.run(capture())
    assert without_indexes == with_indexes
