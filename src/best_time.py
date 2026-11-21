"""Most common post and release clocks from rows already loaded.

No database access and no writes. Each exact timestamp is shifted on its own,
so a July instant and a January instant do not share a fixed offset. A date
with no clock is counted as excluded and is not given an hour. The result is
a frequency of stored clocks, not an engagement score.
"""

from __future__ import annotations

from datetime import datetime, timezone

from analytics import WEEKDAYS, WEEKDAY_SHORT, classify_date, parse_utc, peak
from timezones import UTC, normalize_tz, shift, zone_label
from timing import HOUR_NOTE, MODAL_MIN_EXACT

# A weekday-and-hour slot needs a wider sample than a single hour.
SLOT_MIN_EXACT = 5
POST_SOURCES = frozenset({"instagram", "tiktok", "youtube"})
# The artist page and the CSV export already cap their lists. This stops a
# caller from walking a longer list than those reads return.
_ROW_CAP = 5000
_VIEW_KEYS = ("views", "view_count")
_LIKE_KEYS = ("likes", "like_count")
BASIS = (
    "Based on when this artist has historically posted or released "
    "(frequency of exact timestamps), not on engagement."
)


def best_time(history: dict | None, tz: str | None = None) -> dict:
    """Posts and releases, separately. ``history`` is the dict already in memory."""
    zone = normalize_tz(tz)
    loaded = history if isinstance(history, dict) else {}
    post_events, post_excluded = _post_events(loaded)
    release_events, release_excluded = _release_events(loaded)
    posts = _summarize(post_events, zone, "posts", excluded=post_excluded, engagement=True)
    releases = _summarize(release_events, zone, "releases", excluded=release_excluded, engagement=False)
    posts["heading"] = "Posts"
    posts["anchor"] = "best-time-posts"
    releases["heading"] = "Releases"
    releases["anchor"] = "best-time-releases"
    return {
        "timezone": zone,
        "timezone_label": zone_label(zone),
        "basis": BASIS,
        "hour_note": HOUR_NOTE,
        "posts": posts,
        "releases": releases,
        "sections": [posts, releases],
    }


def export_fields(history: dict | None, tz: str | None = None) -> dict[str, str]:
    """CSV cells from rows the export already loaded. Empty when there is no slot."""
    from export_csv import formula_safe

    result = best_time(history, tz)
    posts = result["posts"]
    releases = result["releases"]
    cells = {
        "best_post_slot_local": _csv_slot(posts, "local"),
        "best_post_slot_utc": _csv_slot(posts, "utc"),
        "best_post_n": str(posts["n"]),
        "best_release_slot_local": _csv_slot(releases, "local"),
        "best_release_slot_utc": _csv_slot(releases, "utc"),
        "best_release_n": str(releases["n"]),
    }
    return {key: formula_safe(value) for key, value in cells.items()}


def _csv_slot(series: dict, side: str) -> str:
    slot = series.get("slot")
    if not slot:
        return ""
    if side == "local":
        label = slot["local_label"]
        tied = slot["tied"]
    else:
        label = slot["utc_label"]
        tied = slot["utc_tied"]
    if tied:
        return f"{label} tied"
    return label


def _bounded(rows) -> list:
    if not isinstance(rows, list):
        return []
    if len(rows) <= _ROW_CAP:
        return rows
    return rows[:_ROW_CAP]


def _exact_moment(raw, stated) -> datetime | None:
    """An exact UTC instant. A coarser stored precision is not upgraded.

    Same rule as posting patterns: the stored precision must say ``time`` and
    the string must contain a clock. A date-only value is not read as 00:00.
    """
    text = str(raw or "").strip()
    if str(stated or "").strip().lower() != "time" or classify_date(text) != "time":
        return None
    moment = parse_utc(text)
    if moment is None:
        return None
    return moment.astimezone(timezone.utc)


def _event_key(source: str, moment: datetime) -> tuple[str, str]:
    return source, moment.strftime("%Y-%m-%dT%H:%M:%S")


def _post_events(history: dict) -> tuple[list[tuple[datetime, dict]], int]:
    """Instagram, TikTok, and YouTube only. Apple clocks stay out of posts.

    Sightings are read first so a row that already carries views or likes wins
    over the same instant in the posting-pattern stamp list. The stamp list is
    the longer exact-time sample the page already loaded (the sighting list is
    shorter and mixed). The same source and second is counted once.
    """
    events: list[tuple[datetime, dict]] = []
    seen: set[tuple[str, str]] = set()
    excluded = 0
    for row in _bounded(history.get("sightings")):
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip().lower()
        if source not in POST_SOURCES:
            continue
        moment = _exact_moment(row.get("published_at"), row.get("precision"))
        if moment is None:
            excluded += 1
            continue
        key = _event_key(source, moment)
        if key in seen:
            continue
        seen.add(key)
        events.append((moment, row))
    posting = history.get("posting") if isinstance(history.get("posting"), dict) else {}
    for row in _bounded(posting.get("exact_stamps")):
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip().lower()
        if source not in POST_SOURCES:
            continue
        moment = _exact_moment(row.get("published_at"), row.get("precision") or "time")
        if moment is None:
            excluded += 1
            continue
        key = _event_key(source, moment)
        if key in seen:
            continue
        seen.add(key)
        events.append((moment, {}))
    return events, excluded


