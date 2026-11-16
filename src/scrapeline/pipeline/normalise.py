"""Pure normalisation helpers used by parsers and the declarative engine. No I/O."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time
from urllib.parse import urljoin, urlsplit

from dateutil import parser as dateparser

_WS = re.compile(r"\s+")
_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)
_WEEKDAY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|nesday|sday|urday)?\b\.?,?",
    re.IGNORECASE,
)


def clean_text(value: str | None) -> str | None:
    """NFC-normalise, collapse whitespace (incl. CR/LF, nbsp), strip. Empty -> None."""
    if value is None:
        return None
    text = _WS.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    return text or None


def fold(value: str | None) -> str:
    """Casefolded, punctuation-light form for natural keys and comparisons."""
    text = clean_text(value) or ""
    return re.sub(r"[^\w ]+", "", text.casefold()).strip()


def parse_date(value: str | date | datetime | None) -> date | None:
    """Parse messy human dates: '2026-11-25', '11/25/2026', 'Tue, Nov 25th 2026', '25 Nov 2026'.

    US month-first for slash dates (sources are mostly US). Returns None when unparseable;
    callers decide whether a missing date is a reject.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if not text:
        return None
    text = _ORDINAL.sub(r"\1", _WEEKDAY.sub("", text)).strip(" ,")
    try:
        return dateparser.parse(text, dayfirst=False, yearfirst=False, fuzzy=False).date()
    except (ValueError, OverflowError):
        return None


def parse_time(value: str | time | None) -> time | None:
    """'2:00 PM', '14:00', '9:30am', '9:30 a.m.' -> time. None when absent/unparseable."""
    if value is None or isinstance(value, time):
        return value
    text = clean_text(value)
    if not text:
        return None
    text = text.lower().replace("a.m.", "am").replace("p.m.", "pm").replace(".", ":")
    if text in {"tbd", "tba", "n/a", "none"}:
        return None
    try:
        parsed = dateparser.parse(text, default=datetime(2000, 1, 1))
    except (ValueError, OverflowError):
        return None
    return parsed.time().replace(second=0, microsecond=0)


def parse_datetime_utc(value: str | None) -> datetime | None:
    """ISO timestamp without zone that the source documents as UTC -> aware datetime."""
    if not value:
        return None
    from datetime import UTC

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def absolute_url(href: str | None, base: str) -> str | None:
    """Resolve a link against the page URL. Rejects javascript:, mailto:, data:, fragments."""
    href = clean_text(href)
    if not href or href.startswith("#"):
        return None
    url = urljoin(base, href)
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    return url
