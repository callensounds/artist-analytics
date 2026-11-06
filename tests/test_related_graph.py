"""Stored related-artist ego graph. No Spotify calls."""

import asyncio

from fastapi.testclient import TestClient

import main
from crawl import reset_pool
from db import SqliteDatabase, migrate
from graph import NEIGHBOR_CAP, layout_ego, load_related_graph


def _db():
    database = SqliteDatabase(":memory:")

    async def ready():
        await migrate(database)
        return database

    return asyncio.run(ready())


def test_layout_keeps_only_edges_between_placed_nodes():
    graph = layout_ego(
        {"id": 1, "name": "Clairo"},
        [
            {"id": 2, "name": "Snail Mail", "rank": 1, "last_seen_at": "2026-10-31T00:00:00Z"},
            {"id": 3, "name": "An extremely long related artist name", "rank": 2, "last_seen_at": "2026-10-31T01:00:00Z"},
        ],
        [(1, 2), (1, 3), (2, 3), (2, 99), (2, 2)],
    )
    assert graph["nodes"][0]["center"] is True
    assert graph["nodes"][0]["x"] == 320.0
    assert graph["nodes"][0]["y"] == 210.0
    assert (graph["nodes"][1]["x"], graph["nodes"][1]["y"]) != (graph["nodes"][2]["x"], graph["nodes"][2]["y"])
    assert graph["nodes"][2]["label"].endswith("…")
    pairs = {tuple(sorted((edge["from_id"], edge["to_id"]))) for edge in graph["edges"]}
    assert pairs == {(1, 2), (1, 3), (2, 3)}


def test_graph_query_is_bounded_and_skips_names_not_in_d1():
    database = _db()

    async def seed():
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-10-31T00:00:00Z')"
        )
        center = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        center_id = int(center["id"])
        linked = []
        for index in range(NEIGHBOR_CAP + 2):
            await database.execute(
                "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES (?, ?, 'related', 1, '2026-10-31T00:00:00Z')",
                (f"Neighbor {index}", f"neighbor{index}"),
            )
            row = await database.query_one("SELECT id FROM artists WHERE normalized_name = ?", (f"neighbor{index}",))
            linked.append(int(row["id"]))
            await database.execute(
                """
                INSERT INTO related_edges
                    (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, 'spotify', ?, '2026-10-30T00:00:00Z', ?)
                """,
                (center_id, f"sp-{index}", f"Neighbor {index}", linked[-1], index + 1, f"2026-10-31T00:{index:02d}:00Z"),
            )
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'unstored', 'Not Stored', 'spotify', 0, '2026-10-30T00:00:00Z', '2026-10-31T00:00:00Z')
            """,
            (center_id,),
        )
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'back', 'Neighbor 1', ?, 'spotify', 1, '2026-10-30T00:00:00Z', '2026-10-31T02:00:00Z')
            """,
            (linked[0], linked[1]),
        )
        graph = await load_related_graph(database, center_id)
        missing = await load_related_graph(database, 99999)
        return graph, missing, linked

    graph, missing, linked = asyncio.run(seed())
    assert missing is None
    assert graph["center_name"] == "Clairo"
    assert [node["name"] for node in graph["neighbors"]] == [f"Neighbor {index}" for index in range(NEIGHBOR_CAP)]
    assert "Not Stored" not in {node["name"] for node in graph["nodes"]}
    assert len(graph["neighbors"]) == NEIGHBOR_CAP
    pairs = {tuple(sorted((edge["from_id"], edge["to_id"]))) for edge in graph["edges"]}
    assert (linked[0], linked[1]) in pairs


def test_related_page_does_not_call_providers(monkeypatch, tmp_path):
    path = tmp_path / "graph.sqlite"
    monkeypatch.setenv("ARTIST_ANALYTICS_SQLITE", str(path))
    reset_pool()
    database = SqliteDatabase(str(path))

    async def seed():
        await migrate(database)
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Clairo', 'clairo', 'search', 1, '2026-10-31T00:00:00Z')"
        )
        await database.execute(
            "INSERT INTO artists (name, normalized_name, origin, tracked, created_at) VALUES ('Snail Mail', 'snailmail', 'related', 1, '2026-10-31T00:00:00Z')"
        )
        clairo = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'clairo'")
        snail = await database.query_one("SELECT id FROM artists WHERE normalized_name = 'snailmail'")
        await database.execute(
            """
            INSERT INTO related_edges
                (from_artist_id, to_spotify_id, to_name, to_artist_id, source, rank, first_seen_at, last_seen_at)
            VALUES (?, 'snail', 'Snail Mail', ?, 'spotify', 1, '2026-10-30T00:00:00Z', '2026-10-31T06:00:00Z')
            """,
            (clairo["id"], snail["id"]),
        )
        return int(clairo["id"]), int(snail["id"])

    clairo_id, snail_id = asyncio.run(seed())
    calls = []

    async def fan_out(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("related graph must not fan out providers")

    monkeypatch.setattr(main, "collect_artist", fan_out)
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    artist = client.get(f"/artist/{clairo_id}")
    assert f'href="/artist/{clairo_id}/related"' in artist.text
    page = client.get(f"/artist/{clairo_id}/related")
    assert page.status_code == 200
    assert "Snail Mail" in page.text
    assert f'href="/artist/{snail_id}"' in page.text
    assert "<svg" in page.text
    assert "does not call Spotify" in page.text
    slug = client.get("/artist/clairo/related", follow_redirects=False)
    assert slug.status_code == 303
    assert slug.headers["location"] == f"/artist/{clairo_id}/related"
    missing = client.get("/artist/99999/related")
    assert missing.status_code == 404
    assert "not tracked" in missing.text
    assert calls == []
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
