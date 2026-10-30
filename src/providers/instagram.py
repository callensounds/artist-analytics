"""Instagram timestamps from the public profile embed page. No login and no OAuth."""

from __future__ import annotations

import json
import re

from analytics import compact_count
from http_client import TransportError
from models import Metric, SourceResult, make_unix_item, sort_items, unavailable
from providers.common import cache_key, cached

# The embed page ships escaped JSON inside a script. Quotes are backslash-escaped.
SHORTCODE_RE = re.compile(r'shortcode\\":\\"([^"\\]+)\\"')
TIMESTAMP_RE = re.compile(r'taken_at_timestamp\\":(\d+)')
USERNAME_RE = re.compile(r'username\\":\\"([^"\\]+)\\"')
CAPTION_RE = re.compile(r'text\\":\\"((?:\\.|[^"\\])*?)\\"')
LIKES_RE = re.compile(r'edge_liked_by\\":\{\\"count\\":(\d+)')
FOLLOWERS_RE = re.compile(r'edge_followed_by\\":\{\\"count\\":(\d+)')
FULL_NAME_RE = re.compile(r'full_name\\":\\"((?:\\.|[^"\\])*?)\\"')


def decode_js(value: str) -> str:
    text = value
    for _ in range(2):
        try:
            decoded = json.loads(f'"{text}"')
        except json.JSONDecodeError:
            break
        if decoded == text:
            break
        text = decoded
    return text


def login_wall(result) -> bool:
    if result.status in {301, 302, 303, 307, 308}:
        location = result.header("location").lower()
        return "login" in location or "challenge" in location
    return False


def parse_embed(html: str, handle: str, *, light: bool = False) -> dict:
    """Pair each taken_at_timestamp with the nearest preceding shortcode."""
    posts = []
    seen = set()
    followers = None
    full_name = None
    private = False
    page = html or ""
    post_cap = 4 if light else 12
    for stamp in TIMESTAMP_RE.finditer(page):
        if len(posts) >= post_cap:
            break
        before = page[max(0, stamp.start() - 8000) : stamp.start()]
        after = page[stamp.end() : stamp.end() + 2500]
        shortcodes = list(SHORTCODE_RE.finditer(before))
        username = USERNAME_RE.search(after)
        if not shortcodes or not username:
            continue
        if username.group(1).casefold() != handle.casefold():
            continue
        shortcode = shortcodes[-1].group(1)
        if shortcode in seen:
            continue
        seen.add(shortcode)
        if 'is_private\\":true' in after:
            private = True
        # Caption and like scans walk the same window again. A crawl only needs the time and the follower count.
        if light:
            caption = shortcode
            likes = []
        else:
            captions = list(CAPTION_RE.finditer(before))
            caption = decode_js(captions[-1].group(1)) if captions else shortcode
            likes = list(LIKES_RE.finditer(before))
        follower_match = FOLLOWERS_RE.search(after) or FOLLOWERS_RE.search(before)
        name_match = FULL_NAME_RE.search(after) or FULL_NAME_RE.search(before)
        if follower_match and followers is None:
            followers = int(follower_match.group(1))
        if name_match and not full_name:
            full_name = decode_js(name_match.group(1))
        title = caption.strip().split("\n", 1)[0][:140] or shortcode
        like_count = likes[-1].group(1) if likes else None
        item = make_unix_item(
            title=title,
            kind="post",
            stamp=stamp.group(1),
            url=f"https://www.instagram.com/p/{shortcode}/",
            detail=f"{compact_count(like_count)} likes" if like_count else None,
        )
        if item is not None:
            posts.append(item)
    return {
        "posts": sort_items(posts),
        "followers": followers,
        "full_name": full_name,
        "private": private,
    }


async def fetch_instagram(name: str, links: dict, http, cache) -> SourceResult:
    handle = (links or {}).get("instagram") or ""
    if (links or {}).get("_incomplete"):
        return unavailable(
            "instagram",
            "Instagram",
            "MusicBrainz did not return URL relationships, so the official Instagram profile was not looked up. "
            "A handle is not guessed.",
            ttl=120,
        )
    if not handle:
        return unavailable(
            "instagram",
            "Instagram",
            "MusicBrainz has no official Instagram URL for this artist. "
            "A handle is not guessed, so the wrong account cannot be shown.",
            ttl=1800,
        )

    async def produce() -> SourceResult:
        url = f"https://www.instagram.com/{handle}/embed/"
        try:
            result = await http.get_text(
                url,
                timeout=15,
                allow_redirects=False,
                max_bytes=48_000,
            )
        except TransportError as exc:
            return unavailable("instagram", "Instagram", str(exc))
        if login_wall(result):
            return unavailable(
                "instagram",
                "Instagram",
                "Instagram redirected to a login wall. Follower counts and post times were not read.",
            )
        if result.status in {403, 429}:
            return unavailable(
                "instagram",
                "Instagram",
                f"Instagram blocked the public embed request (HTTP {result.status}).",
            )
        if not result.ok:
            return unavailable(
                "instagram",
                "Instagram",
                f"The public embed page returned HTTP {result.status}.",
            )
        parsed = parse_embed((result.text or "")[:48_000], handle, light=True)
        if parsed["private"] and not parsed["posts"]:
            return unavailable(
                "instagram",
                "Instagram",
                "The public embed marks this account private, so post timestamps are not shown.",
            )
        if parsed["followers"] is None and not parsed["posts"]:
            return unavailable(
                "instagram",
                "Instagram",
                "The public embed page did not include follower counts or taken_at_timestamp values. "
                "The markup may have changed.",
            )
        follower_exact = parsed["followers"] if isinstance(parsed["followers"], int) else None
        follower_value = compact_count(follower_exact) if follower_exact is not None else None
        return SourceResult(
            platform="instagram",
            label="Instagram",
            status="ok",
            profile_name=parsed["full_name"] or handle,
            profile_url=f"https://www.instagram.com/{handle}/",
            metrics=[
                Metric(
                    label="Followers",
                    available=follower_value is not None,
                    value=follower_value,
                    exact=follower_exact,
                    reason=None
                    if follower_value is not None
                    else "The embed page did not include edge_followed_by.",
                ),
                Metric(label="Posts with timestamps", available=True, value=str(len(parsed["posts"]))),
            ],
            items=parsed["posts"],
            notices=[
                "Post times are taken_at_timestamp values from Instagram's public /embed/ page, in UTC. "
                "That page only includes a handful of posts. "
                "The profile URL itself often redirects to a login wall and is not followed."
            ],
        )

    return await cached(cache, cache_key("instagram", handle), produce)
