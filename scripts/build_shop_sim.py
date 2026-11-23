"""Generate the shop-sim fixture: one small shop whose catalogue changes over three days.

    uv run python scripts/build_shop_sim.py

Outputs (committed; golden files are human-owned):
  tests/sites/shop-sim/day-<n>/index.html, page-2.html   catalogue listing for day n
  tests/fixtures/golden/shop-sim-day-<n>.json             expected product rows for day n

Day 2: five prices change (two up, three down), three products flip stock and the desk
lamp is gone. Day 3: the desk lamp is back as it was, a bird feeder is new and the
espresso cups change price again. Product URLs never change, so a product keeps one
identity across days. Output is deterministic: same script, same bytes.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITES = ROOT / "tests" / "sites"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
FIRST_PAGE_SIZE = 10


@dataclass(frozen=True)
class Product:
    slug: str
    sku: str
    title: str
    category: str
    price: Decimal
    in_stock: bool

    @property
    def path(self) -> str:
        return f"/shop-sim/products/{self.slug}.html"


def _product(n: int, slug: str, title: str, category: str, price: str, stock: bool) -> Product:
    return Product(slug, f"SIM-{n:04d}", title, category, Decimal(price), stock)


CATALOGUE = [
    _product(1, "stoneware-mug", "Stoneware mug, 350 ml", "Kitchen", "8.50", True),
    _product(2, "espresso-cups", "Espresso cup set (4)", "Kitchen", "14.99", True),
    _product(3, "cast-iron-skillet", "Cast iron skillet, 26 cm", "Kitchen", "34.00", True),
    _product(4, "chef-knife", "Chef's knife, 20 cm", "Kitchen", "49.95", True),
    _product(5, "linen-tea-towels", "Linen tea towels (2)", "Kitchen", "12.00", True),
    _product(6, "bamboo-board", "Bamboo chopping board", "Kitchen", "18.75", True),
    _product(7, "desk-lamp", "Adjustable desk lamp", "Home office", "39.99", True),
    _product(8, "ergonomic-chair", "Ergonomic office chair", "Home office", "1249.00", True),
    _product(9, "monitor-arm", "Single monitor arm", "Home office", "64.50", True),
    _product(10, "notebook-a5", "A5 dotted notebook", "Stationery", "6.25", True),
    _product(11, "fountain-pen", "Fountain pen, medium nib", "Stationery", "27.00", True),
    _product(12, "pencil-set", "Graphite pencil set (12)", "Stationery", "9.99", True),
    _product(13, "washi-tape", "Washi tape (5 rolls)", "Stationery", "7.49", True),
    _product(14, "wool-throw", "Wool throw blanket", "Home", "79.00", True),
    _product(15, "scented-candle", "Fig scented candle", "Home", "22.00", False),
    _product(16, "plant-pot", "Terracotta plant pot, 15 cm", "Garden", "11.50", True),
    _product(17, "watering-can", "Galvanised watering can", "Garden", "29.95", True),
    _product(18, "secateurs", "Bypass secateurs", "Garden", "19.99", True),
    _product(19, "seed-tray", "Seed tray set (3)", "Garden", "8.99", False),
    _product(20, "garden-kneeler", "Foam garden kneeler", "Garden", "12.49", True),
]
BIRD_FEEDER = _product(21, "bird-feeder", "Hanging bird feeder", "Garden", "16.00", True)
# Listing order: catalogue order, new products last.
ORDER = {p.slug: i for i, p in enumerate([*CATALOGUE, BIRD_FEEDER])}


def changed(
    products: Iterable[Product],
    *,
    prices: dict[str, str] | None = None,
    flip_stock: Iterable[str] = (),
    remove: Iterable[str] = (),
    add: Iterable[Product] = (),
) -> list[Product]:
    prices = prices or {}
    flips, gone = set(flip_stock), set(remove)
    out = []
    for p in products:
        if p.slug in gone:
            continue
        if p.slug in prices:
            p = replace(p, price=Decimal(prices[p.slug]))
        if p.slug in flips:
            p = replace(p, in_stock=not p.in_stock)
        out.append(p)
    return sorted([*out, *add], key=lambda p: ORDER[p.slug])


def catalogues() -> dict[int, list[Product]]:
    day_1 = list(CATALOGUE)
    day_2 = changed(
        day_1,
        prices={
            "espresso-cups": "16.49",
            "linen-tea-towels": "9.60",
            "ergonomic-chair": "1099.00",
            "monitor-arm": "69.00",
            "washi-tape": "5.99",
        },
        flip_stock=["chef-knife", "fountain-pen", "scented-candle"],
        remove=["desk-lamp"],
    )
    desk_lamp = next(p for p in day_1 if p.slug == "desk-lamp")
    day_3 = changed(day_2, prices={"espresso-cups": "13.99"}, add=[desk_lamp, BIRD_FEEDER])
    return {1: day_1, 2: day_2, 3: day_3}


def item(p: Product) -> str:
    stock = "In stock" if p.in_stock else "Out of stock"
    return (
        f'  <li class="product" data-sku="{p.sku}">\n'
        f'    <h2><a class="name" href="{p.path}">{html.escape(p.title)}</a></h2>\n'
        f'    <p class="category">{html.escape(p.category)}</p>\n'
        f'    <p class="price">£{p.price:,.2f}</p>\n'
        f'    <p class="stock">{stock}</p>\n'
        "  </li>\n"
    )


def page(day: int, number: int, products: list[Product], *, last: bool) -> str:
    links = []
    if number > 1:
        links.append('<a rel="prev" href="./">Previous page</a>')
    if not last:
        links.append(f'<a rel="next" href="page-{number + 1}.html">Next page</a>')
    return (
        "<!doctype html>\n"
        f"<!-- shop-sim day {day}, generated by scripts/build_shop_sim.py -->\n"
        '<html lang="en-GB"><head><meta charset="utf-8">'
        f"<title>Shop Sim - catalogue, page {number}</title></head>\n<body>\n"
        '<header><h1>Shop Sim</h1><nav><a href="/shop-sim/">Catalogue</a></nav></header>\n'
        f'<main>\n<ul class="products">\n{"".join(item(p) for p in products)}</ul>\n'
        f'<nav class="pager">{" ".join(links)}</nav>\n</main>\n'
        "<footer>Simulated shop for scrapeline tests. Prices are made up.</footer>\n"
        "</body></html>\n"
    )


def golden(day: int, products: list[Product]) -> str:
    records = [
        {
            "external_id": "{FIXTURE_BASE_URL}" + p.path,
            "url": "{FIXTURE_BASE_URL}" + p.path,
            "title": p.title,
            "sku": p.sku,
            "category": p.category,
            "price": {"amount": f"{p.price:.2f}", "currency": "GBP"},
            "in_stock": p.in_stock,
        }
        for p in products
    ]
    data = {"site": f"shop-sim/day-{day}", "schema": "product", "records": records}
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def build(sites: Path = SITES, golden_dir: Path = GOLDEN) -> list[Path]:
    """Write every day's pages and golden rows; return the paths written."""
    written = []
    for day, products in catalogues().items():
        out = sites / "shop-sim" / f"day-{day}"
        out.mkdir(parents=True, exist_ok=True)
        pages = [products[:FIRST_PAGE_SIZE], products[FIRST_PAGE_SIZE:]]
        for number, rows in enumerate(pages, start=1):
            path = out / ("index.html" if number == 1 else f"page-{number}.html")
            path.write_text(page(day, number, rows, last=number == len(pages)), encoding="utf-8")
            written.append(path)
        golden_dir.mkdir(parents=True, exist_ok=True)
        path = golden_dir / f"shop-sim-day-{day}.json"
        path.write_text(golden(day, products), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in build():
        print(path.relative_to(ROOT))
