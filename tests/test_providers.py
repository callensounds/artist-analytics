import asyncio
import json

from cache import MemoryCache
from http_client import HttpResult
from models import make_item
from providers.common import cache_key
from runtime import env_get
from providers.apple import (
    LOOKUP,
    SEARCH,
    WEB_EXACT_LIMIT,
    fetch_apple,
    itunes_url,
    parse_apple_albums,
    parse_collections,
    parse_release_meta,
)
from providers.instagram import parse_embed
from providers.musicbrainz import aggregate_catalog, extract_links
from providers.spotify import fetch_spotify, parse_albums, parse_discography, parse_monthly_listeners, parse_popular, visible_lines
from providers.tiktok import parse_embed_videos, parse_profile, parse_video
from providers.youtube import fetch_youtube, parse_videos
from providers import collect_artist


class FakeHttp:
    def __init__(self, responder):
        self.responder = responder
        self.headers = []

    async def get_text(self, url, **kwargs):
        self.headers.append(kwargs.get("headers") or {})
        return self.responder("GET", url, kwargs.get("params"), kwargs.get("data"))

    async def get_json(self, url, **kwargs):
        result = await self.get_text(url, **kwargs)
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
        return result, payload

    async def post_form(self, url, data, **kwargs):
        result = self.responder("POST", url, kwargs.get("params"), data)
        try:
            payload = result.json()
        except json.JSONDecodeError:
            payload = None
        return result, payload


def test_musicbrainz_links_and_date_refinement():
    links = extract_links(
        [
            {"type": "free streaming", "url": {"resource": "https://open.spotify.com/artist/06HL4z0CvFAxyc27GXpf02"}},
            {"type": "streaming", "url": {"resource": "https://music.apple.com/us/artist/159260351"}},
            {"type": "social network", "url": {"resource": "https://www.instagram.com/taylorswift/"}},
            {"type": "social network", "url": {"resource": "https://www.tiktok.com/@taylorswift"}},
            {"type": "youtube", "url": {"resource": "https://www.youtube.com/channel/UCqECaJ8Gagnn7YCbPEzWH6g"}},
            {"type": "youtube music", "url": {"resource": "https://music.youtube.com/channel/UCPC0L1d253x-KuMNwa05TpA"}},
        ]
    )
    assert links["spotify_id"] == "06HL4z0CvFAxyc27GXpf02"
    assert links["apple_id"] == "159260351"
    assert links["instagram"] == "taylorswift"
    assert links["tiktok"] == "taylorswift"
    assert links["youtube_channel_ids"] == ["UCqECaJ8Gagnn7YCbPEzWH6g"]

    items = aggregate_catalog(
        [
            {
                "id": "rg1",
                "title": "Tim McGraw",
                "primary-type": "Single",
                "first-release-date": "2006-06-19",
            },
            {
                "id": "rg2",
                "title": "Lucky You",
                "primary-type": "Single",
                "secondary-types": ["Demo"],
                "first-release-date": "2003",
            },
            {
                "id": "rg3",
                "title": "Early",
                "primary-type": "Album",
                "first-release-date": "2003",
            },
        ],
        [
            {
                "date": "2006-07-22",
                "country": "US",
                "release-group": {"id": "rg1", "title": "Tim McGraw", "primary-type": "Single"},
                "release-events": [{"date": "2006-07-22", "area": {"name": "United States"}}],
            },
            {
                "date": "2003-04-12",
                "country": "US",
                "release-group": {"id": "rg3", "title": "Early", "primary-type": "Album"},
                "release-events": [{"date": "2003-04-12", "area": {"name": "United States"}}],
            },
        ],
    )
    by_title = {item.title: item for item in items}
    assert "Lucky You" not in by_title
    assert by_title["Tim McGraw"].display == "2006-06-19"
    assert by_title["Tim McGraw"].precision == "day"
    assert by_title["Early"].display == "2003-04-12"


def test_apple_lookup_keeps_the_exact_release_timestamp():
    artist, items = parse_collections(
        {
            "results": [
                {"wrapperType": "artist", "artistId": 5, "artistName": "Example", "artistLinkUrl": "https://music.apple.com/us/artist/example/5"},
                {
                    "wrapperType": "collection",
                    "collectionType": "Album",
                    "artistId": 5,
                    "collectionId": 9,
                    "collectionName": "Lover",
                    "releaseDate": "2019-08-23T07:00:00Z",
                    "trackCount": 18,
                    "country": "USA",
                    "collectionViewUrl": "https://music.apple.com/album/9",
                },
                {
                    "wrapperType": "collection",
                    "artistId": 99,
                    "collectionId": 10,
                    "collectionName": "Tribute",
                    "releaseDate": "2020-01-01T07:00:00Z",
                },
            ]
        },
        "5",
    )
    assert artist["artistName"] == "Example"
    assert len(items) == 1
    assert items[0].display == "2019-08-23 07:00 UTC"
    assert items[0].precision == "time"
    assert items[0].hour_utc == 7
    assert items[0].weekday == "Friday"


