"""Shared provider result types. Serialized to plain dicts for templates and cache."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from analytics import (
    PRECISION_LABELS,
    calendar_parts,
    classify_date,
    display_timestamp,
    parse_unix,
    parse_utc,
    weekday_name,
)


@dataclass
class Metric:
    label: str
    available: bool
    value: str | None = None
    reason: str | None = None


@dataclass
class TimedItem:
    title: str
    kind: str
    precision: str
    precision_label: str
    display: str
    weekday: str | None = None
    hour_utc: int | None = None
    timezone: str | None = None
    url: str | None = None
    detail: str | None = None
    sort_key: str = ""


@dataclass
class SourceResult:
    platform: str
    label: str
    status: str
    reason: str | None = None
    profile_name: str | None = None
    profile_url: str | None = None
    image_url: str | None = None
    metrics: list[Metric] = field(default_factory=list)
    items: list[TimedItem] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    links: dict = field(default_factory=dict)
    cacheable: bool = True
    ttl: int = 2700

    def to_dict(self) -> dict:
        return asdict(self)


def source_from_dict(data: dict) -> SourceResult:
    return SourceResult(
        platform=data["platform"],
        label=data["label"],
        status=data["status"],
        reason=data.get("reason"),
        profile_name=data.get("profile_name"),
        profile_url=data.get("profile_url"),
        image_url=data.get("image_url"),
        metrics=[Metric(**item) for item in data.get("metrics") or []],
        items=[TimedItem(**item) for item in data.get("items") or []],
        notices=list(data.get("notices") or []),
        links=dict(data.get("links") or {}),
        cacheable=bool(data.get("cacheable", True)),
        ttl=int(data.get("ttl") or 2700),
    )


def unavailable(platform: str, label: str, reason: str, *, ttl: int = 120) -> SourceResult:
    return SourceResult(
        platform=platform,
        label=label,
        status="unavailable",
        reason=reason,
        cacheable=False,
        ttl=ttl,
    )


def missing_credentials(platform: str, label: str, reason: str) -> SourceResult:
    return SourceResult(
        platform=platform,
        label=label,
        status="missing_credentials",
        reason=reason,
        cacheable=False,
    )


def errored(platform: str, label: str, reason: str) -> SourceResult:
    return SourceResult(
        platform=platform,
        label=label,
        status="error",
        reason=reason,
        cacheable=False,
    )


def _day_datetime(raw: str) -> datetime | None:
    parts = calendar_parts(raw)
    if len(parts) < 3:
        return None
    try:
        return datetime(parts[0], parts[1], parts[2])
    except ValueError:
        return None


def make_item(
    *,
    title: str,
    kind: str,
    raw: str,
    precision: str | None = None,
    url: str | None = None,
    detail: str | None = None,
) -> TimedItem | None:
    precision = precision or classify_date(raw)
    if precision not in {"time", "day", "month", "year"}:
        return None
    when = parse_utc(raw) if precision == "time" else None
    day = _day_datetime(raw) if precision in {"time", "day"} else None
    if precision == "time" and when is None:
        return None
    if precision == "day" and day is None:
        return None
    clock = when if precision == "time" else day
    return TimedItem(
        title=title or "Untitled",
        kind=kind,
        precision=precision,
        precision_label=PRECISION_LABELS[precision],
        display=display_timestamp(precision, clock, raw),
        weekday=weekday_name(clock) if precision in {"time", "day"} else None,
        hour_utc=when.hour if precision == "time" and when else None,
        timezone="UTC" if precision == "time" else None,
        url=url,
        detail=detail,
        sort_key=when.strftime("%Y-%m-%dT%H:%M:%S") if precision == "time" and when else (raw or ""),
    )


def make_unix_item(
    *,
    title: str,
    kind: str,
    stamp: object,
    url: str | None = None,
    detail: str | None = None,
) -> TimedItem | None:
    when = parse_unix(stamp)
    if when is None:
        return None
    when = when.astimezone(timezone.utc)
    return TimedItem(
        title=title or "Untitled",
        kind=kind,
        precision="time",
        precision_label=PRECISION_LABELS["time"],
        display=when.strftime("%Y-%m-%d %H:%M UTC"),
        weekday=weekday_name(when),
        hour_utc=when.hour,
        timezone="UTC",
        url=url,
        detail=detail,
        sort_key=when.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def sort_items(items: list[TimedItem]) -> list[TimedItem]:
    return sorted(items, key=lambda item: item.sort_key, reverse=True)
