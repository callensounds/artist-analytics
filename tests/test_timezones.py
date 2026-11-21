"""Viewer timezone: DST bucketing, partial dates, invalid names, cache keys, exports."""

import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
from analytics import build_posting_patterns
from compare import _fill_local_clocks, report_statements
from digest import apply_release_zone, render_digest_csv, render_digest_json
from page_cache import cache_key_from_scope
from timing import assemble_timing, render_timing_csv, render_timing_json
from timezones import ALLOWED, UTC, ZONES, normalize_tz, shift


def _scope(path="/timing", query=b"", cookie=None):
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query if isinstance(query, bytes) else query.encode(),
        "headers": headers,
    }


def test_allowlist_is_small_and_every_zone_loads():
    assert UTC in ALLOWED
    assert 20 <= len(ZONES) <= 32
    assert len(ALLOWED) == len(ZONES)
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    for name, _label in ZONES:
        assert normalize_tz(name) == name
        assert shift(summer, name).tzinfo is not None


def test_unknown_zone_is_utc_and_does_not_raise():
    assert normalize_tz(None) == "UTC"
    assert normalize_tz("") == "UTC"
    assert normalize_tz("not a zone") == "UTC"
    assert normalize_tz("America/Nowhere") == "UTC"
    assert normalize_tz("utc") == "UTC"
    assert shift(datetime(2026, 1, 15, 5, tzinfo=timezone.utc), "America/Nowhere").hour == 5


def test_dst_boundary_recolors_hours_and_weekdays_partial_dates_stay_put():
    rows = [
        {"source": "instagram", "published_at": "2026-01-15T05:00:00Z", "precision": "time"},
        {"source": "instagram", "published_at": "2026-07-15T04:00:00Z", "precision": "time"},
        {"source": "tiktok", "published_at": "2026-07-16T03:30:00Z", "precision": "time"},
        {"source": "apple", "published_at": "2026-07-15", "precision": "day"},
        {"source": "apple", "published_at": "2026-07", "precision": "month"},
        {"source": "youtube", "published_at": "2014", "precision": "year"},
        {"source": "instagram", "published_at": "2026-07-15T04:00:00Z", "precision": "day"},
    ]
    utc = build_posting_patterns(rows)
    local = build_posting_patterns(rows, tz="America/New_York")
    assert utc["timezone"] == "UTC"
    assert utc["hours"][5] == 1
    assert utc["hours"][4] == 1
    assert utc["hours"][3] == 1
    # January is UTC-5, July is UTC-4. A fixed -5 offset would put July 04:00 at 23:00.
    assert local["timezone"] == "America/New_York"
    assert local["hours"][0] == 2
    assert local["hours"][23] == 1
    assert local["hours"][4] == 0
    assert local["weekdays"][3] == 1  # Thursday 00:00 EST
    assert local["weekdays"][2] == 2  # Wednesday EDT, including the Thursday UTC 03:30
    assert local["exact_count"] == utc["exact_count"] == 3
    assert local["excluded_count"] == 4
    assert "America/New_York, DST-aware" in local["hour_heading"]
    assert local["hour_heading"] == "Hour of day (America/New_York, DST-aware)"
    # Half-hour offset, not a US standard-time shortcut. 18:30 UTC is 00:00 in Mumbai.
    kolkata = build_posting_patterns(
        [
            {"source": "instagram", "published_at": "2026-01-15T18:30:00Z", "precision": "time"},
            {"source": "tiktok", "published_at": "2026-01-16T18:30:00Z", "precision": "time"},
            {"source": "youtube", "published_at": "2026-01-17T18:30:00Z", "precision": "time"},
            {"source": "apple", "published_at": "2026-01-15", "precision": "day"},
        ],
        tz="Asia/Kolkata",
    )
    assert kolkata["hours"][0] == 3
    assert kolkata["hours"][18] == 0
    assert kolkata["excluded_count"] == 1
    bogus = build_posting_patterns(rows, tz="Mars/Olympus")
    assert bogus["hours"] == utc["hours"]
    assert bogus["timezone"] == "UTC"


