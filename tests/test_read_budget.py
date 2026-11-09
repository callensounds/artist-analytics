"""D1 read budget: indexed plans, summary tables, and a login that survives a read limit."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import LATEST_MIGRATION, MIGRATIONS, SqliteDatabase, migrate, split_sql
from momentum import _MOMENTUM_SQL
from scores import _latest_sql


def _plan(database: SqliteDatabase, sql: str, params: tuple = ()) -> str:
    async def run():
        rows = await database.query("EXPLAIN QUERY PLAN " + sql, params)
        return " ".join(str(row.get("detail") or "") for row in rows)

    return asyncio.run(run())


def test_migration_statements_keep_triggers_intact():
    script = (MIGRATIONS / "0011_read_budget.sql").read_text()
    statements = split_sql(script)
    triggers = [item for item in statements if item.lstrip().upper().startswith("CREATE TRIGGER")]
    assert len(triggers) == 3
    assert all("BEGIN" in item and item.rstrip().endswith("END") for item in triggers)


def test_latest_rows_and_query_plans_do_not_scan_snapshot_history():
    database = SqliteDatabase(":memory:")

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-01-01T00:00:00Z')"
        )
        artist_id = int((await database.query_one("SELECT id FROM artists"))["id"])
        await database.execute("DROP TRIGGER metric_snapshots_latest")
        await database.execute("DROP TRIGGER metric_snapshots_fresh")
        await database.execute("DROP TRIGGER metric_snapshots_audience")
        for day in range(1, 40):
            stamp = f"2026-01-{day:02d}T00:00:00Z"
            await database.execute(
                """
                INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
                VALUES (?, 'spotify', 'Followers', ?, ?, ?)
                """,
                (artist_id, 1000 + day, str(1000 + day), stamp),
            )
        await database.execute(
            """
            INSERT INTO metric_snapshots (artist_id, source, metric, value, value_text, captured_at)
            VALUES (?, 'apple', 'Albums returned', 4, '4', '2026-02-01T00:00:00Z')
            """,
            (artist_id,),
        )
        for statement in split_sql((MIGRATIONS / "0011_read_budget.sql").read_text()):
            if statement.upper().startswith("INSERT INTO"):
                await database.execute(statement)
        latest = await database.query_one(
            "SELECT value FROM metric_latest WHERE artist_id = ? AND source = 'spotify' AND metric = 'Followers'",
            (artist_id,),
        )
        fresh = await database.query_one(
            "SELECT captured_at FROM source_freshness WHERE artist_id = ? AND source = 'spotify'",
            (artist_id,),
        )
        audience = await database.query("SELECT artist_id FROM audience_artists")
        return artist_id, latest, fresh, audience

    artist_id, latest, fresh, audience = asyncio.run(seed())
    assert int(latest["value"]) == 1039
    assert fresh["captured_at"] == "2026-01-39T00:00:00Z"
    assert [row["artist_id"] for row in audience] == [artist_id]

    score_plan = _plan(database, _latest_sql(""))
    assert "metric_latest" in score_plan
    assert "SCAN metric_snapshots" not in score_plan

    momentum_plan = _plan(
        database,
        _MOMENTUM_SQL,
        (artist_id, "2026-11-01T00:00:00Z") * 5 + (2000,),
    )
    assert "idx_snapshots_lookup" in momentum_plan
    assert "SCAN metric_snapshots" not in momentum_plan

    history_plan = _plan(
        database,
        """
        SELECT source, metric, value, captured_at
        FROM metric_snapshots
        WHERE artist_id = ? AND value IS NOT NULL
        ORDER BY captured_at DESC, id DESC
        LIMIT ?
        """,
        (artist_id, 120),
    )
    assert "idx_snapshots_artist_captured" in history_plan

    backfill_plan = _plan(
        database,
        """
        SELECT audience_artists.artist_id AS artist_id
        FROM audience_artists
        WHERE NOT EXISTS (
          SELECT 1 FROM alert_backfill WHERE alert_backfill.artist_id = audience_artists.artist_id
        )
        ORDER BY audience_artists.artist_id
        LIMIT ?
        """,
        (5,),
    )
    assert "audience_artists" in backfill_plan
    assert "metric_snapshots" not in backfill_plan

    fresh_plan = _plan(
        database,
        "SELECT source, captured_at FROM source_freshness WHERE artist_id = ?",
        (artist_id,),
    )
    assert "source_freshness" in fresh_plan
    assert "metric_snapshots" not in fresh_plan


def test_a_second_migrate_checks_one_version(tmp_path):
    path = tmp_path / "once.sqlite"
    first = SqliteDatabase(str(path))
    asyncio.run(migrate(first))
    second = SqliteDatabase(str(path))
    calls = []
    original = second.query_one

    async def spy(sql, params=()):
        if "schema_migrations" in sql:
            calls.append(params)
        return await original(sql, params)

    second.query_one = spy

    async def run():
        await migrate(second)
        await migrate(second)

    asyncio.run(run())
    assert calls == [(LATEST_MIGRATION,)]


def test_demo_login_survives_a_d1_read_limit_and_real_users_do_not(monkeypatch, tmp_path):
    path = tmp_path / "outage.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))
    asyncio.run(migrate(database))

    async def explode(db, email, password):
        raise RuntimeError("D1_ERROR: Your account has exceeded D1's free tier daily row read limit")

    monkeypatch.setattr(main, "authenticate", explode)
    client = TestClient(main.app)
    denied = client.post("/login", data={"email": "artist@demo.com", "password": "nope"}, follow_redirects=False)
    assert denied.status_code == 400
    assert "Invalid email or password" in denied.text

    logged = client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    assert logged.status_code == 303
    assert logged.headers["location"] == "/dashboard"
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Internal Server Error" not in page.text
    client.get("/logout", follow_redirects=False)

    other = client.post("/login", data={"email": "person@example.com", "password": "secret1"}, follow_redirects=False)
    assert other.status_code == 503
    assert "Data temporarily unavailable" in other.text
    assert "secret1" not in other.text
    bounced = client.get("/dashboard", follow_redirects=False)
    assert bounced.status_code == 303
    assert bounced.headers["location"] == "/login"
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)


def test_logged_in_pages_render_when_later_reads_fail(monkeypatch, tmp_path):
    path = tmp_path / "later.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    asyncio.run(migrate(SqliteDatabase(str(path))))
    client = TestClient(main.app)
    logged = client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    assert logged.status_code == 303

    async def explode(self, sql, params=()):
        raise RuntimeError("D1_ERROR: exceeded D1 row read limit")

    monkeypatch.setattr(SqliteDatabase, "query", explode)
    monkeypatch.setattr(SqliteDatabase, "query_one", explode)
    for path_name in ("/dashboard", "/artists", "/alerts", "/trending", "/watch", "/charts", "/scores", "/search?q=cla", "/admin"):
        page = client.get(path_name, follow_redirects=False)
        assert page.status_code == 200, path_name
        assert "Data temporarily unavailable" in page.text
        assert "Internal Server Error" not in page.text
    missing = client.get("/artist/1", follow_redirects=False)
    assert missing.status_code == 404
    assert "Data temporarily unavailable" in missing.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