def test_apple_search_does_not_attach_a_longer_artist_name():
    def respond(method, url, params, data):
        if "itunes.apple.com/search" in url:
            body = {
                "results": [
                    {"wrapperType": "artist", "artistId": 1835804494, "artistName": "The Demo Artist"},
                ]
            }
            return HttpResult(200, json.dumps(body), url, {})
        return HttpResult(500, "lookup should not run", url, {})

    result = asyncio.run(
        fetch_apple("Demo Artist", {}, http=FakeHttp(respond), cache=MemoryCache({}))
    )
    assert result.status == "unavailable"
    assert "not shown" in result.reason


def test_spotify_albums_preserve_stated_precision():
    items = parse_albums(
        {
            "items": [
                {
                    "name": "1989",
                    "release_date": "2014-10-27",
                    "release_date_precision": "day",
                    "album_type": "album",
                    "external_urls": {"spotify": "https://open.spotify.com/album/1"},
                },
                {
                    "name": "Sketch",
                    "release_date": "2003",
                    "release_date_precision": "year",
                    "album_type": "single",
                },
            ]
        }
    )
    by_title = {item.title: item for item in items}
    assert by_title["1989"].precision == "day"
    assert by_title["1989"].hour_utc is None
    assert by_title["1989"].weekday == "Monday"
    assert by_title["Sketch"].precision == "year"
    assert by_title["Sketch"].weekday is None


def test_spotify_public_page_keeps_year_precision_and_exact_listeners():
    page = """
    <html><head>
    <meta property="og:title" content="Example"/>
    <meta property="og:description" content="Artist · 1.2M monthly listeners."/>
    </head><body>
    <p>1,234,567 monthly listeners</p>
    <section>
      Popular
      Example Song
      9,876,543
      Other Song
      1200
      Popular releases
      Debut
      2020 • Album
      Albums
      Debut
      Album • 2020
      Second
      Album • 2019
      Singles and EPs
      Single Name
      Single • 2021
    </section>
    </body></html>
    """
    listeners, exact = parse_monthly_listeners(page)
    assert listeners == "1,234,567"
    assert exact is True
    lines = visible_lines(page)
    rows = parse_discography(lines)
    assert ("Debut", "Album", "2020") in rows
    assert ("Second", "Album", "2019") in rows
    assert ("Single Name", "Single", "2021") in rows
    assert rows.count(("Debut", "Album", "2020")) == 1
    items = parse_albums(
        {"items": [{"name": "Ignored", "release_date": "2020", "release_date_precision": "year"}]}
    )
    public_items = [
        item
        for title, kind, year in rows
        if (item := make_item(title=title, kind="release", raw=year, precision="year", detail=kind))
    ]
    assert all(item.precision == "year" and item.hour_utc is None and item.weekday is None for item in public_items)
    assert parse_popular(lines)[0] == ("Example Song", 9876543)
    assert items[0].precision == "year"


def test_spotify_public_fetch_does_not_call_the_web_api():
    seen = []

    def respond(method, url, params, data):
        seen.append(url)
        assert "api.spotify.com" not in url
        assert "accounts.spotify.com" not in url
        if "oembed" in url:
            body = {"title": "Example Artist", "thumbnail_url": "https://image.example/art.jpg"}
            return HttpResult(200, json.dumps(body), url, {})
        if "/embed/artist/" in url:
            body = """<script id="__NEXT_DATA__" type="application/json">%s</script>""" % json.dumps(
                {"props": {"pageProps": {"state": {"data": {"entity": {"name": "Example Artist", "trackList": [{"title": "Example Song"}]}}}}}}
            )
            return HttpResult(200, body, url, {})
        if "/artist/" in url:
            body = "<html><body><p>1,234,567 monthly listeners</p><div>Albums</div><div>Debut</div><div>Album • 2020</div></body></html>"
            return HttpResult(200, body, url, {})
        return HttpResult(404, "", url, {})

    result = asyncio.run(
        fetch_spotify(
            "Example Artist",
            {"spotify_id": "abc123"},
            env={},
            http=FakeHttp(respond),
            cache=MemoryCache({}),
        )
    )
    assert result.status == "ok"
    assert result.profile_name == "Example Artist"
    assert result.image_url == "https://image.example/art.jpg"
    assert result.metrics[0].value == "1,234,567"
    assert result.items[0].precision == "year"
    assert result.items[0].display == "2020"
    assert result.items[0].hour_utc is None
    assert any("open.spotify.com" in url for url in seen)