def test_compare_hour_keys_cross_the_dst_boundary_and_sql_stays_one_statement():
    hours: dict[int, list[int]] = {}
    days: dict[int, list[int]] = {}
    _fill_local_clocks(
        [
            {"artist_id": 1, "hour_key": "2026-01-15T05", "n": 2},
            {"artist_id": 1, "hour_key": "2026-07-15T04", "n": 3},
            {"artist_id": 1, "hour_key": "not-a-clock", "n": 9},
        ],
        hours,
        days,
        "America/New_York",
    )
    assert hours[1][0] == 5
    assert hours[1][23] == 0
    assert days[1][3] == 2  # Thursday
    assert days[1][2] == 3  # Wednesday
    moment = datetime(2026, 11, 15, tzinfo=timezone.utc)
    utc_sql = report_statements([1, 2], "spotify-monthly-listeners", moment)
    local_sql = report_statements([1, 2], "spotify-monthly-listeners", moment, tz="America/Los_Angeles")
    assert len(utc_sql) == len(local_sql)
    utc_hours = [sql for sql, _params in utc_sql if "hour_utc" in sql]
    local_hours = [sql for sql, _params in local_sql if "hour_key" in sql]
    assert len(utc_hours) == 2
    assert len(local_hours) == 2
    assert all("hour_utc" not in sql for sql in local_hours)
    assert all("precision = 'time'" in sql for sql in local_hours)
    local_days = [sql for sql, _params in local_sql if " AS dow" in sql]
    utc_days = [sql for sql, _params in utc_sql if " AS dow" in sql]
    assert len(local_days) == 2
    assert all("precision = 'day'" in sql for sql in local_days)
    assert all("precision IN ('day', 'time')" in sql for sql in utc_days)


def test_cache_key_changes_with_the_viewer_zone():
    anon = cache_key_from_scope(_scope())
    nyc = cache_key_from_scope(_scope(cookie="aa_tz=America/New_York"))
    london = cache_key_from_scope(_scope(cookie="session=abc; aa_tz=Europe/London"))
    bogus = cache_key_from_scope(_scope(cookie="aa_tz=Not/AZone"))
    query = cache_key_from_scope(_scope(query=b"tz=America/Los_Angeles", cookie="aa_tz=Europe/London"))
    invalid_query = cache_key_from_scope(_scope(query=b"tz=nope", cookie="aa_tz=Europe/London"))
    assert anon != nyc
    assert nyc != london
    assert "America/New_York" in nyc
    assert "Europe/London" in london
    assert bogus == anon
    assert "America/Los_Angeles" in query
    assert query != london
    # An unknown tz is UTC in the key. The raw query still differs, so it is not the anon entry.
    assert "\x1fUTC\x1f" in invalid_query
    assert "Europe/London" not in invalid_query
    assert "nope" in invalid_query
    assert "abc" not in london


def test_timing_export_keeps_utc_fields_and_adds_local_time():
    now = datetime(2026, 11, 15, 18, tzinfo=timezone.utc)
    artists = [{"id": 1, "name": "Ada"}]
    sightings = [
        {
            "artist_id": 1,
            "source": "apple",
            "external_id": "a",
            "title": "Winter",
            "kind": "release",
            "published_at": "2026-01-15T05:00:00Z",
            "precision": "time",
        },
        {
            "artist_id": 1,
            "source": "apple",
            "external_id": "b",
            "title": "Summer",
            "kind": "release",
            "published_at": "2026-07-15T04:00:00Z",
            "precision": "time",
        },
        {
            "artist_id": 1,
            "source": "apple",
            "external_id": "c",
            "title": "Late",
            "kind": "release",
            "published_at": "2026-07-16T04:00:00Z",
            "precision": "time",
        },
        {
            "artist_id": 1,
            "source": "spotify",
            "external_id": "d",
            "title": "Day",
            "kind": "release",
            "published_at": "2026-07-17",
            "precision": "day",
        },
        {
            "artist_id": 1,
            "source": "spotify",
            "external_id": "e",
            "title": "Year",
            "kind": "release",
            "published_at": "2014",
            "precision": "year",
        },
    ]
    utc = assemble_timing(artists, sightings, [], scope="all", days="all", kind="all", now=now)
    local = assemble_timing(
        artists, sightings, [], scope="all", days="all", kind="all", now=now, tz="America/New_York"
    )
    assert local["hour_counts"] == utc["hour_counts"]
    assert local["heatmap"] == utc["heatmap"]
    assert local["artists"][0]["modal_hour_utc"] == utc["artists"][0]["modal_hour_utc"]
    assert local["artists"][0]["latest_exact_at"] == utc["artists"][0]["latest_exact_at"]
    assert local["artists"][0]["latest_exact_at"].endswith("Z")
    assert "UTC" in local["artists"][0]["latest_exact_local"]
    assert local["artists"][0]["modal_hour_local"] == "00:00"
    assert local["zone_label"] == "America/New_York, DST-aware"
    # The three exact clocks land at local midnight. The day and the year do not add an hour.
    shown = sum(cell["count"] for row in local["heatmap_rows"] for cell in row["cells"])
    assert shown == 3
    assert sum(cell["count"] for cell in local["heatmap_rows"][3]["cells"] if cell["hour"] == 0) == 2
    document = json.loads(render_timing_json(local))
    assert document["timezone"] == "UTC"
    assert document["viewer_timezone"] == "America/New_York"
    assert document["hour_counts"] == utc["hour_counts"]
    artist = document["artists"][0]
    assert artist["modal_hour_utc"] == utc["artists"][0]["modal_hour_utc"]
    assert artist["latest_exact_at"] == utc["artists"][0]["latest_exact_at"]
    assert artist["latest_exact_local"]
    assert "T" not in "2014"
    text = render_timing_csv(local)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["modal_hour_utc"] == utc["artists"][0]["modal_hour_utc"]
    assert parsed[0]["latest_exact_at"] == utc["artists"][0]["latest_exact_at"]
    assert parsed[0]["latest_exact_local"]
    assert parsed[0]["viewer_tz"] == "America/New_York"
    assert "modal_hour_utc" in parsed[0]
    utc_csv = list(csv.DictReader(io.StringIO(render_timing_csv(utc))))
    assert utc_csv[0]["latest_exact_local"] == ""
    assert utc_csv[0]["viewer_tz"] == "UTC"


