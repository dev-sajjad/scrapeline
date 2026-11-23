"""shop-sim end to end: three daily catalogue snapshots give an exact price and stock history."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, Row, func, select

from scrapeline.pipeline.runner import Runner, RunSummary
from scrapeline.registry.config import load_source_file
from scrapeline.settings import Settings
from scrapeline.store import tables as t
from scrapeline.store.raw import FilesystemRawStore
from tests.conftest import ROOT, public_resolver

pytestmark = pytest.mark.integration

SITES = ROOT / "tests" / "sites"
CONFIG = ROOT / "config" / "sources" / "shop-sim.yaml"
BASE = "http://shop-sim.test"

DAY_1_SLUGS = {
    "stoneware-mug",
    "espresso-cups",
    "cast-iron-skillet",
    "chef-knife",
    "linen-tea-towels",
    "bamboo-board",
    "desk-lamp",
    "ergonomic-chair",
    "monitor-arm",
    "notebook-a5",
    "fountain-pen",
    "pencil-set",
    "washi-tape",
    "wool-throw",
    "scented-candle",
    "plant-pot",
    "watering-can",
    "secateurs",
    "seed-tray",
    "garden-kneeler",
}
DAY_2_PRICES = {
    "espresso-cups": ("14.99", "16.49"),
    "monitor-arm": ("64.50", "69.00"),
    "linen-tea-towels": ("12.00", "9.60"),
    "washi-tape": ("7.49", "5.99"),
    "ergonomic-chair": ("1249.00", "1099.00"),
}
DAY_2_STOCK = {
    "chef-knife": (True, False),
    "fountain-pen": (True, False),
    "scented-candle": (False, True),
}
LIFECYCLE = ["run.started", "records.loaded", "checks.done", "run.finished"]


class ShopSimSite:
    """Serves tests/sites/ the way the nginx fixture server does, through MockTransport."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        file = (SITES / path.lstrip("/")).resolve()
        if path.endswith("/"):
            file = file / "index.html"
        if path.startswith("/shop-sim/") and file.is_relative_to(SITES) and file.is_file():
            return httpx.Response(
                200, content=file.read_bytes(), headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(404)


@pytest.fixture
def run_day(clean_db: Engine, tmp_path: Path) -> Callable[[int], RunSummary]:
    site = ShopSimSite()

    def run(day: int) -> RunSummary:
        cfg = load_source_file(CONFIG, {"FIXTURE_BASE_URL": BASE, "SHOP_SIM_DAY": str(day)})
        runner = Runner(
            Settings(_env_file=None, raw_store_dir=tmp_path / "raw"),
            clean_db,
            FilesystemRawStore(tmp_path / "raw"),
            transport=httpx.MockTransport(site.handler),
            resolver=public_resolver,
            sleep=lambda s: None,
            sources={cfg.id: cfg},
        )
        summary = runner.run(cfg.id)
        assert summary.status == "succeeded", summary.error
        assert site.paths[-2:] == [f"/shop-sim/day-{day}/", f"/shop-sim/day-{day}/page-2.html"]
        return summary

    return run


def loaded(**changes: int) -> dict[str, int]:
    return {"inserted": 0, "updated": 0, "unchanged": 0, "restored": 0, "removed": 0} | changes


def slug(url: str) -> str:
    return url.rsplit("/", 1)[-1].removesuffix(".html")


def history(engine: Engine) -> dict[str, list[Row[Any]]]:
    """Every product's versions in order, keyed by its URL slug."""
    r, v = t.records, t.record_versions
    query = (
        select(
            r.c.external_id,
            r.c.canonical_url,
            v.c.version,
            v.c.change_type,
            v.c.payload,
            v.c.content_hash,
            v.c.valid_from,
            v.c.run_id,
            v.c.raw_sha256,
        )
        .join(r, r.c.id == v.c.record_id)
        .where(r.c.source_id == "shop-sim", r.c.dataset == "product")
        .order_by(r.c.external_id, v.c.version)
    )
    found: dict[str, list[Row[Any]]] = defaultdict(list)
    with engine.connect() as conn:
        for row in conn.execute(query):
            found[slug(row.canonical_url)].append(row)
    return dict(found)


def amounts(rows: list[Row[Any]]) -> list[str | None]:
    return [row.payload["price"]["amount"] if row.payload else None for row in rows]


def test_three_days_give_an_exact_history(
    run_day: Callable[[int], RunSummary], clean_db: Engine
) -> None:
    days = [run_day(1), run_day(2), run_day(3)]
    assert [d.loaded for d in days] == [
        {"product": loaded(inserted=20)},
        {"product": loaded(updated=8, unchanged=11, removed=1)},
        {"product": loaded(inserted=1, updated=1, unchanged=18, restored=1)},
    ]
    assert all(d.complete and d.rejects == 0 and d.pages == 2 for d in days)
    run_of = {d.run_id: n for n, d in enumerate(days, start=1)}

    found = history(clean_db)
    expected = {name: ["insert"] for name in DAY_1_SLUGS | {"bird-feeder"}}
    expected |= {name: ["insert", "update"] for name in DAY_2_PRICES.keys() | DAY_2_STOCK.keys()}
    expected["espresso-cups"] = ["insert", "update", "update"]
    expected["desk-lamp"] = ["insert", "removed", "restored"]
    assert {name: [row.change_type for row in rows] for name, rows in found.items()} == expected
    assert sum(len(rows) for rows in found.values()) == 32

    # Day 2: each update carries the old and the new value, written by the day-2 run.
    for name, (old, new) in DAY_2_PRICES.items():
        assert amounts(found[name])[:2] == [old, new]
        assert run_of[found[name][1].run_id] == 2
    for name, (old_stock, new_stock) in DAY_2_STOCK.items():
        insert, update = found[name]
        assert (insert.payload["in_stock"], update.payload["in_stock"]) == (old_stock, new_stock)
        assert insert.payload["price"] == update.payload["price"]
        assert run_of[update.run_id] == 2
    assert found["ergonomic-chair"][1].payload["price"] == {"amount": "1099.00", "currency": "GBP"}

    # Day 2 removes the desk lamp; day 3 restores it exactly as it was.
    inserted, removed, restored = found["desk-lamp"]
    assert inserted.external_id == "SIM-0007"  # the shop's SKU, not the host-bound URL
    assert removed.payload is None and removed.content_hash is None and removed.raw_sha256 is None
    assert (run_of[removed.run_id], run_of[restored.run_id]) == (2, 3)
    assert restored.payload == inserted.payload and restored.content_hash == inserted.content_hash
    assert restored.raw_sha256 is not None

    # Day 3: one more price change and one new product.
    assert amounts(found["espresso-cups"]) == ["14.99", "16.49", "13.99"]
    assert run_of[found["espresso-cups"][2].run_id] == 3
    [feeder] = found["bird-feeder"]
    assert feeder.payload["price"] == {"amount": "16.00", "currency": "GBP"}
    assert run_of[feeder.run_id] == 3

    for rows in found.values():
        starts = [row.valid_from for row in rows]
        assert starts == sorted(starts)
        assert [row.version for row in rows] == list(range(1, len(rows) + 1))

    with clean_db.connect() as conn:
        current = conn.execute(
            select(
                t.records.c.canonical_url, t.records.c.current_version, t.records.c.removed_at
            ).where(t.records.c.source_id == "shop-sim")
        ).all()
        events = conn.execute(
            select(t.event.c.run_id, t.event.c.kind, t.event.c.data).order_by(t.event.c.id)
        ).all()
        checks = conn.execute(select(t.check_result.c.run_id, t.check_result.c.status)).all()
        rejects = conn.execute(select(func.count()).select_from(t.rejects)).scalar_one()
        incidents = conn.execute(select(func.count()).select_from(t.incident)).scalar_one()
    assert len(current) == 21 and all(row.removed_at is None for row in current)
    assert {slug(row.canonical_url): row.current_version for row in current}["desk-lamp"] == 3

    for day, summary in enumerate(days, start=1):
        mine = [e for e in events if e.run_id == summary.run_id]
        assert [e.kind for e in mine] == LIFECYCLE, day
        assert mine[1].data == {
            "dataset": "product",
            "storage": "generic",
            **summary.loaded["product"],
        }
        assert mine[2].data["verdict"] == "pass"
        statuses = [c.status for c in checks if c.run_id == summary.run_id]
        assert statuses and set(statuses) == {"pass"}, day
    assert len(events) == 3 * len(LIFECYCLE)
    assert rejects == 0 and incidents == 0


def test_seeing_the_same_day_again_adds_no_versions(
    run_day: Callable[[int], RunSummary], clean_db: Engine
) -> None:
    for day in (1, 2, 3):
        run_day(day)
    again = run_day(3)
    assert again.loaded["product"] == loaded(unchanged=21) and again.complete
    assert sum(len(rows) for rows in history(clean_db).values()) == 32
