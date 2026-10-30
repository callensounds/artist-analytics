"""Pure timestamp and pattern helpers. No network and no guessed metrics."""

from __future__ import annotations

import re
from datetime import datetime, timezone

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
PRECISION_RANK = {"unknown": 0, "year": 1, "month": 2, "day": 3, "time": 4}
PRECISION_LABELS = {
    "time": "Exact time",
    "day": "Exact day",
    "month": "Month only",
    "year": "Year only",
    "unknown": "Unknown",
}
SOURCE_COLORS = {
    "musicbrainz": "#34d399",
    "apple": "#fb7185",
    "spotify": "#4ade80",
    "youtube": "#f87171",
    "instagram": "#c084fc",
    "tiktok": "#22d3ee",
}
RELEASE_PLATFORMS = {"musicbrainz", "apple", "spotify"}
SOCIAL_PLATFORMS = {"youtube", "instagram", "tiktok"}
PATTERN_RELEASE_TYPES = {"Album", "Single", "EP"}
EXCLUDED_SECONDARY = {"Bootleg", "Demo", "Interview", "Audiobook"}


def classify_date(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return "unknown"
    date_part, sep, time_part = raw.partition("T")
    if not sep and " " in date_part:
        date_part, time_part = date_part.split(" ", 1)
    pieces = [part for part in date_part.split("-") if part]
    if not pieces or not pieces[0].isdigit() or len(pieces[0]) != 4:
        return "unknown"
    if len(pieces) >= 3 and pieces[1].isdigit() and pieces[2][:2].isdigit():
        if time_part.strip():
            return "time"
        return "day"
    if len(pieces) == 2 and pieces[1].isdigit():
        return "month"
    if len(pieces) == 1:
        return "year"
    return "unknown"


def calendar_parts(value: str) -> list[int]:
    precision = classify_date(value)
    if precision == "unknown":
        return []
    date_part = value.strip().split("T", 1)[0].split(" ", 1)[0]
    numbers = []
    for part in date_part.split("-"):
        head = part[:2] if len(numbers) == 2 else part
        if not head.isdigit():
            break
        numbers.append(int(head))
    return numbers


def is_strictly_earlier(left: str, right: str) -> bool:
    """True when left is an earlier calendar date, not merely less precise."""
    a = calendar_parts(left)
    b = calendar_parts(right)
    if not a or not b:
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        return x < y
    return False


def choose_best_date(candidates: list[tuple[str, str | None]]) -> tuple[str, str | None]:
    """Keep the earliest release, then the most precise date within that period."""
    valid = []
    for raw, place in candidates:
        text = (raw or "").strip()
        if classify_date(text) == "unknown":
            continue
        valid.append((text, place or None))
    if not valid:
        return "", None
    # One pass per calendar field. The old check compared every candidate to every
    # other candidate, which got expensive on a release group with many events.
    min_year = min(calendar_parts(text)[0] for text, _ in valid)
    valid = [item for item in valid if calendar_parts(item[0])[0] == min_year]
    with_month = [item for item in valid if len(calendar_parts(item[0])) >= 2]
    if with_month:
        min_month = min(calendar_parts(item[0])[1] for item in with_month)
        valid = [item for item in valid if len(calendar_parts(item[0])) < 2 or calendar_parts(item[0])[1] == min_month]
        with_day = [item for item in valid if len(calendar_parts(item[0])) >= 3]
        if with_day:
            min_day = min(calendar_parts(item[0])[2] for item in with_day)
            valid = [item for item in valid if len(calendar_parts(item[0])) < 3 or calendar_parts(item[0])[2] == min_day]
    text, place = max(
        valid,
        key=lambda item: (
            PRECISION_RANK[classify_date(item[0])],
            1 if item[1] else 0,
        ),
    )
    return text, place


def spotify_precision(date_value: str, stated: str | None) -> str:
    """Never treat a Spotify date as more precise than release_date_precision."""
    parsed = classify_date(date_value)
    stated_key = (stated or "").strip().lower()
    if stated_key not in PRECISION_RANK:
        return parsed
    if PRECISION_RANK[stated_key] < PRECISION_RANK.get(parsed, 0):
        return stated_key
    return parsed


def parse_utc(value: str) -> datetime | None:
    """ISO-8601 only. dateutil.parser pulled a large pure-Python module into every cold start."""
    raw = (value or "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_unix(value: object) -> datetime | None:
    try:
        stamp = int(str(value))
    except (TypeError, ValueError):
        return None
    if stamp > 10_000_000_000:  # milliseconds
        stamp //= 1000
    if stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def weekday_name(value: datetime | None) -> str | None:
    if value is None:
        return None
    return WEEKDAYS[value.weekday()]


def display_timestamp(precision: str, when: datetime | None, raw: str) -> str:
    if precision == "time" and when is not None:
        return when.strftime("%Y-%m-%d %H:%M UTC")
    if precision == "day" and when is not None:
        return when.strftime("%Y-%m-%d")
    if precision == "month":
        parts = calendar_parts(raw)
        if len(parts) >= 2:
            return f"{parts[0]:04d}-{parts[1]:02d}"
    if precision == "year":
        parts = calendar_parts(raw)
        if parts:
            return f"{parts[0]:04d}"
    return raw


def compact_count(value: object) -> str | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    scales = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for size, suffix in scales:
        if number >= size:
            scaled = number / size
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(number)


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def sanitize_artist_query(value: str) -> str:
    cleaned = re.sub(r"[+\-!(){}\[\]^\"~*?:\\/]|&&|\|\|", " ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120]


def names_match(left: str, right: str) -> bool:
    """Exact match after case and punctuation folding. Substrings are different artists."""
    a = normalize_name(left)
    b = normalize_name(right)
    return bool(a and b and a == b)


def peak(counts: list[int], labels: list[str]) -> dict | None:
    total = sum(counts)
    if total <= 0:
        return None
    index = max(range(len(counts)), key=lambda i: (counts[i], -i))
    if counts[index] <= 0:
        return None
    return {
        "label": labels[index],
        "count": counts[index],
        "total": total,
        "share": counts[index] / total,
    }


def empty_hours() -> list[int]:
    return [0] * 24


def empty_weekdays() -> list[int]:
    return [0] * 7


def hour_labels() -> list[str]:
    return [f"{hour:02d}" for hour in range(24)]


def _dataset(platform: str, label: str, data: list[int]) -> dict | None:
    if not any(data):
        return None
    return {
        "platform": platform,
        "label": label,
        "data": data,
        "color": SOURCE_COLORS.get(platform, "#94a3b8"),
    }


def build_patterns(sources: list[dict]) -> dict:
    release_hours: dict[str, list[int]] = {}
    social_hours: dict[str, list[int]] = {}
    weekdays: dict[str, list[int]] = {}
    labels = {source["platform"]: source["label"] for source in sources}
    for source in sources:
        platform = source["platform"]
        for item in source.get("items") or []:
            precision = item.get("precision")
            if precision in {"time", "day"} and item.get("weekday"):
                bucket = weekdays.setdefault(platform, empty_weekdays())
                bucket[WEEKDAYS.index(item["weekday"])] += 1
            if precision == "time" and item.get("hour_utc") is not None:
                target = release_hours if platform in RELEASE_PLATFORMS else social_hours
                if platform not in RELEASE_PLATFORMS and platform not in SOCIAL_PLATFORMS:
                    target = social_hours
                bucket = target.setdefault(platform, empty_hours())
                bucket[int(item["hour_utc"])] += 1
    return {
        "hour_labels": hour_labels(),
        "weekday_labels": WEEKDAYS,
        "release_hours": [
            row
            for platform, counts in release_hours.items()
            if (row := _dataset(platform, labels.get(platform, platform), counts))
        ],
        "social_hours": [
            row
            for platform, counts in social_hours.items()
            if (row := _dataset(platform, labels.get(platform, platform), counts))
        ],
        "weekdays": [
            row
            for platform, counts in weekdays.items()
            if (row := _dataset(platform, labels.get(platform, platform), counts))
        ],
    }


def build_insights(sources: list[dict]) -> list[str]:
    insights: list[str] = []
    for source in sources:
        items = source.get("items") or []
        hours = empty_hours()
        days = empty_weekdays()
        partial = 0
        for item in items:
            precision = item.get("precision")
            if precision == "time" and item.get("hour_utc") is not None:
                hours[int(item["hour_utc"])] += 1
            if precision in {"time", "day"} and item.get("weekday"):
                days[WEEKDAYS.index(item["weekday"])] += 1
            elif precision in {"month", "year"}:
                partial += 1
        hour_peak = peak(hours, [f"{hour:02d}:00 UTC" for hour in range(24)])
        if hour_peak:
            insights.append(
                f"{source['label']} peaks at {hour_peak['label']} "
                f"({hour_peak['count']} of {hour_peak['total']} exact timestamps)."
            )
        day_peak = peak(days, WEEKDAYS)
        if day_peak:
            insights.append(
                f"{day_peak['label']} is the most common {source['label']} weekday "
                f"({day_peak['count']} of {day_peak['total']} day-level dates)."
            )
        if partial and not day_peak:
            insights.append(
                f"{source['label']} has {partial} month- or year-only dates, "
                "so they are left out of the weekday chart."
            )
    if not insights:
        insights.append(
            "No day-level dates or exact timestamps were returned for this artist yet."
        )
    return insights[:8]


def scrub(text: str, secrets: list[str]) -> str:
    cleaned = text or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:300]
