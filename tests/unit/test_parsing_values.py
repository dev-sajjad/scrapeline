"""Locale-aware value parsing: dates, datetimes, numbers and prices (pipeline/normalise.py)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    start_of_day,
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
NY = Locale(language="en-US", timezone="America/New_York", date_order="mdy", currency="USD")
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
        ("26/11/25", AMBIGUOUS, None, date(2025, 11, 26)),  # also yy/mm/dd 2026-11-25
        ("13/11/12", AMBIGUOUS, None, date(2012, 11, 13)),
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


def test_two_digit_year_dates_name_the_year_first_reading() -> None:
    with pytest.raises(AmbiguousValueError, match="26 November 2025 or 25 November 2026"):
        parse_date("26/11/25", locale=STRICT)


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
        ("Wed, 25 Nov 2026 18:00:00 +0000", datetime(2026, 11, 25, 18, tzinfo=UTC)),  # RFC 2822
        ("Wed, 25 Nov 2026 18:00:00 GMT", datetime(2026, 11, 25, 18, tzinfo=UTC)),
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
        (DE, "24.11.2026, 18:30 Uhr", datetime(2026, 11, 24, 17, 30, tzinfo=UTC)),
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


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        (DE, "25.11.2026 18:00-20:00"),
        (UK, "25/11/2026 09:00-17:00"),
        (US, "2026-11-25 10:00-12:00"),
        (US, "11/25/2026 10:00-11:30"),
        (US, "Nov 25, 2026 7:00 PM - 9:00 PM"),
        (US, "Nov 25, 2026, 7:00 - 9:00 PM"),
        (US, "Nov 25, 2026 7-9 PM"),
        (DE, "25.11.2026, 18:00 bis 20:00 Uhr"),
    ],
)
def test_time_ranges_are_ambiguous_not_read_as_offsets(locale: Locale, text: str) -> None:
    with pytest.raises(AmbiguousValueError, match="time range"):
        parse_datetime(text, locale=locale)
    assert parse_date(text, locale=locale) == date(2026, 11, 25)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-25T10:00-12:00", datetime(2026, 11, 25, 22, tzinfo=UTC)),  # ISO 'T' offset
        ("2026-11-25 10:00:00-05:00", datetime(2026, 11, 25, 15, tzinfo=UTC)),  # with seconds
        ("2026-11-25 10:00+01:00", datetime(2026, 11, 25, 9, tzinfo=UTC)),  # '+' is no range
        ("Nov 25, 2026 7:00 PM -0500", datetime(2026, 11, 26, 0, tzinfo=UTC)),
        ("11/25/2026 - 7:00 PM", datetime(2026, 11, 26, 3, tzinfo=UTC)),  # one time, no range
        ("2026-11-25T10:00-20:00", None),  # no zone is 20 hours behind UTC
        ("Nov 25, 2026 10:00 UTC-20", None),
    ],
)
def test_real_offsets_are_kept_and_impossible_ones_refused(
    text: str, expected: datetime | None
) -> None:
    assert parse_datetime(text, locale=US) == expected


@pytest.mark.parametrize(
    ("locale", "text", "expected_utc"),
    [
        (NY, "Nov 25, 2026 7:00 PM ET", datetime(2026, 11, 26, 0, tzinfo=UTC)),  # EST
        (NY, "Jul 1, 2026 7:00 PM ET", datetime(2026, 7, 1, 23, tzinfo=UTC)),  # EDT
        (
            Locale(timezone="America/Chicago"),
            "2026-11-25 7:00 PM CT",
            datetime(2026, 11, 26, 1, tzinfo=UTC),
        ),
        (US, "Nov 25, 2026 7:00 PM PT", datetime(2026, 11, 26, 3, tzinfo=UTC)),
    ],
)
def test_generic_north_american_zone_names(
    locale: Locale, text: str, expected_utc: datetime
) -> None:
    assert parse_datetime(text, locale=locale) == expected_utc


def test_generic_zone_names_still_have_to_fit_the_source() -> None:
    with pytest.raises(AmbiguousValueError, match="PT is not in use"):
        parse_datetime("Nov 25, 2026 7:00 PM PT", locale=NY)
    with pytest.raises(AmbiguousValueError, match="occurs twice"):
        parse_datetime("Nov 1, 2026 1:30 AM ET", locale=NY)  # both EDT and EST


def test_start_of_day_is_local_midnight_as_utc() -> None:
    assert start_of_day(date(2026, 11, 25), locale=UK) == datetime(2026, 11, 25, tzinfo=UTC)
    assert start_of_day(date(2026, 7, 1), locale=UK) == datetime(2026, 6, 30, 23, tzinfo=UTC)
    # Chile starts DST at midnight, so 6 September 2026 begins at 01:00 local time.
    santiago = Locale(timezone="America/Santiago")
    start = start_of_day(date(2026, 9, 6), locale=santiago)
    zone = ZoneInfo("America/Santiago")
    assert start.astimezone(zone).date() == date(2026, 9, 6)
    assert (start - timedelta(seconds=1)).astimezone(zone).date() == date(2026, 9, 5)
    with pytest.raises(AmbiguousValueError, match=r"locale\.timezone"):
        start_of_day(date(2026, 11, 25), locale=STRICT)


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
        (UK, "Save 20% £15.99", "15.99", "GBP"),  # a percentage is not an amount
        (UK, "BLUE PEN 2.99", "2.99", "GBP"),  # 'PEN' is part of the name here
        (STRICT, "Price in USD: 12.99", "12.99", "USD"),
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
        (STRICT, "BLUE PEN 2.99", "names no currency"),
        (UK, "USD 12.00", "names USD but the source's currency is GBP"),
        (UK, "Was £60.00 Now £45.00", "several amounts"),
        (UK, "2 for £5", "several amounts"),
        (UK, "£5.00 (£1.25 each)", "several amounts"),
        (UK, "12.99 (was 15.99)", "several amounts"),
        (US, "SKU-123 $10", "several amounts"),
        (US, "3 for $10", "several amounts"),
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
