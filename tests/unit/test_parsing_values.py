"""Locale-aware value parsing: dates, datetimes, numbers and prices (pipeline/normalise.py)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scrapeline.errors import AmbiguousValueError
from scrapeline.pipeline.locale import Locale
from scrapeline.pipeline.normalise import (
    parse_date,
    parse_datetime,
    parse_datetime_utc,
    parse_money,
    parse_number,
    parse_time,
)
from scrapeline.pipeline.values import Money

STRICT = Locale()
US = Locale(language="en-US", timezone="America/Los_Angeles", date_order="mdy", currency="USD")
UK = Locale(language="en-GB", timezone="Europe/London", date_order="dmy", currency="GBP")
DE = Locale(
    language="de-DE",
    timezone="Europe/Berlin",
    date_order="dmy",
    currency="EUR",
    decimal_separator=",",
)
JP = Locale(language="ja-JP", timezone="Asia/Tokyo", date_order="ymd", currency="JPY")
AMBIGUOUS = "ambiguous"


@pytest.mark.parametrize(
    ("text", "strict", "mdy", "dmy"),
    [
        ("2026-11-25", date(2026, 11, 25), date(2026, 11, 25), date(2026, 11, 25)),
        ("2026/11/25", date(2026, 11, 25), date(2026, 11, 25), date(2026, 11, 25)),
        ("11/25/2026", date(2026, 11, 25), date(2026, 11, 25), None),
        ("25/11/2026", date(2026, 11, 25), None, date(2026, 11, 25)),
        ("25.11.2026", date(2026, 11, 25), None, date(2026, 11, 25)),
        ("03/04/2026", AMBIGUOUS, date(2026, 3, 4), date(2026, 4, 3)),
        ("12-01-2026", AMBIGUOUS, date(2026, 12, 1), date(2026, 1, 12)),
        ("03/03/2026", date(2026, 3, 3), date(2026, 3, 3), date(2026, 3, 3)),
        ("11/25/26", date(2026, 11, 25), date(2026, 11, 25), None),
        ("31/02/2026", None, None, None),
        ("Tue, Nov 25th 2026", date(2026, 11, 25), date(2026, 11, 25), date(2026, 11, 25)),
        ("Tuesday, November 25, 2026", date(2026, 11, 25), date(2026, 11, 25), date(2026, 11, 25)),
        ("3 April 2026", date(2026, 4, 3), date(2026, 4, 3), date(2026, 4, 3)),
        ("03-Apr-2026", date(2026, 4, 3), date(2026, 4, 3), date(2026, 4, 3)),
        ("2026年11月25日", date(2026, 11, 25), date(2026, 11, 25), date(2026, 11, 25)),
        ("Nov 25", AMBIGUOUS, AMBIGUOUS, AMBIGUOUS),
        ("June", AMBIGUOUS, AMBIGUOUS, AMBIGUOUS),
        ("2026", None, None, None),
        ("not a date", None, None, None),
        ("", None, None, None),
    ],
)
def test_date_ambiguity_matrix(text: str, strict: object, mdy: object, dmy: object) -> None:
    for locale, expected in ((STRICT, strict), (US, mdy), (UK, dmy)):
        if expected == AMBIGUOUS:
            with pytest.raises(AmbiguousValueError):
                parse_date(text, locale=locale)
        else:
            assert parse_date(text, locale=locale) == expected, (text, locale.date_order)


def test_ambiguous_date_names_both_readings() -> None:
    with pytest.raises(AmbiguousValueError, match="03 April 2026 or 04 March 2026"):
        parse_date("03/04/2026", locale=STRICT)


def test_date_order_ymd_reads_short_years_first() -> None:
    assert parse_date("26/11/25", locale=JP) == date(2026, 11, 25)
    assert parse_date("25/11/2026", locale=JP) == date(2026, 11, 25)
    with pytest.raises(AmbiguousValueError):
        parse_date("03/04/2026", locale=JP)


def test_date_ignores_a_trailing_time_and_takes_date_objects() -> None:
    assert parse_date("11/25/2026 6:00 PM", locale=US) == date(2026, 11, 25)
    assert parse_date("2026-11-25T23:30:00+01:00", locale=STRICT) == date(2026, 11, 25)
    assert parse_date(datetime(2026, 1, 2, 3, 4), locale=STRICT) == date(2026, 1, 2)
    assert parse_date(date(2026, 1, 2), locale=STRICT) == date(2026, 1, 2)
    assert parse_date(None, locale=STRICT) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-25T18:00:00Z", datetime(2026, 11, 25, 18, tzinfo=UTC)),
        ("2026-11-25T18:00:00+01:00", datetime(2026, 11, 25, 17, tzinfo=UTC)),
        ("2026-11-25 18:00:00-0500", datetime(2026, 11, 25, 23, tzinfo=UTC)),
        ("25/11/2026 18:00 UTC+1", datetime(2026, 11, 25, 17, tzinfo=UTC)),
        ("Nov 25, 2026 6:00 PM GMT", datetime(2026, 11, 25, 18, tzinfo=UTC)),
    ],
)
def test_datetime_respects_explicit_offsets_without_a_timezone(
    text: str, expected: datetime
) -> None:
    parsed = parse_datetime(text, locale=Locale(date_order="dmy"))
    assert parsed == expected and parsed is not None and parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("locale", "text", "expected_utc"),
    [
        # Europe/London: GMT (+0) in winter, BST (+1) in summer.
        (UK, "15/01/2026 18:00", datetime(2026, 1, 15, 18, tzinfo=UTC)),
        (UK, "01/07/2026 18:00", datetime(2026, 7, 1, 17, tzinfo=UTC)),
        (UK, "29/03/2026 00:59", datetime(2026, 3, 29, 0, 59, tzinfo=UTC)),
        (UK, "29/03/2026 03:00", datetime(2026, 3, 29, 2, tzinfo=UTC)),
        (UK, "25/10/2026 00:30", datetime(2026, 10, 24, 23, 30, tzinfo=UTC)),
        (UK, "25/10/2026 02:30", datetime(2026, 10, 25, 2, 30, tzinfo=UTC)),
        (UK, "25/10/2026 01:30 BST", datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
        # America/Los_Angeles: PST (-8) in winter, PDT (-7) in summer.
        (US, "01/15/2026 6:00 PM", datetime(2026, 1, 16, 2, tzinfo=UTC)),
        (US, "07/01/2026 6:00 PM", datetime(2026, 7, 2, 1, tzinfo=UTC)),
        (US, "03/08/2026 1:59 AM", datetime(2026, 3, 8, 9, 59, tzinfo=UTC)),
        (US, "03/08/2026 3:00 AM", datetime(2026, 3, 8, 10, tzinfo=UTC)),
        (US, "11/01/2026 1:30 AM PDT", datetime(2026, 11, 1, 8, 30, tzinfo=UTC)),
        (US, "11/01/2026 1:30 AM PST", datetime(2026, 11, 1, 9, 30, tzinfo=UTC)),
        (US, "Tuesday, November 25, 2026 at 6:00 PM", datetime(2026, 11, 26, 2, tzinfo=UTC)),
    ],
)
def test_naive_datetimes_use_the_source_timezone_across_dst(
    locale: Locale, text: str, expected_utc: datetime
) -> None:
    parsed = parse_datetime(text, locale=locale)
    assert parsed == expected_utc and parsed is not None and parsed.tzinfo is UTC


@pytest.mark.parametrize(
    ("locale", "text", "message"),
    [
        (UK, "29/03/2026 01:30", "does not exist"),  # clocks jump 01:00 -> 02:00
        (UK, "25/10/2026 01:30", "occurs twice"),  # clocks fall back 02:00 -> 01:00
        (US, "03/08/2026 2:30 AM", "does not exist"),
        (US, "11/01/2026 1:30 AM", "occurs twice"),
        (US, "07/01/2026 6:00 PM PST", "PST is not in use"),
        (STRICT, "2026-11-25 18:00", "locale.timezone"),
        (UK, "25/11/2026", "no time of day"),
        (UK, "2026-11-25", "no time of day"),
    ],
)
def test_datetimes_that_need_a_guess_are_ambiguous(locale: Locale, text: str, message: str) -> None:
    with pytest.raises(AmbiguousValueError, match=message):
        parse_datetime(text, locale=locale)


def test_datetime_objects_and_unreadable_text() -> None:
    aware = datetime(2026, 11, 25, 18, tzinfo=timezone(timedelta(hours=9)))
    assert parse_datetime(aware, locale=STRICT) == datetime(2026, 11, 25, 9, tzinfo=UTC)
    assert parse_datetime(datetime(2026, 7, 1, 18), locale=UK) == datetime(
        2026, 7, 1, 17, tzinfo=UTC
    )
    with pytest.raises(AmbiguousValueError):
        parse_datetime(datetime(2026, 7, 1, 18), locale=STRICT)
    assert parse_datetime("soon", locale=UK) is None
    assert parse_datetime(None, locale=UK) is None


def test_legistar_helpers_keep_working() -> None:
    assert parse_time("6:00 PM") is not None and parse_time("TBD") is None
    assert parse_datetime_utc("2026-11-17T08:00:00") == datetime(2026, 11, 17, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("locale", "text", "amount", "currency"),
    [
        (UK, "£51.77", "51.77", "GBP"),
        (STRICT, "£51.77", "51.77", "GBP"),
        (DE, "1.234,56 €", "1234.56", "EUR"),
        (DE, "1 234,56 €", "1234.56", "EUR"),
        (STRICT, "USD 1,299.00", "1299.00", "USD"),
        (STRICT, "1,299.00 USD", "1299.00", "USD"),
        (US, "$1,299.99", "1299.99", "USD"),
        (Locale(currency="CAD"), "$15.00", "15.00", "CAD"),
        (STRICT, "$5 USD", "5", "USD"),
        (STRICT, "US$ 20", "20", "USD"),
        (STRICT, "₹1,50,000.00", "150000.00", "INR"),
        (STRICT, "₩12,000", "12000", "KRW"),
        (Locale(decimal_separator=","), "1.299,90 ₽", "1299.90", "RUB"),
        (Locale(decimal_separator=","), "₺49,99", "49.99", "TRY"),
        (JP, "¥1,500", "1500", "JPY"),
        (Locale(decimal_separator=","), "R$ 10,00", "10.00", "BRL"),
        (STRICT, "CHF 1'234.50", "1234.50", "CHF"),
        (Locale(currency="SEK", decimal_separator=","), "1 299 kr", "1299", "SEK"),
        (STRICT, "PEN SET £5.00", "5.00", "GBP"),  # the marker next to the amount wins
        (UK, "51.77", "51.77", "GBP"),  # no marker: the locale's currency
        (UK, "Was £60.00", "60.00", "GBP"),
        (STRICT, "-£5.00", "-5.00", "GBP"),
    ],
)
def test_money_formats_across_locales(
    locale: Locale, text: str, amount: str, currency: str
) -> None:
    assert parse_money(text, locale=locale) == Money(amount=Decimal(amount), currency=currency)


@pytest.mark.parametrize(
    ("locale", "text", "message"),
    [
        (STRICT, "$5", "can be USD, CAD"),  # '$' alone needs locale.currency
        (UK, "$5", "can be USD"),  # ... and GBP is not a dollar
        (STRICT, "¥500", "JPY, CNY"),
        (STRICT, "12 kr", "SEK, NOK"),
        (STRICT, "51.77", "names no currency"),
        (UK, "1.234,56 €", "decimal separator"),  # comma-decimal text under a '.' locale
        (DE, "£51.77", "decimal separator"),
    ],
)
def test_money_that_needs_a_guess_is_ambiguous(locale: Locale, text: str, message: str) -> None:
    with pytest.raises(AmbiguousValueError, match=message):
        parse_money(text, locale=locale)


def test_money_without_an_amount_and_non_text_values() -> None:
    assert parse_money("Free", locale=UK) is None
    assert parse_money("", locale=UK) is None
    assert parse_money(None, locale=UK) is None
    assert parse_money(Decimal("2.50"), locale=UK) == Money(amount=Decimal("2.50"), currency="GBP")
    same = Money(amount=Decimal(1), currency="EUR")
    assert parse_money(same, locale=UK) is same


@pytest.mark.parametrize(
    ("locale", "text", "expected"),
    [
        (US, "In stock (22 available)", Decimal(22)),
        (US, "1,234.56", Decimal("1234.56")),
        (DE, "1.234,56", Decimal("1234.56")),
        (DE, "1 234,5", Decimal("1234.5")),
        (US, "4.5 out of 5", Decimal("4.5")),
        (DE, "4,5 von 5", Decimal("4.5")),
        (US, "1,234,567", Decimal(1234567)),
        (US, "12,34,567", Decimal(1234567)),  # Indian lakh grouping
        (US, chr(0x2212) + "12.50", Decimal("-12.50")),  # unicode minus sign
        (US, "Save -£5.00", Decimal("-5.00")),
        (US, "SKU-123", Decimal(123)),
        (US, "51.77 123 sold", Decimal("51.77")),
        (US, "No reviews", None),
        (US, "", None),
        (US, 7, Decimal(7)),
        (US, 2.5, Decimal("2.5")),
    ],
)
def test_number_formats(locale: Locale, text: str | int | float, expected: Decimal | None) -> None:
    assert parse_number(text, locale=locale) == expected


@pytest.mark.parametrize(
    ("locale", "text"),
    [(US, "1,5"), (US, "1.234,56"), (US, "1 234,56"), (DE, "1,234.56"), (DE, "1'234.50")],
)
def test_numbers_written_another_way_are_ambiguous(locale: Locale, text: str) -> None:
    with pytest.raises(AmbiguousValueError, match="decimal separator"):
        parse_number(text, locale=locale)
