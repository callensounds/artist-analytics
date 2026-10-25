import asyncio
import json

from cache import MemoryCache
from http_client import HttpResult
from models import make_item
from providers.apple import fetch_apple, parse_collections
from providers.instagram import parse_embed
from providers.musicbrainz import aggregate_catalog, extract_links
from providers.spotify import fetch_spotify, parse_albums, parse_discography, parse_monthly_listeners, parse_popular, visible_lines
from providers.tiktok import parse_embed_videos, parse_profile, parse_video
from providers.youtube import parse_videos
from providers import collect_artist


class FakeHttp:
    def __init__(self, responder):
        self.responder = responder

    async def get_text(self, url, **kwargs):
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
