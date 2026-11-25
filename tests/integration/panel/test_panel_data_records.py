"""Data explorer pages over the seeded database: dataset list, records and record lineage."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select

from scrapeline.serve.panel import filters
from scrapeline.store import tables as t
from tests.integration.panel.conftest import Seeded

pytestmark = pytest.mark.integration

EN_DASH = chr(0x2013)  # the pager separates a range with an en dash


def main(html: str) -> str:
    return re.split(r"<main\b[^>]*>", html, maxsplit=1)[1].split("</main>", 1)[0]


def section(html: str, heading: str) -> str:
    return main(html).split(f"<h2>{heading}</h2>", 1)[1].split("<h2>", 1)[0]


def record_id(seeded: Seeded, sku: str) -> int:
    with seeded.engine.connect() as conn:
        found: int = conn.execute(
            select(t.records.c.id).where(t.records.c.external_id == sku)
        ).scalar_one()
    return found


def versions(seeded: Seeded, rid: int) -> list[Any]:
    v = t.record_versions
    with seeded.engine.connect() as conn:
        return list(conn.execute(select(v).where(v.c.record_id == rid).order_by(v.c.version)))


@contextmanager
def counting_queries(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", count)


def test_data_lists_every_dataset_type_with_its_records(client: TestClient) -> None:
    page = client.get("/data")
    assert page.status_code == 200
    rows = {
        name: row
        for name, row in re.findall(
            r'<a href="/data/(\w+)">\w+</a>(.*?)</tr>', main(page.text), flags=re.DOTALL
        )
    }
    product = rows["product"]
    assert "v1" in product and "generic" in product
    assert '<td class="num">21</td>' in product  # current
    assert '<td class="num">0</td>' in product  # removed: the desk lamp came back
    assert '<a href="/sources/shop-sim">shop-sim</a>' in product
    assert "not&nbsp;synced" not in product
    meeting = rows["meeting"]
    assert "typed" in meeting and '<td class="num">25</td>' in meeting
    assert '<a href="/sources/legistar-testcity">legistar-testcity</a>' in meeting
    # Defined in the registry, never synced or used.
    assert "not&nbsp;synced" in rows["article"] and "never" in rows["article"]


def test_product_records_are_listed_with_typed_columns(client: TestClient) -> None:
    page = client.get("/data/product")
    assert page.status_code == 200
    body = main(page.text)
    assert "21 records (current)" in body
    assert body.count('<th scope="row"><a class="mono" href="/data/product/') == 21
    header = body.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert 'url <abbr title="Key field: part of the record identity">key</abbr>' in header
    espresso = body.split("SIM-0002</a>", 1)[1].split("</tr>", 1)[0]
    assert '<td class="num">13.99 GBP</td>' in espresso
    assert "<td>yes</td>" in espresso
    assert (
        '<a href="http://shop-sim.test/shop-sim/products/espresso-cups.html" '
        'rel="noopener noreferrer nofollow"'
    ) in espresso
    assert "Not shown because they are empty on this page: list_price" in body


def test_records_are_newest_first(client: TestClient) -> None:
    body = main(client.get("/data/product").text)
    # Bird feeder is new on day 3; every other product was last seen in the same run.
    assert body.index("SIM-0021") < body.index("SIM-0001")


def test_record_filters(client: TestClient) -> None:
    def count(**params: str) -> int:
        body = main(client.get("/data/product", params=params).text)
        return body.count('<th scope="row"><a class="mono" href="/data/product/')

    assert count(q="espresso") == 1
    assert count(q="ESPRESSO") == 1
    assert count(q="SIM-0007") == 1  # external ids are searched too
    assert count(q="title") == 0  # field names are not
    assert count(q="%") == 0  # wildcards are literal
    assert count(source_id="shop-sim") == 21
    assert count(source_id="legistar-testcity") == 0
    assert count(state="removed") == 0
    assert count(state="all") == 21
    assert count(q="espresso", source_id="", state="current") == 1  # blank form fields


def test_record_list_pages(client: TestClient) -> None:
    body = main(client.get("/data/product", params={"limit": 5, "offset": 5}).text)
    assert body.count('href="/data/product/') == 5
    assert f"6{EN_DASH}10 of 21" in body
    assert "offset=10" in body and "offset=0" in body


def test_invalid_filters_are_rejected(client: TestClient) -> None:
    assert client.get("/data/product", params={"state": "gone"}).status_code == 422
    assert client.get("/data/product", params={"q": "x" * 101}).status_code == 422


def test_typed_meetings_come_from_their_table(client: TestClient) -> None:
    page = client.get("/data/meeting")
    assert page.status_code == 200
    body = main(page.text)
    assert "25 records" in body and "without version history" in body
    assert body.count('href="/data/meeting/') == 25
    assert 'name="state"' not in body  # typed tables keep no removals
    assert "Select Budget Committee" in body
    # Ten fields: the first eight that have values show, the rest wait on the record page.
    assert "More fields on each record's page: minutes_url, source_modified_at." in body
    assert "body_<wbr>external_<wbr>id" in body
    assert count_text(body, "<td>09:30</td>") >= 1
    filtered = main(client.get("/data/meeting", params={"q": "budget committee"}).text)
    assert 0 < filtered.count('href="/data/meeting/') < 25


def count_text(body: str, text: str) -> int:
    return body.count(text)


@pytest.mark.parametrize("path", ["/data/nope", "/data/Product", "/data/product%2F..", "/data/x-y"])
def test_unknown_datasets_are_not_found(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_desk_lamp_lineage_shows_removal_and_restore(client: TestClient, seeded: Seeded) -> None:
    rid = record_id(seeded, "SIM-0007")
    stored = versions(seeded, rid)
    page = client.get(f"/data/product/{rid}")
    assert page.status_code == 200
    provenance = section(page.text, "Identity and provenance")
    assert '<a href="/sources/shop-sim">shop-sim</a>' in provenance
    assert f'<a class="mono" href="/runs/{seeded.shop_runs[2]}">' in provenance
    assert f'<a class="mono" href="/storage/raw/{stored[2].raw_sha256}">' in provenance
    assert (
        '<a href="http://shop-sim.test/shop-sim/products/desk-lamp.html" '
        'rel="noopener noreferrer nofollow"'
    ) in provenance
    assert "DeclarativeAdapter" in provenance

    history = section(page.text, "History")
    rows = history.split("<tbody>", 1)[1].split("</tr>")[:3]  # newest first
    assert ["restored" in rows[0], "removed" in rows[1], "insert" in rows[2]] == [True] * 3
    # valid_to is the next version's valid_from; the newest is still current.
    assert f"<td>{filters.dt(stored[1].valid_from)}</td>" in rows[2]
    assert f"<td>{filters.dt(stored[2].valid_from)}</td>" in rows[1]
    assert '<span class="muted">current</span>' in rows[0]
    assert "a removal has no values" in rows[1]
    assert "No field changes since version 1." in rows[0]
    for row, run in zip(rows, reversed(seeded.shop_runs), strict=True):
        assert f'href="/runs/{run}"' in row
    assert f'href="/storage/raw/{stored[0].raw_sha256}"' in rows[2]


def test_espresso_lineage_diffs_each_price_change(client: TestClient, seeded: Seeded) -> None:
    rid = record_id(seeded, "SIM-0002")
    history = section(client.get(f"/data/product/{rid}").text, "History")
    first = "<code>price</code>: <del>14.99 GBP</del> → <ins>16.49 GBP</ins>"
    second = "<code>price</code>: <del>16.49 GBP</del> → <ins>13.99 GBP</ins>"
    assert first in history and second in history
    assert history.index(second) < history.index(first)  # newest first
    assert history.count("<li><code>") == 2  # only the price changed
    current = section(client.get(f"/data/product/{rid}").text, "Current values")
    assert "<td>13.99 GBP</td>" in current


def test_meeting_lineage_has_provenance_but_no_history(client: TestClient, seeded: Seeded) -> None:
    with seeded.engine.connect() as conn:
        meeting = conn.execute(select(t.meetings).order_by(t.meetings.c.id).limit(1)).one()
    page = client.get(f"/data/meeting/{meeting.id}")
    assert page.status_code == 200
    assert "Typed tables keep the current row only" in page.text
    provenance = section(page.text, "Identity and provenance")
    assert f'href="/runs/{seeded.legistar_run}"' in provenance
    assert f'href="/storage/raw/{meeting.raw_sha256}"' in provenance
    assert meeting.content_hash in provenance
    assert f"<td>{meeting.title}</td>" in section(page.text, "Current values")


@pytest.mark.parametrize("rid", ["999999", "0", "abc", "99999999999999999999"])
def test_unknown_records_are_not_found(client: TestClient, rid: str) -> None:
    assert client.get(f"/data/product/{rid}").status_code == 404


def test_a_record_is_only_found_under_its_own_dataset(client: TestClient, seeded: Seeded) -> None:
    rid = record_id(seeded, "SIM-0002")
    assert client.get(f"/data/job_posting/{rid}").status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/data", "/data/product", "/data/product?q=espresso", "/data/meeting", "/data/product/2"],
)
def test_pages_use_a_fixed_number_of_queries(client: TestClient, seeded: Seeded, path: str) -> None:
    with counting_queries(seeded.engine) as statements:
        assert client.get(path).status_code == 200
    assert len(statements) <= 6, statements
