"""Month query on the release calendar. Invalid input stays on the current UTC month."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from analytics import (
    MONTH_NAMES,
    build_release_calendar,
    calendar_month_links,
    focus_release_month,
    parse_calendar_month,
    shift_calendar_month,
)
from crawl import reset_pool
from db import SqliteDatabase, migrate
from history import find_artist

import main


NOW = datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc)


def test_parse_calendar_month_accepts_yyyy_mm_and_falls_back():
    assert parse_calendar_month("2021-06", now=NOW) == (2021, 6)
    assert parse_calendar_month(" 2020-12 ", now=NOW) == (2020, 12)
    assert parse_calendar_month("1000-01", now=NOW) == (1000, 1)
    assert parse_calendar_month("9999-12", now=NOW) == (9999, 12)
    for raw in (None, "", "   ", "nope", "2021", "2021-6", "2021-13", "2021-00", "0999-01", "2021-06-16", "10000-01"):
        assert parse_calendar_month(raw, now=NOW) == (2026, 11)
    assert parse_calendar_month("bad", now=datetime(2024, 1, 2)) == (2024, 1)


def test_month_links_roll_december_into_january_and_january_into_december():
    december = calendar_month_links(2021, 12, 4, today=(2026, 11))
    assert december["label"] == "December 2021"
    assert december["timezone"] == "UTC"
    assert december["prev_label"] == "< Prev"
    assert december["next_label"] == "Next >"
    assert december["today_label"] == "Today"
    assert december["prev_href"] == "/artist/4/calendar?month=2021-11"
    assert december["next_href"] == "/artist/4/calendar?month=2022-01"
    assert december["today_href"] == "/artist/4/calendar?month=2026-11"
    assert december["is_current"] is False

    january = calendar_month_links(2021, 1, 4, today=(2026, 11))
    assert january["prev_href"] == "/artist/4/calendar?month=2020-12"
    assert january["next_href"] == "/artist/4/calendar?month=2021-02"
    assert shift_calendar_month(2021, 12, 1) == (2022, 1)
    assert shift_calendar_month(2021, 1, -1) == (2020, 12)
    assert calendar_month_links(1000, 1, 4, today=(2026, 11))["prev_href"] is None
    assert calendar_month_links(9999, 12, 4, today=(2026, 11))["next_href"] is None
    assert calendar_month_links(2026, 11, 4, today=(2026, 11))["is_current"] is True


def test_day_cell_prints_the_utc_clock_and_does_not_invent_one():
    calendar = build_release_calendar(
        [
            {
                "title": "Sling",
                "source": "apple",
                "published_at": "2021-06-16T07:00:00Z",
                "precision": "time",
            },
            {
                "title": "Sling",
                "source": "musicbrainz",
                "released_at": "2021-06-16",
                "precision": "day",
                "mbid": "rg-sling",
            },
            {
                "title": "Bag",
                "source": "musicbrainz",
                "released_at": "2006-06",
                "precision": "month",
            },
        ]
    )
    june = next(month for month in calendar["months"] if month["key"] == "2021-06")
    day = next(cell for cell in june["cells"] if cell["day"] == 16)
    apple = next(item for item in day["items"] if item["source"] == "apple")
    musicbrainz = next(item for item in day["items"] if item["source"] == "musicbrainz")
    assert apple["clock"] == "07:00 UTC"
    assert apple["timestamp"] == "2021-06-16 07:00 UTC"
    assert musicbrainz["clock"] == ""
    assert musicbrainz["timestamp"] == "2021-06-16"
    html = june["grid_html"]
    assert "calendar-grid" in html
    assert "calendar-agenda" in html
    assert "calendar-day-has" in html
    assert "2021-06-16 07:00 UTC" in html
    assert 'datetime="2021-06-16T07:00:00Z"' in html
    assert "00:00 UTC" not in html
    focused = focus_release_month(calendar, 2021, 6, 4, today=(2026, 11))
    assert [month["key"] for month in focused["months"]] == ["2021-06"]
    assert focused["month_nav"]["prev_href"].endswith("?month=2021-05")
    assert focused["month_nav"]["next_href"].endswith("?month=2021-07")
    empty = focus_release_month(build_release_calendar([]), 2026, 11, 4, today=(2026, 11))
    assert empty["available"] is True
    assert empty["month_empty"] is True
    assert empty["months"][0]["show_grid"] is True
    assert "November 2026" in empty["reason"]


def test_unavailable_source_card_does_not_invent_a_number():
    html = main.render_source_cards(
        [
            {
                "label": "Spotify",
                "status": "unavailable",
                "reason": "No Spotify artist is stored.",
                "metrics": [{"label": "Followers", "available": False, "value": None, "reason": "Not stored."}],
                "items": [],
                "tracks": [],
            }
        ]
    )
    assert "source-card-quiet" in html
    assert "Unavailable" in html
    assert "Followers" in html
    assert "stat-value" not in html
    assert "1.2M" not in html
    numbered = main.render_source_cards(
        [
            {
                "label": "Spotify",
                "status": "ok",
                "metrics": [{"label": "Followers", "available": True, "value": "4,000"}],
            }
        ]
    )
    assert "stat-value" in numbered
    assert "4,000" in numbered
    assert "source-card-quiet" not in numbered


def test_calendar_route_renders_month_navigation_for_valid_and_invalid_months(monkeypatch, tmp_path):
    path = tmp_path / "calendar-month.sqlite"
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
        await database.execute(
            """
            INSERT INTO musicbrainz_releases (
                artist_id, mbid, title, release_type, released_at, precision, first_seen_at, last_seen_at
            ) VALUES (?, 'rg-sling', 'Sling', 'Album', '2021-06-16', 'day', '2026-11-01T00:00:00Z', '2026-11-06T00:00:00Z')
            """,
            (artist_id,),
        )
        await database.execute(
            """
            INSERT INTO content_sightings
                (artist_id, source, external_id, title, kind, published_at, precision, first_seen_at)
            VALUES (?, 'apple', 'apple-sling', 'Sling', 'release', '2021-06-16T07:00:00Z', 'time', '2026-11-06T18:00:00Z')
            """,
            (artist_id,),
        )
        return artist_id

    import asyncio

    artist_id = asyncio.run(seed())
    client = TestClient(main.app)
    client.post("/login", data={"email": "artist@demo.com", "password": "demo123"}, follow_redirects=False)
    today = datetime.now(timezone.utc)
    today_key = f"{today.year:04d}-{today.month:02d}"
    today_label = f"{MONTH_NAMES[today.month]} {today.year}"

    june = client.get(f"/artist/{artist_id}/calendar?month=2021-06")
    december = client.get(f"/artist/{artist_id}/calendar?month=2021-12")
    january = client.get(f"/artist/{artist_id}/calendar?month=2021-01")
    invalid = client.get(f"/artist/{artist_id}/calendar?month=nope")
    out_of_range = client.get(f"/artist/{artist_id}/calendar?month=2021-13")
    day_value = client.get(f"/artist/{artist_id}/calendar?month=2021-06-16")
    blank = client.get(f"/artist/{artist_id}/calendar?month=")
    edge = client.get(f"/artist/{artist_id}/calendar?month=9999-12")

    assert june.status_code == 200
    assert "June 2021" in june.text
    assert "2021-06-16 07:00 UTC" in june.text
    assert "calendar-day-has" in june.text
    assert "calendar-agenda" in june.text
    assert "00:00 UTC" not in june.text
    assert "&lt; Prev" in june.text
    assert "Next &gt;" in june.text
    assert ">Today<" in june.text
    assert f'href="/artist/{artist_id}/calendar?month=2021-05"' in june.text
    assert f'href="/artist/{artist_id}/calendar?month=2021-07"' in june.text
    assert f'href="/artist/{artist_id}/calendar?month={today_key}"' in june.text

    assert december.status_code == 200
    assert f'href="/artist/{artist_id}/calendar?month=2022-01"' in december.text
    assert f'href="/artist/{artist_id}/calendar?month=2021-11"' in december.text
    assert january.status_code == 200
    assert f'href="/artist/{artist_id}/calendar?month=2020-12"' in january.text
    assert f'href="/artist/{artist_id}/calendar?month=2021-02"' in january.text

    for page in (invalid, out_of_range, day_value, blank):
        assert page.status_code == 200
        assert today_label in page.text
        assert "Sling" not in page.text
        assert f'href="/artist/{artist_id}/calendar?month={today_key}"' in page.text

    assert edge.status_code == 200
    assert "December 9999" in edge.text
    assert "Next &gt;" in edge.text
    reset_pool()
    monkeypatch.delenv("ARTIST_ANALYTICS_SQLITE", raising=False)