def test_youtube_uses_video_published_at():
    items = parse_videos(
        {
            "items": [
                {
                    "snippet": {"title": "Private video", "publishedAt": "2024-01-01T00:00:00Z"},
                    "contentDetails": {"videoId": "priv"},
                },
                {
                    "snippet": {"title": "Live", "publishedAt": "2024-05-01T12:00:00Z"},
                    "contentDetails": {"videoId": "abc", "videoPublishedAt": "2024-05-01T15:42:00Z"},
                },
            ]
        }
    )
    assert len(items) == 1
    assert items[0].display == "2024-05-01 15:42 UTC"
    assert items[0].url == "https://www.youtube.com/watch?v=abc"


def test_instagram_embed_parser_reads_exact_timestamps_and_ignores_other_users():
    html = r'''
    shortcode\":\"taylored\",\"is_video\":false,
    edge_media_to_caption\":{\"edges\":[{\"node\":{\"text\":\"A night at the museum\"}}]},
    edge_liked_by\":{\"count\":10},
    taken_at_timestamp\":1792535205,
    username\":\"someoneelse\",
    edge_followed_by\":{\"count\":3},
    shortcode\":\"Deu50A8jiIq\",\"is_video\":false,
    edge_media_to_caption\":{\"edges\":[{\"node\":{\"text\":\"A night at the museum I\u2019ll remember\"}}]},
    edge_liked_by\":{\"count\":3798858},
    taken_at_timestamp\":1792535205,
    username\":\"taylorswift\",
    full_name\":\"Taylor Swift\",
    is_private\":false,
    edge_followed_by\":{\"count\":272440413}
    '''
    parsed = parse_embed(html, "taylorswift")
    assert parsed["followers"] == 272440413
    assert parsed["full_name"] == "Taylor Swift"
    assert len(parsed["posts"]) == 1
    assert parsed["posts"][0].precision == "time"
    assert parsed["posts"][0].timezone == "UTC"
    assert parsed["posts"][0].url.endswith("/p/Deu50A8jiIq/")


def test_tiktok_parsers_use_create_time_not_a_guess():
    profile_html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(
            {
                "__DEFAULT_SCOPE__": {
                    "webapp.user-detail": {
                        "statusCode": 0,
                        "userInfo": {
                            "user": {"uniqueId": "taylorswift", "nickname": "Taylor Swift", "privateAccount": False},
                            "stats": {"followerCount": 33400000, "heartCount": 279500000, "videoCount": 86},
                        },
                    }
                }
            }
        )
        + "</script>"
    )
    profile = parse_profile(profile_html)
    assert profile["stats"]["followerCount"] == 33400000
    embed_html = (
        '<script id="__FRONTITY_CONNECT_STATE__" type="application/json">'
        + json.dumps(
            {
                "source": {
                    "data": {
                        "/embed/@taylorswift": {
                            "videoList": [
                                {"id": "7690393326704430366", "desc": "Patient Zero", "authorUniqueId": "taylorswift"},
                                {"id": "1", "desc": "Other", "authorUniqueId": "someoneelse"},
                            ]
                        }
                    }
                }
            }
        )
        + "</script>"
    )
    videos = parse_embed_videos(embed_html, "taylorswift")
    assert [video["id"] for video in videos] == ["7690393326704430366"]
    video_html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(
            {
                "__DEFAULT_SCOPE__": {
                    "webapp.video-detail": {
                        "itemInfo": {
                            "itemStruct": {
                                "id": "7690393326704430366",
                                "desc": "Patient Zero",
                                "createTime": "1790559242",
                                "stats": {"playCount": 8100000},
                            }
                        }
                    }
                }
            }
        )
        + "</script>"
    )
    item = parse_video(video_html)
    assert item["createTime"] == "1790559242"