def test_digest_export_keeps_the_utc_release_field():
    view = {
        "generated_at": "2026-11-15T18:00:00Z",
        "source": "watchlist",
        "source_note": "",
        "read_error": "",
        "threshold_percent": 10,
        "section_notes": {},
        "overview": {
            "artist_count": 1,
            "spike_count": 0,
            "release_count": 2,
            "chart_count": 0,
            "playlist_count": 0,
            "alert_count": 0,
            "spikes": [],
        },
        "artists": [
            {
                "id": 1,
                "name": "Ada",
                "metrics": [],
                "releases": [
                    {
                        "title": "Timed EP",
                        "mbid": "mb",
                        "release_type": "album",
                        "released_at": "2026-07-15T04:00:00Z",
                        "precision": "time",
                        "date_only": False,
                        "note": "Exact UTC time.",
                    },
                    {
                        "title": "Day Single",
                        "mbid": "day",
                        "release_type": "single",
                        "released_at": "2026-07-15",
                        "precision": "day",
                        "date_only": True,
                        "note": "Date only. No clock time is stored.",
                    },
                ],
                "charts": [],
                "playlists": [],
                "alerts": [],
            }
        ],
    }
    stamped = apply_release_zone(view, "America/New_York")
    timed = stamped["artists"][0]["releases"][0]
    day = stamped["artists"][0]["releases"][1]
    assert timed["released_at"] == "2026-07-15T04:00:00Z"
    assert "UTC" in timed["released_at_local"]
    assert "00:00" in timed["released_at_local"]
    assert day["released_at"] == "2026-07-15"
    assert day["released_at_local"] == ""
    assert "T" not in day["released_at"]
    document = json.loads(render_digest_json(stamped))
    assert document["viewer_timezone"] == "America/New_York"
    assert document["artists"][0]["releases"][0]["released_at"] == "2026-07-15T04:00:00Z"
    assert document["artists"][0]["releases"][1]["released_at_local"] == ""
    rows = list(csv.DictReader(io.StringIO(render_digest_csv(stamped))))
    release_rows = [row for row in rows if row["section"] == "release"]
    assert release_rows[0]["occurred_at"] == "2026-07-15T04:00:00Z"
    assert "UTC" in release_rows[0]["released_at_local"]
    assert release_rows[1]["occurred_at"] == "2026-07-15"
    assert release_rows[1]["released_at_local"] == ""
    untouched = apply_release_zone(
        {"artists": [{"releases": [{"released_at": "2026-07-15T04:00:00Z", "precision": "time", "date_only": False}]}]},
        "nope",
    )
    assert untouched["viewer_timezone"] == "UTC"
    assert untouched["artists"][0]["releases"][0]["released_at_local"] == ""


def test_selector_remembers_a_valid_zone_and_ignores_an_unknown_one():
    client = TestClient(main.app)
    page = client.get("/", params={"tz": "America/New_York"})
    assert page.status_code == 200
    assert "aa_tz=America/New_York" in page.headers["set-cookie"]
    assert 'value="America/New_York" selected' in page.text
    bad = client.get("/", params={"tz": "Not/AZone"})
    assert bad.status_code == 200
    assert "aa_tz=" not in bad.headers.get("set-cookie", "")
    assert 'value="UTC" selected' in bad.text
    again = client.get("/")
    assert again.status_code == 200
    assert 'value="America/New_York" selected' in again.text
    login = client.post(
        "/login",
        data={"email": "artist@demo.com", "password": "demo123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
