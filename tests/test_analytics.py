from analytics import (
    build_patterns,
    choose_best_date,
    classify_date,
    is_strictly_earlier,
    names_match,
    spotify_precision,
)
from models import make_item, make_unix_item


def test_classify_date_levels():
    assert classify_date("2014") == "year"
    assert classify_date("2014-10") == "month"
    assert classify_date("2014-10-27") == "day"
    assert classify_date("2014-10-27T07:00:00Z") == "time"
    assert classify_date("") == "unknown"


def test_partial_date_is_not_earlier_than_a_more_precise_date_in_the_same_period():
    assert not is_strictly_earlier("2003", "2003-04-12")
    assert not is_strictly_earlier("2006-06", "2006-06-19")
    assert is_strictly_earlier("2006-06-19", "2006-07-22")
    assert is_strictly_earlier("2003", "2009-06-16")


def test_best_date_refines_precision_without_taking_a_later_reissue():
    assert choose_best_date([("2003", None), ("2003-04-12", "US")])[0] == "2003-04-12"
    assert choose_best_date([("2006-06-19", None), ("2006-07-22", "US")])[0] == "2006-06-19"
    assert choose_best_date([("2003", "US"), ("2009-06-16", "XW")])[0] == "2003"
    assert choose_best_date([("2006-06", None), ("2006-06-19", "GB"), ("2006-07-01", "US")])[0] == "2006-06-19"


def test_artist_names_match_only_when_normalized_text_is_equal():
    assert names_match("Taylor Swift", "taylor swift")
    assert names_match("Guns N' Roses", "Guns N Roses")
    assert not names_match("Demo Artist", "The Demo Artist")
    assert not names_match("Drake", "Drake Bell")
    assert not names_match("Drake", "Nick Drake")


def test_spotify_precision_is_never_upgraded():
    assert spotify_precision("2014-10-27", "day") == "day"
    assert spotify_precision("2014-10-27", "year") == "year"
    assert spotify_precision("2014", "year") == "year"


def test_patterns_ignore_partial_dates_and_keep_exact_hours():
    apple = make_item(title="Lover", kind="release", raw="2019-08-23T07:00:00Z")
    rough = make_item(title="Early", kind="release", raw="2003", precision="year")
    post = make_unix_item(title="Clip", kind="post", stamp=1790559242)
    sources = [
        {"platform": "apple", "label": "Apple Music", "items": [apple.__dict__, rough.__dict__]},
        {"platform": "spotify", "label": "Spotify", "items": [rough.__dict__]},
        {"platform": "tiktok", "label": "TikTok", "items": [post.__dict__]},
    ]
    patterns = build_patterns(sources)
    assert patterns["release_hours"][0]["label"] == "Apple Music"
    assert patterns["release_hours"][0]["data"][7] == 1
    assert sum(patterns["release_hours"][0]["data"]) == 1
    assert patterns["social_hours"][0]["platform"] == "tiktok"
    assert sum(patterns["social_hours"][0]["data"]) == 1
    weekday_totals = {row["platform"]: sum(row["data"]) for row in patterns["weekdays"]}
    assert weekday_totals["apple"] == 1
    assert "spotify" not in weekday_totals