def test_collect_reports_missing_secrets_and_does_not_invent_metrics():
    def respond(method, url, params, data):
        if "musicbrainz.org/ws/2/artist/" in url and params and "query" in (params or {}):
            body = {"artists": [{"id": "mbid-1", "name": "Example Artist", "score": 100}]}
        elif url.endswith("/artist/mbid-1"):
            body = {
                "name": "Example Artist",
                "disambiguation": "test",
                "relations": [
                    {"url": {"resource": "https://music.apple.com/us/artist/example/5"}},
                ],
            }
        elif "release-group" in url:
            body = {
                "release-groups": [
                    {"id": "rg", "title": "Debut", "primary-type": "Album", "first-release-date": "2020-01-02"}
                ]
            }
        elif "musicbrainz.org/ws/2/release" in url:
            body = {"releases": []}
        elif "itunes.apple.com/lookup" in url:
            body = {
                "results": [
                    {"wrapperType": "artist", "artistId": 5, "artistName": "Example Artist", "artistLinkUrl": "https://music.apple.com/a/5"},
                    {
                        "wrapperType": "collection",
                        "artistId": 5,
                        "collectionId": 8,
                        "collectionName": "Debut",
                        "releaseDate": "2020-01-02T08:00:00Z",
                        "collectionType": "Album",
                    },
                ]
            }
        else:
            return HttpResult(404, "", url, {})
        return HttpResult(200, json.dumps(body), url, {})

    bundle = asyncio.run(
        collect_artist(
            "Example Artist",
            env={},
            http=FakeHttp(respond),
            cache=MemoryCache({}),
        )
    )
    by_platform = {source["platform"]: source for source in bundle["sources"]}
    assert by_platform["apple"]["status"] == "ok"
    assert by_platform["apple"]["items"][0]["display"] == "2020-01-02 08:00 UTC"
    assert by_platform["spotify"]["status"] == "unavailable"
    assert "not guessed" in by_platform["spotify"]["reason"]
    assert by_platform["youtube"]["status"] == "missing_credentials"
    assert by_platform["instagram"]["status"] == "unavailable"
    assert "not guessed" in by_platform["instagram"]["reason"]
    assert by_platform["tiktok"]["status"] == "unavailable"
    assert bundle["patterns"]["release_hours"]
    blob = json.dumps(bundle)
    assert "1.2M" not in blob
    assert "[12, 28, 35, 18]" not in blob
    assert "_mbid" not in blob


def test_env_get_reads_a_plain_string_and_rejects_placeholders():
    calls = {"to_py": 0}

    class ProxyKey:
        def to_py(self):
            calls["to_py"] += 1
            return "AIzaExactKey123"

        def __str__(self):
            return "[object Object]"

    class Garbage:
        def __str__(self):
            return "[object Object]"

    class Env:
        def __init__(self, value):
            self.YOUTUBE_API_KEY = value

    assert env_get(Env(ProxyKey()), "YOUTUBE_API_KEY") is None
    assert calls["to_py"] == 0
    assert env_get(Env("AIzaExactKey123"), "YOUTUBE_API_KEY") == "AIzaExactKey123"
    assert env_get(Env(Garbage()), "YOUTUBE_API_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "undefined"}, "YOUTUBE_API_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "null"}, "YOUTUBE_API_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "[object Object]"}, "YOUTUBE_API_KEY") is None
    assert env_get({"YOUTUBE_API_KEY": "[REDACTED]"}, "YOUTUBE_API_KEY") is None


def test_youtube_request_url_contains_the_exact_api_key():
    class Env:
        YOUTUBE_API_KEY = "AIzaExactKey123"

    urls = []

    def respond(method, url, params, data):
        urls.append(url)
        if "/youtube/v3/search?" in url:
            body = {
                "items": [
                    {"id": {"channelId": "UCexact"}, "snippet": {"title": "Callen Sounds"}},
                ]
            }
        elif "/youtube/v3/channels?" in url:
            body = {
                "items": [
                    {
                        "id": "UCexact",
                        "snippet": {"title": "Callen Sounds"},
                        "statistics": {"subscriberCount": "12", "hiddenSubscriberCount": False},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUexact"}},
                    }
                ]
            }
        elif "/youtube/v3/playlistItems?" in url:
            body = {
                "items": [
                    {
                        "snippet": {
                            "title": "First",
                            "publishedAt": "2024-05-01T15:04:05Z",
                            "resourceId": {"videoId": "vid1"},
                        },
                        "contentDetails": {"videoId": "vid1", "videoPublishedAt": "2024-05-01T15:04:05Z"},
                    }
                ]
            }
        else:
            return HttpResult(404, "", url, {})
        return HttpResult(200, json.dumps(body), url, {})

    from providers.youtube import fetch_youtube, redact_key, youtube_url

    http = FakeHttp(respond)
    result = asyncio.run(fetch_youtube("Callen Sounds", {}, Env(), http, MemoryCache({})))
    assert urls
    assert result.status == "ok"
    assert any("Matched by name" in note for note in result.notices)
    assert http.headers
    for header in http.headers:
        assert header.get("X-Goog-Api-Key") == "AIzaExactKey123"
    for url in urls:
        assert "AIzaExactKey123" not in url
        assert "[object Object]" not in url
        assert "undefined" not in url
    built = youtube_url(
        "channels",
        {"part": "id", "forHandle": "@TaylorSwift", "key": "AIzaExactKey123"},
    )
    assert "key=AIzaExactKey123" in built
    redacted = redact_key(built, "AIzaExactKey123")
    assert "AIzaExactKey123" not in redacted
    assert "AIza…(15)" in redacted
    assert "AIzaExactKey123" not in (result.reason or "")
    assert "AIzaExactKey123" not in " ".join(result.notices)