def _release_events(history: dict) -> tuple[list[tuple[datetime, dict]], int]:
    events: list[tuple[datetime, dict]] = []
    excluded = 0
    for row in _bounded(history.get("releases")):
        if not isinstance(row, dict):
            continue
        raw = row.get("released_at")
        if raw is None or str(raw).strip() == "":
            raw = row.get("published_at")
        moment = _exact_moment(raw, row.get("precision"))
        if moment is None:
            excluded += 1
            continue
        events.append((moment, row))
    return events, excluded


def _whole(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _engagement_value(row: dict) -> tuple[str, int] | None:
    """A stored views or likes count. Strings and missing keys are not parsed."""
    if not isinstance(row, dict):
        return None
    for key in _VIEW_KEYS:
        if key in row:
            number = _whole(row.get(key))
            return ("views", number) if number is not None else None
    for key in _LIKE_KEYS:
        if key in row:
            number = _whole(row.get(key))
            return ("likes", number) if number is not None else None
    return None


def _note_pair(table: list[int], index: int, utc_index: int) -> None:
    current = table[index]
    if current == -2:
        table[index] = utc_index
    elif current != utc_index:
        table[index] = -1


def _winner(counts: list[int]) -> dict | None:
    """Earliest index wins a tie, the same rule as ``analytics.peak``."""
    found = peak(counts, [str(index) for index in range(len(counts))])
    if found is None:
        return None
    top = found["count"]
    return {
        "index": int(found["label"]),
        "count": top,
        "total": found["total"],
        "tied": sum(1 for count in counts if count == top) > 1,
    }


def _span(hour: int) -> str:
    end = hour + 1
    end_label = f"{end:02d}:00" if end < 24 else "24:00"
    return f"{hour:02d}:00-{end_label}"


def _share(count: int, total: int, noun: str) -> str:
    return f"{count} of {total} exact {noun}"


def _median(values: list[int]) -> int | float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    left = ordered[mid - 1]
    right = ordered[mid]
    if (left + right) % 2 == 0:
        return (left + right) // 2
    return (left + right) / 2


def _format_count(number: int | float) -> str:
    if isinstance(number, float) and not number.is_integer():
        return f"{number:.1f}"
    return f"{int(number):,}"


def _clock_text(
    local_label: str,
    utc_label: str,
    zone: str,
    *,
    aligned: bool,
    local_tied: bool,
    utc_tied: bool,
    local_count: int,
    utc_count: int,
    total: int,
    noun: str,
    utc_suffix: str,
) -> str:
    local_share = _share(local_count, total, noun)
    utc_share = _share(utc_count, total, noun)
    if zone == UTC:
        body = f"{utc_label} {utc_suffix}".rstrip()
        if local_tied or utc_tied:
            return f"{body} (tied, {utc_share})"
        return f"{body} · {utc_share}"
    if aligned and local_count == utc_count:
        body = f"{local_label} {zone} = {utc_label} {utc_suffix}".rstrip()
        if local_tied or utc_tied:
            return f"{body} (tied, {local_share})"
        return f"{body} · {local_share}"
    local_bit = f"{local_label} {zone}"
    utc_bit = f"{utc_label} {utc_suffix}".rstrip()
    if local_tied:
        local_bit += " (tied)"
    if utc_tied:
        utc_bit += " (tied)"
    return f"{local_bit} ({local_share}). {utc_bit} ({utc_share})."


def _hour_result(local_hours, utc_hours, hour_pair, zone: str, noun: str) -> dict | None:
    local = _winner(local_hours)
    utc = _winner(utc_hours)
    if local is None or utc is None:
        return None
    paired = hour_pair[local["index"]]
    aligned = paired >= 0 and paired == utc["index"]
    local_label = _span(local["index"])
    utc_label = _span(utc["index"])
    return {
        "local_hour": local["index"],
        "utc_hour": utc["index"],
        "count": local["count"],
        "utc_count": utc["count"],
        "total": local["total"],
        "tied": local["tied"],
        "utc_tied": utc["tied"],
        "share": _share(local["count"], local["total"], noun),
        "utc_share": _share(utc["count"], utc["total"], noun),
        "local_label": local_label,
        "utc_label": utc_label,
        "aligned": aligned,
        "display": _clock_text(
            local_label,
            utc_label,
            zone,
            aligned=aligned,
            local_tied=local["tied"],
            utc_tied=utc["tied"],
            local_count=local["count"],
            utc_count=utc["count"],
            total=local["total"],
            noun=noun,
            utc_suffix="UTC",
        ),
    }


def _slot_result(local_slots, utc_slots, slot_pair, zone: str, noun: str) -> dict | None:
    local = _winner(local_slots)
    utc = _winner(utc_slots)
    if local is None or utc is None:
        return None
    local_day, local_hour = divmod(local["index"], 24)
    utc_day, utc_hour = divmod(utc["index"], 24)
    paired = slot_pair[local["index"]]
    aligned = paired >= 0 and paired == utc["index"]
    local_label = f"{WEEKDAY_SHORT[local_day]} {_span(local_hour)}"
    utc_label = f"{WEEKDAY_SHORT[utc_day]} {_span(utc_hour)}"
    return {
        "local_weekday": WEEKDAYS[local_day],
        "local_weekday_short": WEEKDAY_SHORT[local_day],
        "local_hour": local_hour,
        "utc_weekday": WEEKDAYS[utc_day],
        "utc_weekday_short": WEEKDAY_SHORT[utc_day],
        "utc_hour": utc_hour,
        "count": local["count"],
        "utc_count": utc["count"],
        "total": local["total"],
        "tied": local["tied"],
        "utc_tied": utc["tied"],
        "share": _share(local["count"], local["total"], noun),
        "utc_share": _share(utc["count"], utc["total"], noun),
        "local_label": local_label,
        "utc_label": utc_label,
        "aligned": aligned,
        "display": _clock_text(
            local_label,
            utc_label,
            zone,
            aligned=aligned,
            local_tied=local["tied"],
            utc_tied=utc["tied"],
            local_count=local["count"],
            utc_count=utc["count"],
            total=local["total"],
            noun=noun,
            utc_suffix="UTC",
        ),
    }


def _median_line(view_buckets, like_buckets, hour_index: int, zone: str, label: str) -> dict | None:
    views = view_buckets[hour_index]
    likes = like_buckets[hour_index]
    if len(views) >= MODAL_MIN_EXACT:
        metric, values = "views", views
    elif len(likes) >= MODAL_MIN_EXACT:
        metric, values = "likes", likes
    else:
        return None
    median = _median(values)
    zone_name = UTC if zone == UTC else zone
    return {
        "metric": metric,
        "n": len(values),
        "median": median,
        "text": (
            f"Median {metric} in {label} {zone_name}: {_format_count(median)}, "
            f"from {len(values)} posts in that hour."
        ),
    }


def _summarize(events, zone: str, noun: str, *, excluded: int, engagement: bool) -> dict:
    n = len(events)
    local_hours = [0] * 24
    utc_hours = [0] * 24
    local_slots = [0] * 168
    utc_slots = [0] * 168
    hour_pair = [-2] * 24
    slot_pair = [-2] * 168
    view_buckets = [[] for _ in range(24)] if engagement else None
    like_buckets = [[] for _ in range(24)] if engagement else None
    for moment, row in events:
        utc_hour = moment.hour
        utc_day = moment.weekday()
        if zone == UTC:
            local_hour = utc_hour
            local_day = utc_day
        else:
            local = shift(moment, zone)
            local_hour = local.hour
            local_day = local.weekday()
        local_hours[local_hour] += 1
        utc_hours[utc_hour] += 1
        local_index = local_day * 24 + local_hour
        utc_index = utc_day * 24 + utc_hour
        local_slots[local_index] += 1
        utc_slots[utc_index] += 1
        _note_pair(hour_pair, local_hour, utc_hour)
        _note_pair(slot_pair, local_index, utc_index)
        if engagement:
            found = _engagement_value(row)
            if found is not None and found[0] == "views":
                view_buckets[local_hour].append(found[1])
            elif found is not None:
                like_buckets[local_hour].append(found[1])
    hour = _hour_result(local_hours, utc_hours, hour_pair, zone, noun) if n >= MODAL_MIN_EXACT else None
    slot = _slot_result(local_slots, utc_slots, slot_pair, zone, noun) if n >= SLOT_MIN_EXACT else None
    if n >= SLOT_MIN_EXACT:
        status = "ok"
        message = ""
    else:
        status = "insufficient"
        if n < MODAL_MIN_EXACT:
            message = f"Not enough exact timestamps (n={n}) to suggest a time."
        else:
            message = f"Not enough exact timestamps (n={n}) to suggest a weekday and hour."
    median = None
    if engagement and hour is not None:
        median = _median_line(view_buckets, like_buckets, hour["local_hour"], zone, hour["local_label"])
    return {
        "status": status,
        "n": n,
        "excluded": excluded,
        "message": message,
        "slot": slot,
        "hour": hour,
        "engagement": median,
    }
