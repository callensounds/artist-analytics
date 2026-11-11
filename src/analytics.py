"""Pure timestamp and pattern helpers. No network and no guessed metrics."""

from __future__ import annotations

import html
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
WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
# Exact clock only. A day, month, or year is not a posting hour.
POSTING_SOURCES = ("instagram", "tiktok", "youtube", "apple")
POSTING_LABELS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "apple": "Apple Music",
}
POSTING_MIN_EXACT = 3
CALENDAR_SOURCES = ("musicbrainz", "apple", "spotify", "deezer")
CALENDAR_LABELS = {
    "musicbrainz": "MusicBrainz",
    "apple": "Apple Music",
    "spotify": "Spotify",
    "deezer": "Deezer",
}
CALENDAR_MONTH_CAP = 12
CALENDAR_CELL_CAP = 3
POSTING_NONE = (
    "No exact UTC timestamp is stored for Instagram, TikTok, YouTube, or Apple. "
    "Year, month, and day-only dates are excluded, and no hour is guessed."
)
CALENDAR_EMPTY = (
    "No MusicBrainz, Apple, Spotify, or Deezer release date is stored yet. "
    "Year-only dates are not placed on a day."
)
_PERIOD_RE = re.compile(r"\A(\d{4})(?:-(\d{2}))?\Z")
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
    "deezer": "#a78bfa",
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


def _posting_few(exact: int, minimum: int) -> str:
    noun = "timestamp" if exact == 1 else "timestamps"
    return (
        f"{exact} exact UTC {noun} stored. "
        f"Hour and weekday patterns need at least {minimum}. "
        "Year, month, and day-only dates are excluded, and the missing hours are not filled in."
    )


def _bar_heights(counts: list[int]) -> list[int]:
    top = max(counts) if counts else 0
    heights = []
    for count in counts:
        if top <= 0 or count <= 0:
            heights.append(0)
        else:
            heights.append(max(12, int(round(100 * count / top))))
    return heights