def test_apple_lookup_by_id_does_not_search_and_429_uses_stale_cache(monkeypatch):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("providers.apple.asyncio.sleep", no_sleep)
    lookup = itunes_url(LOOKUP, {"id": "5", "entity": "album", "limit": "100", "country": "us"})
    stale = {
        "results": [
            {"wrapperType": "artist", "artistId": 5, "artistName": "Example", "artistLinkUrl": "https://music.apple.com/a/5"},
            {
                "wrapperType": "collection",
                "artistId": 5,
                "collectionId": 8,
                "collectionName": "Debut",
                "releaseDate": "2020-01-02T08:00:00Z",
                "collectionType": "Album",
            },
        ]
    }
    cache = MemoryCache({})

    async def seed():
        await cache.set(
            cache_key("itunes-stale", lookup),
            json.dumps({"at": "2026-01-02 03:04 UTC", "body": stale}),
            1000,
        )

    asyncio.run(seed())
    calls = []

    def respond(method, url, params, data):
        calls.append(url)
        if "itunes.apple.com/search" in url:
            return HttpResult(500, "search should not run", url, {})
        return HttpResult(429, "", url, {})

    result = asyncio.run(fetch_apple("Example", {"apple_id": "5"}, FakeHttp(respond), cache))
    assert calls
    assert all("itunes.apple.com/search" not in url for url in calls)
    assert len(calls) == 2
    assert result.status == "ok"
    assert result.items[0].display == "2020-01-02 08:00 UTC"
    assert result.reason == "Apple is rate-limiting; showing cached data from 2026-01-02 03:04 UTC."


def test_apple_403_without_cache_says_try_again(monkeypatch):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("providers.apple.asyncio.sleep", no_sleep)
    calls = []

    def respond(method, url, params, data):
        calls.append(url)
        return HttpResult(403, "", url, {})

    result = asyncio.run(fetch_apple("Example", {"apple_id": "5"}, FakeHttp(respond), MemoryCache({})))
    assert calls[0].startswith("https://itunes.apple.com/lookup")
    assert any("music.apple.com" in url for url in calls)
    assert result.status == "unavailable"
    assert "HTTP 403" in result.reason
    assert "Try again shortly" in result.reason


def test_unmatched_artist_is_labeled_matched_by_name_and_does_not_invent_counts():
    def respond(method, url, params, data):
        if "musicbrainz.org/ws/2/artist/" in url and params and "query" in (params or {}):
            body = {"artists": []}
        elif "itunes.apple.com/search" in url:
            assert "entity=musicArtist" in url
            body = {"results": [{"wrapperType": "artist", "artistId": 42, "artistName": "Callen Sounds"}]}
        elif "itunes.apple.com/lookup" in url:
            assert "id=42" in url
            assert "entity=album" in url
            body = {
                "results": [
                    {"wrapperType": "artist", "artistId": 42, "artistName": "Callen Sounds"},
                    {
                        "wrapperType": "collection",
                        "artistId": 42,
                        "collectionId": 7,
                        "collectionName": "Demo",
                        "releaseDate": "2024-03-01T08:00:00Z",
                        "collectionType": "Album",
                    },
                ]
            }
        elif "/youtube/v3/search?" in url:
            body = {"items": [{"id": {"channelId": "UCname"}, "snippet": {"title": "Callen Sounds"}}]}
        elif "/youtube/v3/channels?" in url:
            body = {
                "items": [
                    {
                        "id": "UCname",
                        "snippet": {"title": "Callen Sounds"},
                        "statistics": {"hiddenSubscriberCount": True},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUname"}},
                    }
                ]
            }
        elif "/youtube/v3/playlistItems?" in url:
            body = {"items": []}
        else:
            return HttpResult(404, "", url, {})
        return HttpResult(200, json.dumps(body), url, {})

    bundle = asyncio.run(
        collect_artist(
            "Callen Sounds",
            env={"YOUTUBE_API_KEY": "AIzaExactKey123"},
            http=FakeHttp(respond),
            cache=MemoryCache({}),
        )
    )
    by_platform = {source["platform"]: source for source in bundle["sources"]}
    assert by_platform["musicbrainz"]["status"] == "unavailable"
    assert any("Matched by name" in note for note in by_platform["apple"]["notices"])
    assert any("Matched by name" in note for note in by_platform["youtube"]["notices"])
    assert "not guessed" in by_platform["spotify"]["reason"]
    assert "not guessed" in by_platform["instagram"]["reason"]
    blob = json.dumps(bundle)
    assert "1.2M" not in blob
    assert "AIzaExactKey123" not in blob


