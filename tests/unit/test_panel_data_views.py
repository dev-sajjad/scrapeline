"""Data, storage and marts page helpers without a database: formatting, diffs, raw safety."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from scrapeline.catalog.datasets import FieldDef
from scrapeline.serve.panel.app import create_app
from scrapeline.serve.panel.pages.data import (
    DatasetInfo,
    diff_payloads,
    format_value,
    like_pattern,
    list_columns,
    parse_record_id,
    short_url,
    url_cell,
    version_views,
)
from scrapeline.serve.panel.pages.marts import MINUS, price_chart, product_href, signed
from scrapeline.serve.panel.pages.storage import decode_preview, is_text, resolve_raw
from scrapeline.settings import Settings

FIELDS = {
    "url": FieldDef(type="url", key=True),
    "title": FieldDef(type="text"),
    "price": FieldDef(type="money"),
    "in_stock": FieldDef(type="bool"),
}
T0 = datetime(2026, 11, 1, 9, tzinfo=UTC)


def test_values_are_formatted_by_field_type() -> None:
    assert format_value({"amount": "1099.5", "currency": "GBP"}, "money").text == "1,099.50 GBP"
    assert format_value(True, "bool").text == "yes"
    assert format_value(False, "bool").text == "no"
    assert format_value(0, "bool").text == "no"  # typed tables store flags as integers
    assert format_value(12345, "int").text == "12,345"
    assert format_value(4.5, "float").text == "4.5"
    assert format_value("12.3400", "decimal").text == "12.3400"
    assert format_value("2026-11-25T04:40:05+00:00", "datetime").text == "2026-11-25 04:40 UTC"
    assert format_value("2026-11-25T06:40:05+02:00", "datetime").text == "2026-11-25 04:40 UTC"
    assert format_value("2026-09-25", "date").text == "2026-09-25"
    assert format_value("09:30:00", "time").text == "09:30"
    assert format_value(["a", 1], "text").text == '["a", 1]'


def test_empty_values_are_marked_empty() -> None:
    for value in (None, ""):
        cell = format_value(value, "text")
        assert cell.empty and cell.text == "—"


def test_long_text_is_shortened_but_kept_whole_in_the_title() -> None:
    cell = format_value("x" * 500, "text")
    assert len(cell.text) == 160 and cell.text.endswith("…")
    assert cell.full == "x" * 500


def test_only_http_urls_become_links() -> None:
    link = url_cell("https://shop.example/products/a-very-long-product-name-that-goes-on-and-on")
    assert link.href == "https://shop.example/products/a-very-long-product-name-that-goes-on-and-on"
    assert link.text.startswith("shop.example/") and link.text.endswith("…")
    assert link.full == link.href
    for hostile in ("javascript:alert(1)", "data:text/html,<script>", "//evil.example/x"):
        assert url_cell(hostile).href is None
    # Typed models keep URLs in text fields named *_url.
    assert format_value("https://a.example/agenda.pdf", "text", name="agenda_url").href


def test_long_urls_keep_their_host_and_last_segment() -> None:
    fits = "shop.example/a/b/espresso-cups.html"
    assert short_url(fits, 48) == fits
    assert (
        short_url("shop-sim.test/shop-sim/products/espresso-cups.html", 36)
        == "shop-sim.test/…/espresso-cups.html"
    )
    tail = short_url("legistar2.granicus.com/seattle/meetings/2026/11/6915_A_Agenda.pdf", 36)
    assert tail == "legistar2.granicus.com/…/6915_A_Age…" and len(tail) == 36
    # No path to elide, or no room for one: cut the end.
    assert short_url("seattle.legistar.com/MeetingDetail.aspx?LEGID=6760&GID=393", 36) == (
        "seattle.legistar.com/MeetingDetail.…"
    )
    assert len(format_value("https://" + "x" * 100, "url", compact=True).text) == 36


def test_record_lists_show_keys_then_filled_fields_up_to_a_limit() -> None:
    fields = {f"f{i}": FieldDef() for i in range(12)} | {"k": FieldDef(type="url", key=True)}
    info = DatasetInfo("wide", 1, "generic", "core", "", fields, 1, 1)
    values = [{f"f{i}": "x" for i in range(12) if i != 3}]
    shown, more, empty = list_columns(info, list(fields), values)
    assert len(shown) == 8 and "k" in shown and "f3" not in shown
    assert shown[:7] == ["f0", "f1", "f2", "f4", "f5", "f6", "f7"]
    assert more == ["f8", "f9", "f10", "f11"]
    assert empty == ["f3"]


def test_diff_lists_only_changed_fields_in_field_order() -> None:
    old = {
        "url": "https://s.example/a",
        "title": "Mug",
        "price": {"amount": "14.99", "currency": "GBP"},
    }
    new = {**old, "price": {"amount": "16.49", "currency": "GBP"}, "extra": "new"}
    changes = diff_payloads(old, new, FIELDS)
    assert [c.name for c in changes] == ["price", "extra"]
    assert (changes[0].old.text, changes[0].new.text) == ("14.99 GBP", "16.49 GBP")
    assert changes[1].old.empty and changes[1].new.text == "new"
    assert diff_payloads(old, dict(old), FIELDS) == []


def _version(
    version: int, change: str, payload: dict[str, Any] | None, hours: int
) -> dict[str, Any]:
    return {
        "version": version,
        "change_type": change,
        "schema_version": 1,
        "payload": payload,
        "valid_from": T0 + timedelta(hours=hours),
        "run_id": f"run-{version}",
        "raw_sha256": None if payload is None else "a" * 64,
    }


def test_a_restore_is_compared_with_the_last_observed_values() -> None:
    lamp = {"url": "https://s.example/lamp", "price": {"amount": "39.99", "currency": "GBP"}}
    cheaper = {**lamp, "price": {"amount": "35.00", "currency": "GBP"}}
    views = version_views(
        [
            _version(1, "insert", lamp, 0),
            _version(2, "removed", None, 24),
            _version(3, "restored", cheaper, 48),
        ],
        FIELDS,
    )
    assert [v.change_type for v in views] == ["insert", "removed", "restored"]
    assert [v.valid_to for v in views] == [T0 + timedelta(hours=24), T0 + timedelta(hours=48), None]
    assert views[0].changes is None and views[1].changes is None
    restored = views[2]
    assert restored.compared_with == 1
    assert restored.changes is not None
    assert [(c.name, c.old.text, c.new.text) for c in restored.changes] == [
        ("price", "39.99 GBP", "35.00 GBP")
    ]


def test_a_cut_off_history_does_not_invent_a_comparison() -> None:
    page = {"url": "https://s.example/a", "title": "A"}
    views = version_views([_version(7, "update", page, 0), _version(8, "update", page, 1)], FIELDS)
    assert views[0].changes is None  # version 6 is not loaded
    assert views[1].changes == [] and views[1].compared_with == 7


def test_search_text_is_matched_literally() -> None:
    assert like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"


@pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5", "١٢", "99999999999999999999", ""])
def test_record_ids_that_cannot_name_a_row_are_not_found(value: str) -> None:
    with pytest.raises(HTTPException) as caught:
        parse_record_id(value)
    assert caught.value.status_code == 404


def test_record_ids_are_parsed() -> None:
    assert parse_record_id("42") == 42


def test_text_is_detected_by_content_type_then_extension() -> None:
    assert is_text("text/html; charset=utf-8", "s/2026/11/25/x.bin")
    assert is_text("application/json", "x.bin")
    assert is_text("application/ld+json", "x.bin")
    assert not is_text("application/pdf", "x.html")
    assert not is_text("image/png", "x.png")
    assert is_text(None, "s/2026/11/25/abc.html")
    assert not is_text(None, "s/2026/11/25/abc.pdf")


def test_previews_are_cut_and_never_fail_on_bad_bytes() -> None:
    text, cut = decode_preview(b"caf\xc3\xa9 \xff ok")
    assert text == "café � ok" and not cut
    text, cut = decode_preview(b"a" * (64 * 1024 + 10))
    assert len(text) == 64 * 1024 and cut


def test_raw_paths_stay_inside_the_store(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    sha = hashlib.sha256(b"x").hexdigest()
    inside = resolve_raw(root, f"shop/2026/11/25/{sha}.html", sha)
    assert inside == (root / f"shop/2026/11/25/{sha}.html").resolve()
    assert resolve_raw(root, f"../../etc/{sha}.html", sha) is None
    assert resolve_raw(root, f"/etc/{sha}.html", sha) is None
    assert resolve_raw(root, "shop/2026/11/25/" + "b" * 64 + ".html", sha) is None


def test_symlinks_out_of_the_store_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    (root / "shop").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "shop" / "link").symlink_to(outside)
    sha = "c" * 64
    assert resolve_raw(root, f"shop/link/{sha}.html", sha) is None


def test_changes_carry_their_sign() -> None:
    assert signed(Decimal("1.5")) == "+1.50"
    assert signed(Decimal("-2")) == MINUS + "2.00"
    assert signed(0) == "±0.00"
    assert signed(-9.21, "%", 1) == MINUS + "9.2%"
    assert signed(None) == "—"


def test_product_links_keep_url_ids_in_one_segment() -> None:
    href = product_href("books", "https://books.example/a/b?x=1#y")
    assert href == "/marts/price-history/books/https%3A%2F%2Fbooks.example%2Fa%2Fb%3Fx%3D1%23y"


def _history(*steps: tuple[int, str, str | None]) -> list[dict[str, Any]]:
    rows = []
    for index, (hours, change, price) in enumerate(steps):
        following = steps[index + 1][0] if index + 1 < len(steps) else None
        rows.append(
            {
                "valid_from": T0 + timedelta(hours=hours),
                "valid_to": T0 + timedelta(hours=following) if following is not None else None,
                "change_type": change,
                "is_removed": change == "removed",
                "price_amount": Decimal(price) if price else None,
                "price_currency": "GBP" if price else None,
                "price_changed": index > 0 and change == "update",
            }
        )
    return rows


def test_price_chart_steps_through_each_version() -> None:
    history = _history((0, "insert", "14.99"), (24, "update", "16.49"), (48, "update", "13.99"))
    chart = price_chart(history, "Espresso cups", now=T0 + timedelta(hours=72))
    assert chart is not None
    assert len(chart.paths) == 1 and chart.paths[0].startswith("M 100.0 ")
    assert chart.paths[0].count(" V ") == 2  # two price changes
    assert chart.points[0].label == "14.99 GBP from 2026-11-01 09:00 UTC"
    assert [label for _, label in chart.y_ticks] == ["16.49 GBP", "13.99 GBP"]
    left, top, right, bottom = chart.plot
    assert all(left <= p.x <= right and top <= p.y <= bottom for p in chart.points)
    # The highest price is drawn highest (smallest y).
    assert min(chart.points, key=lambda p: p.y).label.startswith("16.49")
    assert chart.title == "Price of Espresso cups over time"
    assert "From 14.99 GBP" in chart.description and "2 price changes" in chart.description
    assert chart.x_ticks[1][1] == "2026-11-04 09:00 UTC"


def test_price_chart_leaves_a_gap_while_removed() -> None:
    history = _history((0, "insert", "39.99"), (24, "removed", None), (48, "restored", "39.99"))
    chart = price_chart(history, "Desk lamp", now=T0 + timedelta(hours=72))
    assert chart is not None
    assert len(chart.paths) == 2 and len(chart.gaps) == 1
    gap_start, gap_end = chart.gaps[0]
    assert chart.points[0].x < gap_start < gap_end <= chart.points[1].x
    assert [label for _, label in chart.y_ticks] == ["39.99 GBP"]
    assert "removed 1 time" in chart.description


def test_no_chart_without_a_price() -> None:
    assert price_chart(_history((0, "insert", None)), "Mystery") is None


def test_data_and_storage_pages_need_the_database() -> None:
    # Port 1 refuses connections at once.
    engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    settings = Settings(_env_file=None, panel_allowed_hosts=["testserver"])
    client = TestClient(create_app(engine=engine, settings=settings))
    for path in (
        "/data",
        "/data/product",
        "/data/product/1",
        "/storage",
        "/storage/raw/" + "a" * 64,
    ):
        page = client.get(path)
        assert page.status_code == 503, path
        assert "Database unavailable" in page.text
