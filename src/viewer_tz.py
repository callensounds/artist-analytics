"""Apply a viewer zone to rows already loaded. No database access."""

from __future__ import annotations

from analytics import (
    WEEKDAYS,
    _month_grid_html,
    build_posting_patterns,
    parse_utc,
    stated_precision,
)
from timezones import UTC, format_clocks, format_zoned, normalize_tz, shift, zone_label


def _shown_utc(stamp: str):
    text = (stamp or "").strip()
    if " · " in text:
        text = text.split(" · ")[-1].strip()
    if text.endswith(" UTC"):
        text = text[:-4].strip()
    if len(text) == 16 and text[10] == " " and ":" in text:
        return parse_utc(text.replace(" ", "T") + ":00Z")
    if ":" not in text:
        return None
    return parse_utc(text)


def _relabel_item(item: dict, tz: str) -> dict:
    if item.get("precision") != "time":
        return item
    stamp = str(item.get("utc_timestamp") or item.get("timestamp") or "")
    if ":" not in stamp:
        return item
    moment = _shown_utc(stamp)
    if moment is None:
        return item
    clock, full, utc_full = format_clocks(moment, tz)
    if full == item.get("timestamp") and clock == item.get("clock"):
        return item
    copied = dict(item)
    copied["clock"] = clock
    copied["timestamp"] = full
    copied["utc_timestamp"] = utc_full
    return copied


def _relabel_month(month: dict, tz: str) -> dict:
    cells = month.get("cells") or []
    changed = False
    new_cells = []
    for cell in cells:
        if not isinstance(cell, dict):
            new_cells.append(cell)
            continue
        items = cell.get("items") or []
        new_items = []
        cell_changed = False
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            updated = _relabel_item(item, tz)
            if updated is not item:
                cell_changed = True
            new_items.append(updated)
        if cell_changed:
            changed = True
            copied = dict(cell)
            copied["items"] = new_items
            new_cells.append(copied)
        else:
            new_cells.append(cell)
    if not changed:
        return month
    copied = dict(month)
    copied["cells"] = new_cells
    copied["grid_html"] = _month_grid_html(copied) if copied.get("show_grid") else ""
    return copied


def relabel_calendar(calendar: dict, tz: str) -> dict:
    """Rewrite exact clocks. Day placement stays on the UTC date. Partial dates stay blank."""
    if not isinstance(calendar, dict) or tz == UTC:
        return calendar
    months = []
    changed = False
    for month in calendar.get("months") or []:
        if not isinstance(month, dict):
            months.append(month)
            continue
        updated = _relabel_month(month, tz)
        if updated is not month:
            changed = True
        months.append(updated)
    nav = calendar.get("month_nav")
    nav_copy = None
    if isinstance(nav, dict):
        nav_copy = dict(nav)
        nav_copy["timezone"] = zone_label(tz)
        changed = True
    if not changed:
        return calendar
    copied = dict(calendar)
    copied["months"] = months
    if nav_copy is not None:
        copied["month_nav"] = nav_copy
    return copied


def rebucket_posting(posting: dict, tz: str) -> dict:
    """Hour and weekday charts from the exact stamps already counted. Partial dates stay out."""
    if not isinstance(posting, dict) or tz == UTC:
        return posting
    stamps = posting.get("exact_stamps") or []
    if not stamps:
        return posting
    rows = [
        {
            "source": item.get("source"),
            "published_at": item.get("published_at"),
            "precision": "time",
        }
        for item in stamps
        if isinstance(item, dict)
    ]
    minimum = posting.get("minimum")
    rebuilt = build_posting_patterns(rows, minimum=minimum if isinstance(minimum, int) else 3, tz=tz)
    rebuilt["excluded_count"] = posting.get("excluded_count") or 0
    rebuilt["exact_stamps"] = list(stamps)
    return rebuilt


def _local_rows(rows, raw_key: str, tz: str):
    if not isinstance(rows, list):
        return rows
    changed = False
    out = []
    for item in rows:
        if not isinstance(item, dict):
            out.append(item)
            continue
        raw = str(item.get(raw_key) or "")
        if stated_precision(raw, item.get("precision")) != "time":
            out.append(item)
            continue
        moment = parse_utc(raw)
        if moment is None:
            out.append(item)
            continue
        copied = dict(item)
        copied["local_label"] = format_zoned(moment, tz)
        copied["local_weekday"] = WEEKDAYS[shift(moment, tz).weekday()]
        out.append(copied)
        changed = True
    return out if changed else rows


def present_history(history: dict, tz: str) -> dict:
    """Shallow copy with local clocks. UTC returns the same dict. Does not read D1."""
    zone = normalize_tz(tz)
    if zone == UTC or not isinstance(history, dict):
        return history
    posting = history.get("posting")
    calendar = history.get("calendar")
    sightings = history.get("sightings")
    releases = history.get("releases")
    new_posting = rebucket_posting(posting, zone) if isinstance(posting, dict) else posting
    new_calendar = relabel_calendar(calendar, zone) if isinstance(calendar, dict) else calendar
    new_sightings = _local_rows(sightings, "published_at", zone)
    new_releases = _local_rows(releases, "released_at", zone)
    if (
        new_posting is posting
        and new_calendar is calendar
        and new_sightings is sightings
        and new_releases is releases
    ):
        copied = dict(history)
        copied["viewer_timezone"] = zone
        return copied
    copied = dict(history)
    copied["posting"] = new_posting
    copied["calendar"] = new_calendar
    copied["sightings"] = new_sightings
    copied["releases"] = new_releases
    copied["viewer_timezone"] = zone
    return copied