def test_catalog_overlaps_the_other_providers(monkeypatch):
    monkeypatch.setattr("providers.musicbrainz.MB_INTERVAL", 0.02)
    monkeypatch.setattr("providers.musicbrainz._mb_next", 0.0)
    events = []

    class DelayHttp(FakeHttp):
        async def get_text(self, url, **kwargs):
            if "release-group" in url:
                events.append("rg-start")
                await asyncio.sleep(0.15)
                events.append("rg-end")
            if "itunes.apple.com" in url:
                events.append("apple-start")
                await asyncio.sleep(0.05)
                events.append("apple-end")
            return await super().get_text(url, **kwargs)

    def respond(method, url, params, data):
        if "musicbrainz.org/ws/2/artist/" in url and params and "query" in (params or {}):
            body = {"artists": [{"id": "mbid-1", "name": "Example Artist", "score": 100}]}
        elif url.endswith("/artist/mbid-1"):
            body = {
                "name": "Example Artist",
                "relations": [{"url": {"resource": "https://music.apple.com/us/artist/example/5"}}],
            }
        elif "release-group" in url:
            body = {"release-groups": []}
        elif "musicbrainz.org/ws/2/release" in url:
            body = {"releases": []}
        elif "itunes.apple.com/lookup" in url:
            body = {
                "results": [
                    {"wrapperType": "artist", "artistId": 5, "artistName": "Example Artist"},
                    {
                        "wrapperType": "collection",
                        "artistId": 5,
                        "collectionId": 8,
                        "collectionName": "Debut",
                        "releaseDate": "2020-01-02T08:00:00Z",
                    },
                ]
            }
        else:
            return HttpResult(404, "", url, {})
        return HttpResult(200, json.dumps(body), url, {})

    asyncio.run(collect_artist("Example Artist", env={}, http=DelayHttp(respond), cache=MemoryCache({})))
    assert events.index("apple-start") < events.index("rg-start") < events.index("apple-end")


def test_youtube_key_prefers_an_aiza_string_over_to_py_garbage():
    real = "AIza" + ("Y" * 35)

    class Both:
        def to_py(self):
            return "not-the-real-key"

        def valueOf(self):
            return real

        def __str__(self):
            return "[object Object]"

    class Env:
        YOUTUBE_API_KEY = Both()

    assert env_get(Env(), "YOUTUBE_API_KEY") is None
    from diag_secrets import describe_secret

    report = describe_secret(Env(), "YOUTUBE_API_KEY")
    blob = json.dumps(report)
    assert real not in blob
    assert "not-the-real-key" not in blob
    assert report["chosen"]["prefix4"] == "AIza"
    assert report["chosen"]["length"] == 39
    assert report["chosen_path"].endswith("valueOf")


def test_cached_ok_results_ignore_stored_errors():
    from models import errored
    from providers.common import CACHE_VERSION, cached

    assert CACHE_VERSION == "v4"

    async def scenario():
        cache = MemoryCache({})
        key = cache_key("youtube", "Example")
        await cache.set(key, json.dumps(errored("youtube", "YouTube", "API key not valid").to_dict()), 2700)
        calls = {"n": 0}

        async def produce():
            calls["n"] += 1
            return errored("youtube", "YouTube", "API key not valid")

        result = await cached(cache, key, produce)
        return calls["n"], result.status, await cache.get(key)

    calls, status, stored = asyncio.run(scenario())
    assert calls == 1
    assert status == "error"
    stored_status = json.loads(stored)["status"]
    assert stored_status == "error"


def test_apple_public_page_is_used_when_itunes_is_forbidden():
    shelf = """
    <script type="application/json" id="serialized-server-data">
    {"data":[{"data":{"seoData":{"pageTitle":"Example - Apple Music"},"sections":[
      {"header":{"item":{"titleLink":{"title":"Albums"}}},"items":[
        {"titleLinks":[{"title":"Debut"}],"subtitleLinks":[{"title":"2020"}],
         "contentDescriptor":{"kind":"album","url":"https://music.apple.com/us/album/debut/8",
           "identifiers":{"storeAdamID":"8"}}}
      ]}
    ]}}]}
    </script>
    """
    album = '<meta property="music:release_date" content="2020-01-02T08:00:00Z">'

    def respond(method, url, params, data):
        if "itunes.apple.com" in url:
            return HttpResult(403, "", url, {})
        if "/album/" in url:
            return HttpResult(200, album, url, {})
        if "music.apple.com" in url:
            return HttpResult(200, shelf, url, {})
        return HttpResult(404, "", url, {})

    result = asyncio.run(fetch_apple("Example", {"apple_id": "5"}, FakeHttp(respond), MemoryCache({})))
    assert result.status == "ok"
    assert result.items[0].display == "2020-01-02 08:00 UTC"
    assert any("music.apple.com" in note for note in result.notices)
    assert any("HTTP 403" in note for note in result.notices)


