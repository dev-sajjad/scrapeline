"""Jinja filters that format values the same way on every page."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from jinja2 import Undefined

EMPTY = "—"


def present[T](value: T | None) -> TypeGuard[T]:
    """False for None and for a key a template asked for that is not there (Jinja's Undefined)."""
    return value is not None and not isinstance(value, Undefined)


# Load counts in display order, with the word shown for each.
LOAD_COUNTS = (
    ("inserted", "new"),
    ("updated", "updated"),
    ("restored", "restored"),
    ("removed", "removed"),
    ("unchanged", "unchanged"),
)


def dt(value: datetime | None) -> str:
    """A timestamp in UTC to the minute: 2026-11-24 18:03 UTC."""
    if not present(value):
        return EMPTY
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def iso(value: datetime | None) -> str:
    """ISO 8601 in UTC, for machine-readable attributes such as <time datetime>."""
    if not present(value):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def ago(value: datetime | None, now: datetime | None = None) -> str:
    """Relative time: 'just now', '5 min ago', '3 h ago', '2 d ago', or 'in 5 min'."""
    if not present(value):
        return EMPTY
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = ((now or datetime.now(UTC)) - value).total_seconds()
    future, seconds = seconds < 0, abs(seconds)
    if seconds < 45:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            amount = f"{int(seconds // size)} {unit}"
            return f"in {amount}" if future else f"{amount} ago"
    return "just now"


def nbytes(value: int | None) -> str:
    """Bytes in binary units: 512 B, 1.5 KiB, 3.2 MiB."""
    if not present(value):
        return EMPTY
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return EMPTY  # unreachable; keeps mypy satisfied


def money(value: Mapping[str, Any] | None) -> str:
    """A stored money value ({"amount": "51.77", "currency": "GBP"}) as '51.77 GBP'."""
    if not value or value.get("amount") is None:
        return EMPTY
    try:
        amount = Decimal(str(value["amount"]))
    except InvalidOperation:
        return str(value["amount"])
    return f"{amount:,.2f} {value.get('currency') or ''}".strip()


def number(value: float | int | Decimal | None) -> str:
    """Thousands separators; floats keep up to two decimals."""
    if not present(value):
        return EMPTY
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def percent(value: float | None, digits: int = 1) -> str:
    """A fraction as a percentage: 0.125 -> '12.5%'."""
    if not present(value):
        return EMPTY
    return f"{value * 100:.{digits}f}%"


def duration_ms(value: float | int | None) -> str:
    """Milliseconds for people: '350 ms', '1.2 s', '3 min 4 s'."""
    if not present(value):
        return EMPTY
    if value < 1000:
        return f"{int(value)} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest} s"


def duration(start: datetime | None, end: datetime | None) -> str:
    """Elapsed time between two timestamps, as `duration_ms` shows it; empty while unfinished."""
    if not present(start) or not present(end):
        return EMPTY
    return duration_ms(max((end - start).total_seconds(), 0) * 1000)


def loaded(value: Mapping[str, Any] | None) -> str:
    """Per-dataset load counts from run stats: 'product: 8 updated, 1 removed, 11 unchanged'."""
    if not value:
        return EMPTY
    parts = []
    for dataset, counts in value.items():
        if not isinstance(counts, Mapping):
            continue
        shown = [f"{counts[key]:,} {word}" for key, word in LOAD_COUNTS if counts.get(key)]
        parts.append(f"{dataset}: {', '.join(shown) or 'nothing'}")
    return "; ".join(parts) or EMPTY


def to_json(value: object, indent: int | None = None) -> str:
    """JSON for display. Plain text, so autoescaping still applies (unlike Jinja's `tojson`)."""
    return json.dumps(value, indent=indent, sort_keys=True, ensure_ascii=False, default=str)


def http_url(value: object) -> str:
    """The value if it is an absolute http(s) URL, else ''. Keeps javascript: URLs out of hrefs."""
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ""
    return value.strip() if parts.scheme in ("http", "https") and parts.netloc else ""


def short(value: object, length: int = 12) -> str:
    """The start of a long id or hash, for display only."""
    if not present(value):
        return EMPTY
    text = str(value)
    return text if len(text) <= length else text[:length] + "…"


FILTERS: dict[str, Callable[..., str]] = {
    "dt": dt,
    "iso": iso,
    "ago": ago,
    "duration": duration,
    "loaded": loaded,
    "json": to_json,
    "http_url": http_url,
    "bytes": nbytes,
    "money": money,
    "number": number,
    "percent": percent,
    "ms": duration_ms,
    "short": short,
}