def _row_text(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def stated_precision(raw: str, stated: str | None) -> str:
    """Keep the coarser of the stored precision and what the string actually contains."""
    parsed = classify_date(raw)
    if parsed == "unknown":
        return "unknown"
    key = (stated or "").strip().lower()
    if key not in PRECISION_RANK or key == "unknown":
        return parsed
    if PRECISION_RANK[key] < PRECISION_RANK[parsed]:
        return key
    if PRECISION_RANK[parsed] < PRECISION_RANK[key]:
        return parsed
    return key


def build_posting_patterns(rows: list[dict] | None, *, minimum: int = POSTING_MIN_EXACT) -> dict:
    """UTC hour and weekday counts from Exact time rows only.

    Instagram, TikTok, YouTube, and Apple releaseDate qualify. A year, month, or
    day-only value is excluded. A date with no clock is not turned into 00:00.
    Below `minimum` exact timestamps the buckets stay empty so a sample of one
    is not drawn as a pattern.
    """
    floor = minimum if isinstance(minimum, int) and not isinstance(minimum, bool) and minimum > 0 else POSTING_MIN_EXACT
    hours = empty_hours()
    days = empty_weekdays()
    per_source = {source: 0 for source in POSTING_SOURCES}
    exact = 0
    excluded = 0
    allowed = set(POSTING_SOURCES)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip().lower()
        if source not in allowed:
            continue
        precision = str(row.get("precision") or "").strip().lower()
        published = _row_text(row, ("published_at", "when", "released_at"))
        if precision != "time" or classify_date(published) != "time":
            excluded += 1
            continue
        moment = parse_utc(published)
        if moment is None:
            excluded += 1
            continue
        moment = moment.astimezone(timezone.utc)
        hours[moment.hour] += 1
        days[moment.weekday()] += 1
        per_source[source] += 1
        exact += 1
    available = exact >= floor
    public_hours = hours if available else empty_hours()
    public_days = days if available else empty_weekdays()
    hour_heights = _bar_heights(public_hours)
    day_heights = _bar_heights(public_days)
    hour_bars = [
        {"label": f"{hour:02d}:00 UTC", "count": public_hours[hour], "height": hour_heights[hour]}
        for hour in range(24)
    ]
    weekday_bars = [
        {
            "label": WEEKDAYS[index],
            "short": WEEKDAY_SHORT[index],
            "count": public_days[index],
            "height": day_heights[index],
        }
        for index in range(7)
    ]
    nonzero_hours = [bar for bar in hour_bars if bar["count"] > 0]
    sources = [
        {"source": source, "label": POSTING_LABELS[source], "count": per_source[source]}
        for source in POSTING_SOURCES
        if available and per_source[source] > 0
    ]
    hour_peak = peak(public_hours, [bar["label"] for bar in hour_bars]) if available else None
    day_peak = peak(public_days, WEEKDAYS) if available else None
    summary_parts = []
    if hour_peak:
        summary_parts.append(
            f"Most common hour is {hour_peak['label']} "
            f"({hour_peak['count']} of {hour_peak['total']} exact timestamps)."
        )
    if day_peak:
        summary_parts.append(
            f"Most common weekday is {day_peak['label']} "
            f"({day_peak['count']} of {day_peak['total']} exact timestamps)."
        )
    if available:
        reason = None
    elif exact == 0:
        reason = POSTING_NONE
    else:
        reason = _posting_few(exact, floor)
    return {
        "available": available,
        "exact_count": exact,
        "excluded_count": excluded,
        "minimum": floor,
        "timezone": "UTC",
        "reason": reason,
        "summary": " ".join(summary_parts),
        "hour_labels": hour_labels(),
        "weekday_labels": WEEKDAYS,
        "hours": public_hours,
        "weekdays": public_days,
        "hour_bars": hour_bars,
        "weekday_bars": weekday_bars,
        "nonzero_hours": nonzero_hours,
        "sources": sources,
    }


def empty_posting_patterns(reason: str | None = None) -> dict:
    result = build_posting_patterns([])
    if reason:
        result["reason"] = reason
    return result


def _valid_day(year: int, month: int, day: int) -> bool:
    if year < 1 or year > 9999 or not 1 <= month <= 12:
        return False
    try:
        datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return False
    return True


def parse_calendar_period(year: str | None = None, start: str | None = None) -> int | None:
    """A stored year from ?year=YYYY or ?from=YYYY / ?from=YYYY-MM.

    `year` wins when it is usable. A day on the end of the string is not read,
    so the parameter cannot invent a more precise date than the query named.
    """
    for raw in (year, start):
        text = (raw or "").strip()
        if not text:
            continue
        match = _PERIOD_RE.fullmatch(text)
        if match is None:
            continue
        number = int(match.group(1))
        month = match.group(2)
        if month is not None and not 1 <= int(month) <= 12:
            continue
        if 1000 <= number <= 9999:
            return number
    return None


def release_years(rows: list[dict] | None) -> list[int]:
    """Years a row lands on after precision is applied. A clock uses UTC. Nothing is made finer."""
    found = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = _row_text(row, ("when", "released_at", "published_at"))
        place = _calendar_place(raw, stated_precision(raw, row.get("precision")))
        if place is not None and 1000 <= place[1] <= 9999:
            found.append(place[1])
    return found


def calendar_navigation(years: list[int], selected: int | None, artist_id: int) -> dict:
    """Prev, next, and a decade list. Only years that have a stored release are included."""
    clean: list[int] = []
    seen: set[int] = set()
    for value in years:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value in seen or not 1000 <= value <= 9999:
            continue
        seen.add(value)
        clean.append(value)
    clean.sort(reverse=True)
    chosen = selected if isinstance(selected, int) and not isinstance(selected, bool) and 1000 <= selected <= 9999 else None
    prev_year = next((item for item in clean if chosen is not None and item < chosen), None)
    next_year = next((item for item in reversed(clean) if chosen is not None and item > chosen), None)
    try:
        artist_key = int(artist_id)
    except (TypeError, ValueError):
        artist_key = 0

    def href(value: int) -> str:
        return f"/artist/{artist_key}/calendar?year={value}"

    grouped: dict[int, list[dict]] = {}
    for value in clean:
        start = (value // 10) * 10
        grouped.setdefault(start, []).append(
            {"year": value, "href": href(value), "current": value == chosen}
        )
    decades = [{"label": f"{start}s", "years": grouped[start]} for start in sorted(grouped, reverse=True)]
    return {
        "years": clean,
        "decades": decades,
        "selected": chosen,
        "prev_year": prev_year,
        "next_year": next_year,
        "prev_href": href(prev_year) if prev_year is not None else None,
        "next_href": href(next_year) if next_year is not None else None,
    }


def _calendar_place(raw: str, precision: str):
    """A day placement, a month mark, or a year. Never a guessed day."""
    if precision == "time":
        moment = parse_utc(raw)
        if moment is None:
            return None
        moment = moment.astimezone(timezone.utc)
        return ("day", moment.year, moment.month, moment.day, moment)
    parts = calendar_parts(raw)
    if precision == "day" and len(parts) >= 3 and _valid_day(parts[0], parts[1], parts[2]):
        return ("day", parts[0], parts[1], parts[2], None)
    if precision == "month" and len(parts) >= 2 and 1 <= parts[1] <= 12 and 1 <= parts[0] <= 9999:
        return ("month", parts[0], parts[1])
    if precision == "year" and parts and 1 <= parts[0] <= 9999:
        return ("year", parts[0])
    return None


def _calendar_item(row: dict, precision: str, moment: datetime | None) -> dict:
    source = str(row.get("source") or "").strip().lower()
    item = {
        "title": str(row.get("title") or "").strip()[:200],
        "source": source,
        "source_label": CALENDAR_LABELS.get(source, source),
        "kind": _row_text(row, ("kind", "release_type"))[:80],
        "precision": precision,
        "precision_label": PRECISION_LABELS.get(precision, precision or "Unknown"),
        "clock": moment.strftime("%H:%M UTC") if precision == "time" and moment is not None else "",
    }
    mbid = str(row.get("mbid") or "").strip()
    if mbid:
        item["mbid"] = mbid[:64]
    return item


def _month_grid_html(month: dict) -> str:
    """One month grid as a string. Titles are escaped. The template does not loop the cells."""
    parts = ['<div class="calendar-grid grid grid-cols-7 gap-1 text-left text-sm">']
    for label in month.get("weekdays") or []:
        parts.append(f'<div class="text-slate-500 py-1 text-center">{html.escape(str(label))}</div>')
    for cell in month.get("cells") or []:
        if not cell.get("in_month"):
            parts.append('<div class="min-h-16 min-w-0 rounded-md p-1"></div>')
            continue
        items = cell.get("items") or []
        if items:
            klass = "min-h-16 min-w-0 rounded-md p-1 border border-emerald-500/60 bg-slate-800"
            day_class = "text-emerald-300 font-medium"
        else:
            klass = "min-h-16 min-w-0 rounded-md p-1 border border-slate-700"
            day_class = "text-slate-500"
        parts.append(f'<div class="{klass}"><div class="{day_class}">{int(cell["day"])}</div>')
        for item in items:
            bits = [str(item.get("source_label") or "")]
            if item.get("clock"):
                bits.append(str(item["clock"]))
            if item.get("precision_label"):
                bits.append(str(item["precision_label"]))
            title = html.escape(" · ".join(bit for bit in bits if bit), quote=True)
            name = html.escape(str(item.get("title") or ""))
            mbid = str(item.get("mbid") or "")
            if mbid:
                href = "https://musicbrainz.org/release-group/" + html.escape(mbid, quote=True)
                inner = f'<a href="{href}" class="hover:text-emerald-300">{name}</a>'
            else:
                inner = name
            parts.append(f'<div class="break-words text-slate-300" title="{title}">{inner}</div>')
            if item.get("clock"):
                parts.append(f'<div class="text-slate-400">{html.escape(str(item["clock"]))}</div>')
        more = cell.get("more") or 0
        if more:
            parts.append(f'<div class="text-slate-500">+{int(more)}</div>')
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def build_release_calendar(
    rows: list[dict] | None,
    *,
    month_cap: int = CALENDAR_MONTH_CAP,
    year: int | None = None,
) -> dict:
    """Month grids for MusicBrainz, Apple, Spotify, and Deezer releases.

    Day precision and exact UTC times land on that calendar day. A month-only
    date marks the month and does not pick a day. A year-only date, including a
    Spotify year, stays off the day cells. When `year` is set, only that
    calendar year is returned. Other years stay reachable through navigation,
    and this function does not invent a finer date to fill the gap.
    """
    cap = month_cap if isinstance(month_cap, int) and not isinstance(month_cap, bool) and month_cap > 0 else CALENDAR_MONTH_CAP
    selected = year if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 9999 else None
    grouped: dict[tuple[int, int], dict] = {}
    year_only: list[dict] = []
    seen: set[tuple] = set()
    day_count = 0
    month_marks = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        source = str(row.get("source") or "").strip().lower()
        if not title or source not in CALENDAR_LABELS:
            continue
        raw = _row_text(row, ("when", "released_at", "published_at"))
        precision = stated_precision(raw, row.get("precision"))
        place = _calendar_place(raw, precision)
        if place is None:
            continue
        if selected is not None and place[1] != selected:
            continue
        if place[0] == "day":
            place_key = ("day", place[1], place[2], place[3])
        else:
            place_key = place
        dedupe = (source, title.casefold(), precision, place_key)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        moment = place[4] if place[0] == "day" else None
        item = _calendar_item(row, precision, moment)
        if place[0] == "year":
            item["year"] = f"{place[1]:04d}"
            year_only.append(item)
            continue
        bucket = grouped.setdefault((place[1], place[2]), {"days": {}, "month_only": []})
        if place[0] == "month":
            bucket["month_only"].append(item)
            month_marks += 1
            continue
        bucket["days"].setdefault(place[3], []).append(item)
        day_count += 1
    month_keys = sorted(grouped, reverse=True)
    if selected is not None:
        shown_keys = month_keys
        omitted = 0
    else:
        shown_keys = month_keys[:cap]
        omitted = max(0, len(month_keys) - cap)
    months = []
    for year, month in shown_keys:
        bucket = grouped[(year, month)]
        days = bucket["days"]
        show_grid = bool(days)
        cells = []
        if show_grid:
            first = datetime(year, month, 1, tzinfo=timezone.utc)
            if month == 12:
                nxt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                nxt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            count = (nxt - first).days
            cells.extend({"in_month": False, "day": None, "items": [], "more": 0} for _ in range(first.weekday()))
            for day in range(1, count + 1):
                items = sorted(days.get(day) or [], key=lambda item: (item["source"], item["title"].casefold()))
                shown = items[:CALENDAR_CELL_CAP]
                cells.append(
                    {
                        "in_month": True,
                        "day": day,
                        "items": shown,
                        "more": max(0, len(items) - len(shown)),
                    }
                )
            while len(cells) % 7:
                cells.append({"in_month": False, "day": None, "items": [], "more": 0})
        month_only = sorted(bucket["month_only"], key=lambda item: item["title"].casefold())
        month_row = {
            "key": f"{year:04d}-{month:02d}",
            "label": f"{MONTH_NAMES[month]} {year}",
            "show_grid": show_grid,
            "weekdays": WEEKDAY_SHORT,
            "cells": cells,
            "month_only": month_only,
        }
        # The artist page used to walk every cell in Jinja. That loop, mostly
        # empty days, was the bulk of the HTML and the render CPU.
        month_row["grid_html"] = _month_grid_html(month_row) if show_grid else ""
        months.append(month_row)
    year_only.sort(key=lambda item: item["title"].casefold())
    year_only.sort(key=lambda item: item["year"], reverse=True)
    available = bool(months or year_only)
    empty_period = selected is not None and not available
    if available:
        reason = None
    elif empty_period:
        reason = (
            f"No stored MusicBrainz, Apple, Spotify, or Deezer release falls in {selected}. "
            "A month or a year is not given a day."
        )
    else:
        reason = CALENDAR_EMPTY
    return {
        "available": available,
        "reason": reason,
        "months": months,
        "year_only": year_only,
        "omitted_months": omitted,
        "day_count": day_count,
        "month_count": month_marks,
        "empty_period": empty_period,
        "period_year": selected,
        "period_label": str(selected) if selected is not None else None,
        "navigation": None,
        "row_cap_hit": False,
    }


def empty_release_calendar(reason: str | None = None) -> dict:
    result = build_release_calendar([])
    if reason:
        result["reason"] = reason
    return result


def scrub(text: str, secrets: list[str]) -> str:
    cleaned = text or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:300]