def test_apple_public_fallback_reads_a_script_slice_and_caps_album_pages():
    assert WEB_EXACT_LIMIT == 3
    rows = []
    for number in range(6):
        rows.append(
            "{"
            f'"titleLinks":[{{"title":"Album {number}"}}],'
            f'"subtitleLinks":[{{"title":"201{number}"}}],'
            '"contentDescriptor":{"kind":"album",'
            f'"url":"https://music.apple.com/us/album/a/{number}",'
            f'"identifiers":{{"storeAdamID":"{number}"}}}}'
            "}"
        )
    shelf = (
        '<script type="application/json" id="serialized-server-data">'
        '{"data":[{"data":{"seoData":{"pageTitle":"Example - Apple Music"},"sections":['
        '{"header":{"item":{"titleLink":{"title":"Albums by Example"}}},"items":['
        + ",".join(rows)
        + "]}]}}]}</script>"
        + ("<p>padding</p>" * 1000)
    )
    profile, albums = parse_apple_albums(shelf)
    assert profile == "Example"
    assert len(albums) == 6
    assert parse_release_meta('<meta content="2020-01-02T08:00:00Z" property="music:release_date">') == (
        "2020-01-02T08:00:00Z"
    )
    album_urls = []

    def respond(method, url, params, data):
        if "itunes.apple.com" in url:
            return HttpResult(403, "", url, {})
        if "/album/" in url:
            album_urls.append(url)
            return HttpResult(200, '<meta property="music:release_date" content="2020-01-02T08:00:00Z">', url, {})
        if "music.apple.com" in url:
            return HttpResult(200, shelf, url, {})
        return HttpResult(404, "", url, {})

    result = asyncio.run(fetch_apple("Example", {"apple_id": "5"}, FakeHttp(respond), MemoryCache({})))
    assert result.status == "ok"
    assert len(album_urls) == 3
    assert sum(item.precision == "time" for item in result.items) == 3
    assert sum(item.precision == "year" for item in result.items) == 3


def test_youtube_misconfigured_key_is_not_sent():
    calls = []

    def respond(method, url, params, data):
        calls.append(url)
        return HttpResult(200, "{}", url, {})

    http = FakeHttp(respond)
    placeholder = asyncio.run(
        fetch_youtube("Example", {}, {"YOUTUBE_API_KEY": "[REDACTED]"}, http, MemoryCache({}))
    )
    short = asyncio.run(fetch_youtube("Example", {}, {"YOUTUBE_API_KEY": "not-a-google-key"}, http, MemoryCache({})))
    assert placeholder.status == "error"
    assert placeholder.reason == "YouTube key is misconfigured"
    assert short.reason == "YouTube key is misconfigured"
    assert calls == []


