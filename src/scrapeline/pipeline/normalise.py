"""Pure normalisation helpers used by parsers and the declarative engine. No I/O.

Dates, datetimes, numbers and prices are read under the source's `Locale`. Reading is strict:
a value with more than one plausible reading raises `AmbiguousValueError` (the record becomes
a reject that names the problem) instead of being guessed. A time range ('18:00-20:00') and a
price text with several amounts ('Was £60 Now £45') are such values. A value that cannot be
read at all gives None, and the caller decides whether that is a reject. Month names are read
in English.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

from dateutil import parser as dateparser

from scrapeline.errors import AmbiguousValueError
from scrapeline.pipeline.locale import Locale
from scrapeline.pipeline.values import Money

_WS = re.compile(r"\s+")
_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)
_WEEKDAY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|nesday|sday|urday)?\b\.?,?",
    re.IGNORECASE,
)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)")

_YMD = re.compile(r"(\d{4})([-/.])(\d{1,2})\2(\d{1,2})")
_YMD_CJK = re.compile(r"(\d{4})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日일]?")
_NUMERIC = re.compile(r"(\d{1,2})([-/.])(\d{1,2})\2(\d{4}|\d{2})")
_MONTH_NAME = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.IGNORECASE
)

_TIME_12H = re.compile(
    r"(?<![\d:])(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.IGNORECASE
)
_TIME_24H = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6}))?)?(?!\d)")
_TIME_SUFFIX = re.compile(r"^\s*(?:uhr|hrs?|hours|o'clock)\b\.?", re.IGNORECASE)  # '18:00 Uhr'
_RANGE_JOINER = re.compile(r"\s*(?:[-\u2013\u2014]|\b(?:to|until|till|bis)\b)\s*", re.IGNORECASE)
# The start of a range that ends at the time found: '7-9 PM', '7:00 - 9:00 PM'. The lookbehind
# keeps the year in 'Nov 25, 2026 - 7 PM' from reading as a start hour.
_RANGE_START = re.compile(
    r"(?<![\d.,/:-])(?:\d{1,2}:\d{2}|1[0-2]|0?[1-9])(?:\s*[ap]\.?\s?m\b\.?)?"
    r"\s*(?:[-\u2013\u2014]|\b(?:to|until|till|bis)\b)\s*$",
    re.IGNORECASE,
)
_ZONE = re.compile(
    r"\s*(?:"
    r"(?P<offset>(?:UTC|GMT)\s*[+\-\u2212]\d{1,2}(?::?\d{2})?|[+\-\u2212]\d{2}:?\d{2})"
    r"|(?P<utc>Z|UTC|GMT|UT)"
    r"|\(?(?!JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<abbr>[A-Z]{2,5})\)?"
    r")(?![A-Za-z0-9])(?!\s*(?:[ap]\.?m\b|:))",
)
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:T|(?P<space> ))\d{2}:\d{2}(?P<seconds>:\d{2})?")
_MAX_OFFSET = timedelta(hours=14)
# Generic North American zone names stand for the standard and the daylight abbreviation.
_GENERIC_ZONES = {
    "ET": ("EST", "EDT"),
    "CT": ("CST", "CDT"),
    "MT": ("MST", "MDT"),
    "PT": ("PST", "PDT"),
}
_LEADING_JOINERS = re.compile(r"^[\s,;@\u2013\u2014-]+")
_TRAILING_JOINERS = re.compile(r"(?:[\s,;@\u2013\u2014-]|\bat\b|(?<=\d)T)+$")

# Thousands separators that may appear with each decimal separator. Spaces group digits only
# in comma-decimal locales (fr, de-AT, nordic, pl, ru ...); "." locales also accept the Swiss
# apostrophe and Indian lakh grouping (1,50,000).
_GROUPS = {".": ",'\u2019", ",": ". "}
_FOREIGN_NUMBER = re.compile(r"[.,'\u2019]\d")
_SPACE_GROUP = re.compile(r" \d{3}(?!\d)")
_SIGN_BEFORE_SYMBOL = re.compile(r"(?:^|[\s(])[-\u2212]\s*[^\d\s]{1,3}\s*$")  # '-$5.00'
_PERCENT = re.compile(r"\s*%")
# An uppercase code inside a run of uppercase words is a product name, not a currency: 'BLUE PEN'.
_CAPS_WORD_BEFORE = re.compile(r"[A-Z]{2,}\s+$")
_CAPS_WORD_AFTER = re.compile(r"\s+[A-Z]{2,}\b")

_DOLLAR_CURRENCIES = (
    "USD", "CAD", "AUD", "NZD", "HKD", "SGD", "TWD", "MXN", "ARS", "CLP", "COP", "UYU", "JMD",
    "TTD", "BSD", "BBD", "BZD", "FJD", "LRD", "NAD", "SBD", "SRD", "XCD", "BND", "KYD", "GYD",
)  # fmt: skip
# Symbol -> the currencies it can stand for. A symbol with several candidates needs the
# source's locale.currency to be one of them.
_SYMBOLS: dict[str, tuple[str, ...]] = {
    "US$": ("USD",),
    "CA$": ("CAD",),
    "C$": ("CAD",),
    "AU$": ("AUD",),
    "A$": ("AUD",),
    "NZ$": ("NZD",),
    "HK$": ("HKD",),
    "S$": ("SGD",),
    "NT$": ("TWD",),
    "MX$": ("MXN",),
    "R$": ("BRL",),
    "$": _DOLLAR_CURRENCIES,
    "£": ("GBP",),
    "€": ("EUR",),
    "¥": ("JPY", "CNY"),
    "円": ("JPY",),
    "元": ("CNY",),
    "₹": ("INR",),
    "₩": ("KRW",),
    "₽": ("RUB",),
    "₺": ("TRY",),
    "₪": ("ILS",),
    "₫": ("VND",),
    "₱": ("PHP",),
    "฿": ("THB",),
    "₦": ("NGN",),
    "₴": ("UAH",),
    "₸": ("KZT",),
    "₡": ("CRC",),
    "₲": ("PYG",),
    "₵": ("GHS",),
    "₼": ("AZN",),
    "₾": ("GEL",),
    "₭": ("LAK",),
    "₮": ("MNT",),
    "zł": ("PLN",),
    "Kč": ("CZK",),
    "Ft": ("HUF",),
    "kr": ("SEK", "NOK", "DKK", "ISK"),
    "lei": ("RON",),
    "лв": ("BGN",),
    "Rp": ("IDR",),
    "RM": ("MYR",),
    "Rs": ("INR", "PKR", "LKR", "NPR"),
}
_ISO_CURRENCIES = frozenset(
    """
    AED ARS AUD AZN BAM BBD BDT BGN BHD BND BRL BSD BWP BYN BZD CAD CHF CLP CNY COP CRC CZK DKK
    DOP DZD EGP ETB EUR FJD GBP GEL GHS GTQ HKD HNL HUF IDR ILS INR IQD ISK JMD JOD JPY KES KGS
    KRW KWD KYD KZT LAK LBP LKR MAD MDL MKD MMK MNT MOP MUR MXN MYR NAD NGN NOK NPR NZD OMR PAB
    PEN PHP PKR PLN PYG QAR RON RSD RUB SAR SEK SGD THB TND TRY TTD TWD TZS UAH UGX USD UYU UZS
    VES VND XAF XCD XOF ZAR ZMW
    """.split()
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


def parse_date(value: str | date | datetime | None, *, locale: Locale) -> date | None:
    """Read a date: '2026-11-25', '25/11/2026', 'Tue, Nov 25th 2026', '2026年11月25日'.

    ISO and month-name dates are always accepted. Numeric day/month dates follow
    `locale.date_order`; without one, only readings that cannot be swapped are accepted and
    '03/04/2026' raises AmbiguousValueError, as does '26/11/25' (it can also be yy/mm/dd).
    A date without a year raises too. A trailing time or time range is allowed and ignored.
    Returns None when the text is not a date.
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
    return _read_date(_split_time(text).date_text, locale)


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


