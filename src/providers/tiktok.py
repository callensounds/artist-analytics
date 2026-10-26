"""TikTok followers and createTime values from public profile, embed, and video pages."""

from __future__ import annotations

import asyncio
import json

from analytics import compact_count
from http_client import TransportError
from models import Metric, SourceResult, make_unix_item, sort_items, unavailable
from providers.common import cache_key, cached, script_body

MAX_VIDEOS = 6
PAGE_MAX_BYTES = 400_000


def script_json(html: str, script_id: str):
    raw = script_body(html or "", f'id="{script_id}"')
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, MemoryError, RecursionError, ValueError):
        return None


def login_wall(result) -> bool:
    if result.status in {301, 302, 303, 307, 308}:
        location = result.header("location").lower()
        return any(token in location for token in ("/login", "captcha", "challenge"))
    return False


def parse_profile(html: str) -> dict | None:
    payload = script_json(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    if not isinstance(payload, dict):
        return None
    detail = (payload.get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail")
    if not isinstance(detail, dict):
        return None
    status = detail.get("statusCode")
    if status not in (0, None, "0"):
        return {"error": detail.get("statusMsg") or f"TikTok status {status}"}
    info = detail.get("userInfo") or {}
    user = info.get("user") or {}
    if not user.get("uniqueId"):
        return None
    return {"user": user, "stats": info.get("stats") or {}}


def parse_embed_videos(html: str, handle: str) -> list[dict]:
    payload = script_json(html, "__FRONTITY_CONNECT_STATE__")
    if not isinstance(payload, dict):
        return []
    data = ((payload.get("source") or {}).get("data") or {})
    block = data.get(f"/embed/@{handle}")
    if not isinstance(block, dict):
        block = next(
            (
                value
                for value in data.values()
                if isinstance(value, dict) and isinstance(value.get("videoList"), list)
            ),
            None,
        )
    if not isinstance(block, dict):
        return []
    videos = []
    for item in block.get("videoList") or []:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "")
        author = str(item.get("authorUniqueId") or handle)
        if not video_id.isdigit() or author.casefold() != handle.casefold():
            continue
        videos.append({"id": video_id, "desc": item.get("desc") or ""})
    return videos[:MAX_VIDEOS]


def parse_video(html: str) -> dict | None:
    payload = script_json(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    if not isinstance(payload, dict):
        return None
    item = (
        (payload.get("__DEFAULT_SCOPE__") or {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct")
    )
    if not isinstance(item, dict) or item.get("createTime") in (None, ""):
        return None
    return item


async def _get(http, url: str):
    try:
        return await http.get_text(url, timeout=15, allow_redirects=False, max_bytes=PAGE_MAX_BYTES)
    except TransportError as exc:
        raise TransportError(str(exc)) from exc


async def fetch_tiktok(name: str, links: dict, http, cache) -> SourceResult:
    handle = (links or {}).get("tiktok") or ""
    if (links or {}).get("_incomplete"):
        return unavailable(
            "tiktok",
            "TikTok",
            "MusicBrainz did not return URL relationships, so the official TikTok profile was not looked up. "
            "A handle is not guessed.",
            ttl=120,
        )
    if not handle:
        return unavailable(
            "tiktok",
            "TikTok",
            "MusicBrainz has no official TikTok URL for this artist. "
            "A handle is not guessed, so the wrong account cannot be shown.",
            ttl=1800,
        )

    async def produce() -> SourceResult:
        profile_url = f"https://www.tiktok.com/@{handle}"
        try:
            profile_page = await _get(http, profile_url)
        except TransportError as exc:
            return unavailable("tiktok", "TikTok", str(exc))
        if login_wall(profile_page):
            return unavailable(
                "tiktok",
                "TikTok",
                "TikTok redirected to a login or challenge page. Public profile data was not read.",
            )
        if profile_page.status in {403, 429}:
            return unavailable("tiktok", "TikTok", f"TikTok blocked the profile request (HTTP {profile_page.status}).")
        profile = parse_profile(profile_page.text) if profile_page.ok else None
        if profile is None:
            return unavailable(
                "tiktok",
                "TikTok",
                "TikTok's public profile page did not include user stats. The request may be blocked, or the markup changed.",
            )
        if profile.get("error"):
            return unavailable("tiktok", "TikTok", f"TikTok profile status: {profile['error']}")
        user = profile["user"]
        stats = profile["stats"]
        if user.get("privateAccount"):
            return SourceResult(
                platform="tiktok",
                label="TikTok",
                status="ok",
                profile_name=user.get("nickname") or handle,
                profile_url=profile_url,
                metrics=[
                    Metric(
                        label="Followers",
                        available=stats.get("followerCount") is not None,
                        value=compact_count(stats.get("followerCount")),
                    ),
                    Metric(
                        label="Post timestamps",
                        available=False,
                        reason="The account is marked private on the public profile payload.",
                    ),
                ],
                notices=["Follower count is from the public profile JSON. Private accounts are not scraped further."],
            )
        videos = []
        notice = None
        try:
            embed = await _get(http, f"https://www.tiktok.com/embed/@{handle}")
        except TransportError:
            embed = None
        if embed is None or not embed.ok or login_wall(embed):
            notice = "The public embed listing was unavailable, so post timestamps could not be collected."
        else:
            videos = parse_embed_videos(embed.text, handle)
            if not videos:
                notice = "The public embed listing did not include video ids, so createTime could not be read."

        async def load_video(video: dict):
            page = await _get(http, f"https://www.tiktok.com/@{handle}/video/{video['id']}")
            if not page.ok or login_wall(page):
                return None
            item = parse_video(page.text)
            if item is None:
                return None
            stats_row = item.get("stats") or {}
            plays = compact_count(stats_row.get("playCount"))
            title = (video.get("desc") or item.get("desc") or video["id"]).strip().split("\n", 1)[0][:140]
            return make_unix_item(
                title=title,
                kind="post",
                stamp=item.get("createTime"),
                url=f"https://www.tiktok.com/@{handle}/video/{video['id']}",
                detail=f"{plays} plays" if plays else None,
            )

        items = []
        if videos:
            results = await asyncio.gather(*(load_video(video) for video in videos), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) or result is None:
                    continue
                items.append(result)
            if videos and not items:
                notice = "Public video pages did not include createTime, so post timestamps are unavailable."
        followers = compact_count(stats.get("followerCount"))
        hearts = compact_count(stats.get("heartCount"))
        return SourceResult(
            platform="tiktok",
            label="TikTok",
            status="ok",
            profile_name=user.get("nickname") or handle,
            profile_url=profile_url,
            metrics=[
                Metric(
                    label="Followers",
                    available=followers is not None,
                    value=followers,
                    reason=None if followers is not None else "The profile payload had no followerCount.",
                ),
                Metric(
                    label="Likes",
                    available=hearts is not None,
                    value=hearts,
                    reason=None if hearts is not None else "The profile payload had no heartCount.",
                ),
                Metric(label="Posts with timestamps", available=True, value=str(len(items))),
            ],
            items=sort_items(items),
            notices=[
                notice,
                "Follower and like counts come from TikTok's public profile JSON. "
                "Post times are createTime on public video pages (UTC, second precision), "
                "for up to 6 videos listed on the public embed page. "
                "The oEmbed endpoint identifies the account but does not include these timestamps. "
                "Snowflake ids are not decoded into fake times.",
            ],
        )

    result = await cached(cache, cache_key("tiktok", handle), produce)
    result.notices = [note for note in result.notices if note]
    return result