def test_musicbrainz_reports_status_and_body_and_reuses_cached_relations(monkeypatch):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("providers.musicbrainz.asyncio.sleep", no_sleep)
    monkeypatch.setattr("providers.musicbrainz.MB_INTERVAL", 0)
    monkeypatch.setattr("providers.musicbrainz._mb_next", 0.0)
    calls = []

    def respond(method, url, params, data):
        calls.append(url)
        if "musicbrainz.org" in url:
            return HttpResult(503, "rate limit exceeded for this shared egress IP", url, {})
        if "oembed" in url:
            return HttpResult(200, json.dumps({"title": "Billie Eilish"}), url, {})
        if "open.spotify.com/" in url:
            return HttpResult(200, "<html><body><p>1,234,567 monthly listeners</p></body></html>", url, {})
        if "instagram.com/billieeilish/embed" in url:
            return HttpResult(200, "no timestamps", url, {})
        if "tiktok.com/@billieeilish" in url:
            return HttpResult(200, "<html></html>", url, {})
        if "api.spotify.com/v1/search" in url:
            return HttpResult(
                200,
                json.dumps({"artists": {"items": [{"id": "abc", "name": "Billie Eilish"}]}}),
                url,
                {},
            )
        if "accounts.spotify.com" in url:
            return HttpResult(200, json.dumps({"access_token": "token", "expires_in": 3600}), url, {})
        if "api.spotify.com" in url:
            return HttpResult(200, json.dumps({"name": "Billie Eilish", "followers": {"total": 1}, "external_urls": {}}), url, {})
        return HttpResult(404, "", url, {})

    cache = MemoryCache({})
    bundle = asyncio.run(collect_artist("Billie Eilish", env={}, http=FakeHttp(respond), cache=cache))
    musicbrainz = next(source for source in bundle["sources"] if source["platform"] == "musicbrainz")
    assert musicbrainz["status"] == "error"
    assert "HTTP 503" in musicbrainz["reason"]
    assert "shared egress IP" in musicbrainz["reason"]
    assert sum("musicbrainz.org" in url for url in calls) == 2
    spotify = next(source for source in bundle["sources"] if source["platform"] == "spotify")
    assert "did not return URL relationships" in spotify["reason"]

    calls.clear()
    named = asyncio.run(
        collect_artist(
            "Billie Eilish",
            env={"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": "sec"},
            http=FakeHttp(respond),
            cache=MemoryCache({}),
        )
    )
    assert any("api.spotify.com/v1/search" in url for url in calls)
    named_spotify = next(source for source in named["sources"] if source["platform"] == "spotify")
    assert named_spotify["status"] == "ok"

    calls.clear()
    asyncio.run(
        cache.set(
            cache_key("mb-rels", "Billie Eilish"),
            json.dumps(
                {
                    "links": {
                        "spotify_id": "abc",
                        "apple_id": "",
                        "youtube_channel_ids": [],
                        "youtube_handles": [],
                        "instagram": "billieeilish",
                        "tiktok": "billieeilish",
                    },
                    "name": "Billie Eilish",
                }
            ),
            100,
        )
    )
    cached = asyncio.run(collect_artist("Billie Eilish", env={}, http=FakeHttp(respond), cache=cache))
    joined = " ".join(calls)
    assert "open.spotify.com/artist/abc" in joined
    assert "instagram.com/billieeilish/embed" in joined
    assert "tiktok.com/@billieeilish" in joined
    musicbrainz = next(source for source in cached["sources"] if source["platform"] == "musicbrainz")
    assert "HTTP 503" in musicbrainz["reason"]
    assert any("cached for 7 days" in note for note in musicbrainz["notices"])


def test_musicbrainz_search_relations_skip_the_lookup():
    calls = []

    def respond(method, url, params, data):
        calls.append(url)
        if params and "query" in (params or {}):
            assert params.get("inc") == "url-rels"
            body = {
                "artists": [
                    {
                        "id": "mbid-1",
                        "name": "Example Artist",
                        "score": 100,
                        "relations": [{"url": {"resource": "https://open.spotify.com/artist/abc"}}],
                    }
                ]
            }
            return HttpResult(200, json.dumps(body), url, {})
        if "release-group" in url or url.endswith("/release") or "/release?" in url:
            return HttpResult(200, json.dumps({"release-groups": [], "releases": []}), url, {})
        return HttpResult(500, "should not look up the artist again", url, {})

    from providers.musicbrainz import fetch_musicbrainz

    result = asyncio.run(fetch_musicbrainz("Example Artist", FakeHttp(respond), MemoryCache({})))
    assert result.status == "ok"
    assert result.links["spotify_id"] == "abc"
    assert not any(url.rstrip("/").endswith("/artist/mbid-1") for url in calls)


def test_musicbrainz_requests_are_spaced_and_identified(monkeypatch):
    monkeypatch.setattr("providers.musicbrainz.MB_INTERVAL", 0.05)
    monkeypatch.setattr("providers.musicbrainz._mb_next", 0.0)
    starts = []
    seen_headers = []

    class TimedHttp(FakeHttp):
        async def get_text(self, url, **kwargs):
            if "musicbrainz.org" in url:
                starts.append(asyncio.get_running_loop().time())
                seen_headers.append(kwargs.get("headers") or {})
            return await super().get_text(url, **kwargs)

    def respond(method, url, params, data):
        return HttpResult(200, json.dumps({"artists": []}), url, {})

    from providers.musicbrainz import MB_USER_AGENT, _get_json

    async def run():
        http = TimedHttp(respond)
        await asyncio.gather(
            _get_json(http, "https://musicbrainz.org/ws/2/artist/", {"fmt": "json"}),
            _get_json(http, "https://musicbrainz.org/ws/2/artist/mbid-1", {"fmt": "json"}),
        )

    asyncio.run(run())
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.05
    assert seen_headers[0]["User-Agent"] == MB_USER_AGENT