def parse_datetime(value: str | datetime | None, *, locale: Locale) -> datetime | None:
    """Read a date and time of day as an aware UTC datetime.

    An explicit offset ('Z', '+01:00', 'UTC-5') is respected; offsets beyond ±14:00 do not
    exist and give None. A naive value is a wall-clock time in `locale.timezone`; zone
    abbreviations ('PST', 'BST', and 'ET' for EST/EDT) must match that zone at that moment.
    Raises AmbiguousValueError for a naive value without a source timezone, a date with no
    time of day, a time range ('18:00-20:00', '7-9 PM') and wall-clock times that a DST change
    skips or repeats. '2026-11-25 10:00-12:00' is read as a range: an ISO offset after a
    minute needs the 'T' separator or seconds ('10:00:00-12:00').
    Returns None when the text is not a datetime.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_utc(value, locale, text=value.isoformat())
    text = clean_text(value)
    if not text:
        return None
    iso = _ISO_DATETIME.match(text)
    range_like = iso is not None and bool(iso.group("space")) and not iso.group("seconds")
    if iso is not None and not (range_like and _RANGE_JOINER.match(text, iso.end())):
        try:
            parsed = datetime.fromisoformat(text.replace("\u2212", "-"))
        except ValueError:
            pass
        else:
            offset = parsed.utcoffset()
            if offset is not None and abs(offset) > _MAX_OFFSET:
                return None
            return _to_utc(parsed, locale, text=text)
    parts = _split_time(text)
    day = _read_date(parts.date_text, locale)
    if day is None:
        return None
    if parts.time is None:
        raise AmbiguousValueError(f"{text!r} has a date but no time of day")
    if parts.is_range:
        raise AmbiguousValueError(
            f"{text!r} is a time range, not one time; narrow it to the start or the end"
        )
    wall = _time_from_match(parts.time)
    if wall is None:
        return None
    naive = datetime.combine(day, wall)
    zone = parts.zone
    if zone is None:
        return _to_utc(naive, locale, text=text)
    if zone.group("utc"):
        return naive.replace(tzinfo=UTC)
    if offset_text := zone.group("offset"):
        try:
            return naive.replace(tzinfo=_fixed_offset(offset_text)).astimezone(UTC)
        except ValueError:  # beyond ±14:00
            return None
    return _localise(naive, locale, text=text, abbreviation=zone.group("abbr"))


def start_of_day(day: date, *, locale: Locale) -> datetime:
    """The first instant of `day` in the source timezone, as UTC (usually local midnight).

    Used when a spec reads a date-only value into a datetime field on purpose. Raises
    AmbiguousValueError without `locale.timezone`. When a DST change skips midnight, the day
    starts at the first wall-clock time that exists.
    """
    zone = locale.tzinfo
    if zone is None:
        raise AmbiguousValueError(
            f"{day.isoformat()} has no time of day; its start needs the source's locale.timezone"
        )
    # fold=0 is the earlier reading, and for a skipped midnight it lands after the gap.
    return datetime.combine(day, time(), tzinfo=zone).astimezone(UTC)


def parse_datetime_utc(value: str | None) -> datetime | None:
    """ISO timestamp without zone that the source documents as UTC -> aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_number(value: str | int | float | Decimal | None, *, locale: Locale) -> Decimal | None:
    """First number in the text, read with the locale's separators.

    'In stock (22 available)' -> 22; '1.234,56' -> 1234.56 with decimal_separator ','.
    A number written with the other convention ('1,5' under '.') raises AmbiguousValueError
    rather than being read as two numbers. Returns None when the text holds no number.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("a boolean is not a number")
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = clean_text(value)
    if not text:
        return None
    found = next(_numbers(text, locale), None)
    return found[0] if found else None


def parse_money(
    value: str | int | float | Decimal | Money | None, *, locale: Locale
) -> Money | None:
    """Read a price: '£51.77', '1.234,56 €', 'USD 1,299.00', 'R$ 10,00'.

    The text must hold exactly one amount; percentages ('Save 20%') are not amounts. Several
    amounts ('Was £60.00 Now £45.00', '2 for £5', 'SKU-123 $10') raise AmbiguousValueError:
    narrow the selector or the pattern to the price. The currency comes from an ISO code or a
    symbol in the text, else from `locale.currency`. A symbol shared by several currencies
    ('$', '¥', 'kr') needs `locale.currency` to be one of them, and a bare ISO code that
    differs from `locale.currency` raises (it may be part of a name). Raises
    AmbiguousValueError when the currency cannot be determined. Returns None when the text
    holds no amount.
    """
    if value is None or isinstance(value, Money):
        return value
    if not isinstance(value, str):
        amount = parse_number(value, locale=locale)
        return None if amount is None else Money(amount=amount, currency=_currency("", locale))
    text = clean_text(value)
    if text is None:
        return None
    amounts = [n for n in _numbers(text, locale) if not _PERCENT.match(text, n[2])]
    if not amounts:
        return None
    if len(amounts) > 1:
        listed = ", ".join(repr(text[start:end]) for _, start, end in amounts[:4])
        raise AmbiguousValueError(
            f"{text!r} holds several amounts ({listed}); narrow it to the price"
        )
    amount, start, end = amounts[0]
    return Money(amount=amount, currency=_currency(text, locale, near=(start, end)))


def absolute_url(href: str | None, base: str) -> str | None:
    """Resolve a link against the page URL. Rejects javascript:, mailto:, data:, fragments."""
    href = clean_text(href)
    if not href or href.startswith("#"):
        return None
    url = urljoin(base, href)
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    return url


class _TimeParts(NamedTuple):
    date_text: str
    time: re.Match[str] | None
    zone: re.Match[str] | None
    is_range: bool  # the time is one end of a range such as '18:00-20:00'


def _split_time(text: str) -> _TimeParts:
    """Separate a time of day or time range (and a zone marker after it) from the date text."""
    found = _TIME_12H.search(text) or _TIME_24H.search(text)
    if found is None:
        return _TimeParts(text, None, None, False)
    head = text[: found.start()]
    tail = _TIME_SUFFIX.sub("", text[found.end() :], count=1)
    is_range = False
    # Checked before the zone: '-20:00' after a time is the end of a range, not an offset.
    if (joiner := _RANGE_JOINER.match(tail)) and (
        end := _TIME_12H.match(tail, joiner.end()) or _TIME_24H.match(tail, joiner.end())
    ):
        is_range, tail = True, _TIME_SUFFIX.sub("", tail[end.end() :], count=1)
    elif start := _RANGE_START.search(head):
        is_range, head = True, head[: start.start()]
    zone = _ZONE.match(tail)
    if zone is not None:
        tail = tail[zone.end() :]
    date_text = _TRAILING_JOINERS.sub("", _LEADING_JOINERS.sub("", f"{head} {tail}".strip()))
    return _TimeParts(date_text, found, zone, is_range)


def _read_date(text: str, locale: Locale) -> date | None:
    text = _ORDINAL.sub(r"\1", _WEEKDAY.sub("", text))
    text = _EMPTY_BRACKETS.sub("", text).strip(" ,")
    if not text:
        return None
    if m := _YMD.fullmatch(text):
        return _valid_date(int(m.group(1)), int(m.group(3)), int(m.group(4)))
    if m := _YMD_CJK.fullmatch(text):
        return _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := _NUMERIC.fullmatch(text):
        return _numeric_date(text, int(m.group(1)), int(m.group(3)), m.group(4), locale)
    if _MONTH_NAME.search(text):
        return _month_name_date(text)
    return None


def _numeric_date(
    text: str, first: int, second: int, year_text: str, locale: Locale
) -> date | None:
    year = _full_year(year_text)
    readings = {
        "dmy": _valid_date(year, second, first),
        "mdy": _valid_date(year, first, second),
    }
    if len(year_text) == 2:  # '26/11/25' can also be yy/mm/dd
        readings["ymd"] = _valid_date(_full_year(f"{first:02d}"), second, int(year_text))
    # A 'ymd' source writing a four-digit year last falls back to the strict comparison.
    if (order := locale.date_order) is not None and order in readings:
        return readings[order]
    distinct = list(dict.fromkeys(found for found in readings.values() if found is not None))
    if len(distinct) > 1:
        listed = " or ".join(f"{found:%d %B %Y}" for found in distinct)
        raise AmbiguousValueError(f"{text!r} could be {listed}; set the source's locale.date_order")
    return distinct[0] if distinct else None


def _month_name_date(text: str) -> date | None:
    # Two different defaults reveal any part the text leaves out (dateutil fills it silently).
    try:
        first = dateparser.parse(text, default=datetime(2000, 1, 1), dayfirst=True)
        second = dateparser.parse(text, default=datetime(2004, 2, 2), dayfirst=True)
    except (ValueError, OverflowError):
        return None
    if first.date() != second.date():
        raise AmbiguousValueError(f"{text!r} is not a complete date (day, month and year)")
    return first.date()


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _full_year(text: str) -> int:
    """Two-digit years follow the POSIX %y rule: 69-99 -> 19xx, 00-68 -> 20xx."""
    year = int(text)
    if len(text) == 2:
        return year + (1900 if year >= 69 else 2000)
    return year


def _time_from_match(found: re.Match[str]) -> time | None:
    hour, minute, second = (int(g) if g else 0 for g in found.group(1, 2, 3))
    micro = 0
    if found.re is _TIME_12H:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if found.group(4).lower() == "p" else 0)
    elif found.group(4):
        micro = int(found.group(4).ljust(6, "0"))
    try:
        return time(hour, minute, second, micro)
    except ValueError:
        return None


def _fixed_offset(text: str) -> timezone:
    m = re.search(r"([+\-\u2212])(\d{1,2})(?::?(\d{2}))?$", text)
    if m is None:
        raise ValueError(f"not a UTC offset: {text!r}")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    if delta > _MAX_OFFSET:
        raise ValueError(f"no zone has the offset {text!r}")
    return timezone(-delta if m.group(1) != "+" else delta)


def _to_utc(value: datetime, locale: Locale, *, text: str) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return _localise(value, locale, text=text)


def _localise(
    naive: datetime, locale: Locale, *, text: str, abbreviation: str | None = None
) -> datetime:
    zone = locale.tzinfo
    if zone is None:
        raise AmbiguousValueError(f"{text!r} has no UTC offset; set the source's locale.timezone")
    candidates = _wall_time_readings(naive, zone)
    if abbreviation is not None:
        names = _GENERIC_ZONES.get(abbreviation, (abbreviation,))
        candidates = [c for c in candidates if c.tzname() in names]
        if not candidates:
            raise AmbiguousValueError(
                f"{text!r}: {abbreviation} is not in use in {locale.timezone} at that time"
            )
    if not candidates:
        raise AmbiguousValueError(f"{text!r} does not exist in {locale.timezone} (DST change)")
    if len(candidates) > 1:
        raise AmbiguousValueError(f"{text!r} occurs twice in {locale.timezone} (DST change)")
    return candidates[0].astimezone(UTC)


def _wall_time_readings(naive: datetime, zone: tzinfo) -> list[datetime]:
    """Aware readings of a wall-clock time: one normally, two in a repeated hour, none in a gap."""
    readings: dict[timedelta | None, datetime] = {}
    for fold_flag in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold_flag)
        # A wall time skipped by a DST change does not survive the round trip through UTC.
        if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
            readings.setdefault(aware.utcoffset(), aware)
    return list(readings.values())


def _numbers(text: str, locale: Locale) -> Iterator[tuple[Decimal, int, int]]:
    """Numbers in `text` with their spans, in order; raises when one is written another way."""
    sep = locale.decimal_separator
    for match in _number_pattern(sep).finditer(text):
        sign, whole, fraction = match.group("sign"), match.group("int"), match.group("frac")
        rest = text[match.end() :]
        # '1,5' or '1.234,56' under '.', and '1 234,56' (a leading group of up to three digits).
        foreign = _FOREIGN_NUMBER.match(rest) is not None
        if sep == "." and fraction is None and whole.isdigit() and len(whole) <= 3:
            foreign = foreign or _SPACE_GROUP.match(rest) is not None
        if foreign:
            raise AmbiguousValueError(
                f"{text!r} does not read as one number with decimal separator {sep!r}"
            )
        digits = re.sub(r"\D", "", whole) + (f".{fraction}" if fraction else "")
        try:
            number = Decimal(digits)
        except InvalidOperation:
            continue
        negative = bool(sign) or _SIGN_BEFORE_SYMBOL.search(text[: match.start()]) is not None
        yield (-number if negative else number), match.start(), match.end()


@cache
def _number_pattern(decimal_separator: str) -> re.Pattern[str]:
    groups = re.escape(_GROUPS[decimal_separator])
    lakh = r"\d{1,2}(?:,\d{2})+,\d{3}|" if decimal_separator == "." else ""
    # A minus counts only when it starts a word: 'SKU-123' is 123, '-5' is -5.
    return re.compile(
        rf"(?:(?<![\w.,])(?P<sign>[-\u2212]))?(?<![\d.,])"
        rf"(?P<int>{lakh}\d{{1,3}}(?P<sep>[{groups}])\d{{3}}(?:(?P=sep)\d{{3}})*|\d+)"
        rf"(?:{re.escape(decimal_separator)}(?P<frac>\d+))?(?!\d)"
    )


@cache
def _currency_pattern() -> re.Pattern[str]:
    symbols = sorted(_SYMBOLS, key=len, reverse=True)
    # Letter symbols ('kr', 'Rs') must stand alone; sign symbols ('$', '€') may touch digits.
    parts = [
        rf"(?<![A-Za-z]){re.escape(s)}(?![A-Za-z])" if s[-1].isalpha() else re.escape(s)
        for s in symbols
    ]
    return re.compile(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])|" + "|".join(parts))


def _iso_code(text: str, found: re.Match[str]) -> bool:
    """An ISO 4217 code standing on its own, not a word in an uppercase name ('BLUE PEN')."""
    if found.group(0) not in _ISO_CURRENCIES:
        return False
    before = _CAPS_WORD_BEFORE.search(text, 0, found.start())
    return before is None and _CAPS_WORD_AFTER.match(text, found.end()) is None


def _currency(text: str, locale: Locale, *, near: tuple[int, int] = (0, 0)) -> str:
    """Currency of the amount at `near`: the closest marker in the text, else the locale's."""
    markers: list[tuple[int, str, tuple[str, ...]]] = []
    for m in _currency_pattern().finditer(text):
        codes = _SYMBOLS.get(m.group(0)) or ((m.group(0),) if _iso_code(text, m) else ())
        if codes:
            distance = max(near[0] - m.end(), m.start() - near[1], 0)
            markers.append((distance, m.group(0), codes))
    if not markers:
        if locale.currency is None:
            raise AmbiguousValueError(
                f"{text!r} names no currency; set the source's locale.currency"
            )
        return locale.currency
    _, marker, codes = min(markers, key=lambda found: found[0])
    if marker not in _SYMBOLS and locale.currency not in (None, marker):
        raise AmbiguousValueError(
            f"{text!r} names {marker} but the source's currency is {locale.currency}; "
            "the code may be part of a name"
        )
    if len(codes) == 1:
        return codes[0]
    # A shared symbol is settled by an ISO code elsewhere in the text ('$5 USD').
    named = [c[0] for _, _, c in markers if len(c) == 1 and c[0] in codes]
    if named:
        return named[0]
    if locale.currency in codes:
        return locale.currency
    listed = ", ".join(codes[:5]) + (", ..." if len(codes) > 5 else "")
    raise AmbiguousValueError(
        f"{text!r}: {marker!r} can be {listed}; set the source's locale.currency"
    )
