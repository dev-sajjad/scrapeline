"""Operator pages (health, sources, sites, runs, incidents) over the seeded pipeline output."""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from tests.integration.panel.conftest import SHOP_SIM_HOST, Seeded

pytestmark = pytest.mark.integration


def get(client: TestClient, path: str, status: int = 200) -> HTMLParser:
    response = client.get(path)
    assert response.status_code == status, response.text[:500]
    assert "text/html" in response.headers["content-type"]
    return HTMLParser(response.text)


def text(node: Node | None) -> str:
    assert node is not None
    return re.sub(r"\s+", " ", node.text(separator=" ")).strip()


def rows(tree: HTMLParser, selector: str = "#results tbody tr") -> list[str]:
    return [text(row) for row in tree.css(selector)]


def row_for(tree: HTMLParser, needle: str, selector: str = "tbody tr") -> Node:
    found = [row for row in tree.css(selector) if needle in text(row)]
    assert len(found) == 1, [text(row) for row in tree.css(selector)]
    return found[0]


def stat(tree: HTMLParser, label: str) -> str:
    for tile in tree.css("dl.stats > div"):
        if text(tile.css_first("dt")) == label:
            return text(tile.css_first("dd"))
    raise AssertionError(f"no stat {label!r}")


def kv(tree: HTMLParser, label: str) -> str:
    for pair in tree.css("dl.kv > div"):
        if text(pair.css_first("dt")) == label:
            return text(pair.css_first("dd"))
    raise AssertionError(f"no key {label!r}")


def classes(node: Node | None) -> str:
    return (node.attributes.get("class") or "") if node is not None else ""


def live_stream(tree: HTMLParser) -> tuple[str, dict[str, str]]:
    """The live feed's stream path and query, wherever the feed puts its sse-connect."""
    feed = tree.css_first("[sse-connect]")
    assert feed is not None
    url = urlsplit(feed.attributes["sse-connect"] or "")
    return url.path, dict(parse_qsl(url.query))


def section_rows(tree: HTMLParser, heading: str) -> list[str]:
    """Rows of the table under the h2 that starts with `heading`; [] for an empty state."""
    for h2 in tree.css("h2"):
        if not text(h2).startswith(heading):
            continue
        node = h2.parent if "section-head" in classes(h2.parent) else h2
        sibling = node.next
        while sibling is not None:
            if sibling.tag == "h2" or (sibling.tag == "div" and sibling.css_first("h2")):
                break
            if sibling.tag == "div" and sibling.css_first("table"):
                return [text(row) for row in sibling.css("tbody tr")]
            sibling = sibling.next
        return []
    raise AssertionError(f"no section {heading!r}")


# Health


def test_health_flags_the_failed_source_and_counts_the_queue(client: TestClient) -> None:
    tree = get(client, "/")
    assert text(tree.css_first(".verdict h1")) == "Health"
    verdict = text(tree.css_first(".verdict p"))
    assert "1 source needs attention" in verdict
    assert "2 open incidents" in verdict
    assert tree.css_first(".verdict-bad") is not None

    assert stat(tree, "Jobs queued").startswith("1 oldest")
    assert stat(tree, "Jobs running").startswith("0")
    assert stat(tree, "Stalled jobs").startswith("0")
    assert stat(tree, "Failed jobs").startswith("0")
    assert stat(tree, "Sources").startswith("2 1 draft, 1 active")
    assert stat(tree, "Need attention").startswith("1")

    shop = row_for(tree, "shop-sim Fixture")
    assert "flagged" in classes(shop)
    assert "last run failed" in text(shop)
    assert "failed" in text(shop.css_first("td:nth-child(4)"))
    # The draft source succeeded and is never flagged.
    legistar = row_for(tree, "legistar-testcity")
    assert "flagged" not in classes(legistar)
    assert "succeeded" in text(legistar)
    assert "149" in text(legistar)  # records parsed in its last run


def test_health_lists_open_incidents_lake_state_and_activity(client: TestClient) -> None:
    tree = get(client, "/")
    incidents = section_rows(tree, "Open incidents")
    assert any("Price missing on 2 of 20 products" in row for row in incidents)
    assert len(incidents) == 2
    assert stat(tree, "Lake export") == "never export with make lake"
    assert live_stream(tree) == ("/events/stream", {})


# Sources


def test_sources_list_shows_records_sites_and_last_runs(client: TestClient) -> None:
    tree = get(client, "/sources")
    shop = row_for(tree, "shop-sim")
    cells = [text(cell) for cell in shop.css("th, td")]
    assert cells[1] == "product"
    assert cells[2] == f"{SHOP_SIM_HOST} E-commerce"
    assert "active" in cells[3] and "last run failed" in cells[3]
    assert cells[4] == "manual"
    assert cells[5].startswith("failed")
    assert cells[6] == "21"
    assert cells[7] == "2"
    assert shop.css_first('a[href="/data/product"]') is not None
    assert shop.css_first(f'a[href="/sites/{SHOP_SIM_HOST}"]') is not None

    legistar = row_for(tree, "legistar-testcity")
    assert [text(cell) for cell in legistar.css("td")][5] == "25"  # typed meetings


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("status=active", ["shop-sim"]),
        ("status=draft", ["legistar-testcity"]),
        ("dataset=meeting", ["legistar-testcity"]),
        ("q=SHOP", ["shop-sim"]),
        ("q=test+city", ["legistar-testcity"]),
        ("q=%25", []),  # a literal %, not a wildcard
        ("status=retired", []),
    ],
)
def test_sources_filters_narrow_the_list(
    client: TestClient, query: str, expected: list[str]
) -> None:
    tree = get(client, f"/sources?{query}")
    ids = [text(row.css_first("th a")) for row in tree.css("#results tbody tr")]
    assert ids == expected
    if not expected:
        assert "No sources match these filters." in text(tree.css_first("#results"))


def test_source_page_shows_config_runs_incidents_and_events(
    client: TestClient, seeded: Seeded
) -> None:
    tree = get(client, "/sources/shop-sim")
    assert text(tree.css_first("h1")) == "shop-sim"
    assert kv(tree, "Status") == "active"
    assert kv(tree, "Dataset") == "product"
    assert kv(tree, "Site") == f"{SHOP_SIM_HOST} E-commerce"
    assert kv(tree, "Terms checked") == "2026-11-24"
    assert kv(tree, "Personal data") == "blanked before storage"
    assert kv(tree, "Locale currency") == "GBP"
    assert kv(tree, "Entry URL").startswith("http://shop-sim.test/shop-sim/day-")
    assert tree.css_first(".notice-bad") is None  # the terms were reviewed
    assert section_rows(tree, "Stored rows") == ["Current records 21"]
    config = tree.css_first("details.json pre")
    assert config is not None and '"dataset": "product"' in config.text()

    runs = section_rows(tree, "Recent runs")
    assert len(runs) == 4
    assert "failed" in runs[0] and "HTTPStatusError" in runs[0]
    assert tree.css_first(f'a[href="/runs/{seeded.failed_run}"]') is not None
    assert len(section_rows(tree, "Incidents")) == 2
    versions = section_rows(tree, "Scraper versions")
    assert len(versions) == 1
    assert "scrapeline.adapters.declarative:DeclarativeAdapter" in versions[0]
    assert live_stream(tree) == ("/events/stream", {"source_id": "shop-sim"})


def test_typed_source_page_counts_its_tables(client: TestClient) -> None:
    tree = get(client, "/sources/legistar-testcity")
    assert kv(tree, "Platform") == "legistar tenant testcity"
    stored = section_rows(tree, "Stored rows")
    assert "Meetings 25" in stored and "Bodies 83" in stored and "Documents 41" in stored
    assert kv(tree, "Status") == "draft"


def test_unknown_source_is_404(client: TestClient) -> None:
    tree = get(client, "/sources/no-such-source", status=404)
    assert "no-such-source" in text(tree.css_first("main"))


# Sites


def test_sites_list_shows_category_method_and_sources(client: TestClient) -> None:
    tree = get(client, "/sites")
    listed = rows(tree)
    assert len(listed) == 2
    assert listed[0].startswith(f"{SHOP_SIM_HOST} E-commerce 100% human — — — 1 ")
    assert listed[1] == "webapi.legistar.com Unclassified — none — — — 1 —"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("method=human", [SHOP_SIM_HOST]),
        ("method=none", ["webapi.legistar.com"]),
        ("category=ecommerce", [SHOP_SIM_HOST]),
        ("category=government", []),
    ],
)
def test_sites_filters_narrow_the_list(client: TestClient, query: str, expected: list[str]) -> None:
    tree = get(client, f"/sites?{query}")
    assert [text(row.css_first("th")) for row in tree.css("#results tbody tr")] == expected


def test_site_page_shows_evidence_log_and_sources(client: TestClient) -> None:
    tree = get(client, f"/sites/{SHOP_SIM_HOST}")
    assert text(tree.css_first("h1")) == SHOP_SIM_HOST
    assert kv(tree, "Category") == "E-commerce Online shops and marketplaces"
    assert kv(tree, "Confidence") == "100%"
    assert kv(tree, "Method").startswith("human")
    assert section_rows(tree, "Evidence") == ["human Simulated shop for tests 1"]
    log = section_rows(tree, "Classification log")
    assert len(log) == 1 and "Unclassified → E-commerce human 100% test" in log[0]
    assert section_rows(tree, "Sources on this site")[0].startswith("shop-sim Fixture")


def test_site_host_is_canonicalised(client: TestClient) -> None:
    tree = get(client, "/sites/WWW.Shop-Sim.TEST")
    assert text(tree.css_first("h1")) == SHOP_SIM_HOST


def test_unknown_site_is_404(client: TestClient) -> None:
    get(client, "/sites/unknown.example", status=404)
    get(client, "/sites/%20", status=404)


# Runs


def test_runs_list_is_newest_first_with_errors_and_load_summaries(
    client: TestClient, seeded: Seeded
) -> None:
    tree = get(client, "/runs")
    listed = tree.css("#results tbody tr")
    assert len(listed) == 5
    first = listed[0]
    assert first.css_first(f'a[href="/runs/{seeded.failed_run}"]') is not None
    assert "failed" in text(first)
    assert "HTTPStatusError: http 404" in text(first)
    day2 = row_for(tree, "8 updated")
    assert day2.css_first(f'a[href="/runs/{seeded.shop_runs[1]}"]') is not None
    assert "product: 8 updated, 1 removed, 11 unchanged" in text(day2)
    assert "body: 83 new; meeting: 25 new; document: 41 new" in text(
        row_for(tree, "legistar-testcity")
    )


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("status=failed", 1),
        ("status=succeeded", 4),
        ("source_id=legistar-testcity", 1),
        ("source_id=shop-sim&status=succeeded", 3),
        ("kind=backfill", 0),
    ],
)
def test_runs_filters_narrow_the_list(client: TestClient, query: str, count: int) -> None:
    tree = get(client, f"/runs?{query}")
    assert len(tree.css("#results tbody tr")) == count


def test_runs_pager_keeps_the_filters(client: TestClient) -> None:
    tree = get(client, "/runs?status=succeeded&limit=3")
    assert len(tree.css("#results tbody tr")) == 3
    pager = tree.css_first("nav.pager")
    assert "1\u20133 of 4" in text(pager)  # en dash
    next_link = pager.css_first('a[rel="next"]')
    assert next_link is not None
    assert next_link.attributes["href"].endswith("/runs?status=succeeded&limit=3&offset=3")
    assert len(get(client, "/runs?status=succeeded&limit=3&offset=3").css("#results tbody tr")) == 1


def test_run_page_shows_loads_checks_and_fetches(client: TestClient, seeded: Seeded) -> None:
    tree = get(client, f"/runs/{seeded.shop_runs[1]}")
    assert kv(tree, "Status") == "succeeded"
    assert kv(tree, "Complete crawl").startswith("yes")
    assert kv(tree, "Removed") == "1"
    assert section_rows(tree, "Loaded") == ["product 0 8 11 0 1"]
    checks = section_rows(tree, "Quality checks")
    assert len(checks) == 8
    assert any(row.startswith("record_count pass 19 [15, 30]") for row in checks)
    fetches = section_rows(tree, "Fetch log")
    assert len(fetches) == 2 and all(" 200 " in row for row in fetches)
    raw_links = [a.attributes["href"] for a in tree.css('a[href^="/storage/raw/"]')]
    assert len(raw_links) == 2
    assert all(re.fullmatch(r"/storage/raw/[0-9a-f]{64}", href or "") for href in raw_links)
    assert section_rows(tree, "Rejects") == []
    assert live_stream(tree) == ("/events/stream", {"run_id": str(seeded.shop_runs[1])})


def test_failed_run_shows_its_error_and_incident(client: TestClient, seeded: Seeded) -> None:
    tree = get(client, f"/runs/{seeded.failed_run}")
    notice = text(tree.css_first(".notice-bad"))
    assert notice == "Error: HTTPStatusError: http 404: http://shop-sim.test/shop-sim/day-4/"
    assert section_rows(tree, "Loaded") == []
    fetches = section_rows(tree, "Fetch log")
    assert len(fetches) == 1 and " 404 " in fetches[0]
    incidents = section_rows(tree, "Incidents")
    assert len(incidents) == 1 and "HTTPStatusError" in incidents[0]


def test_run_that_opened_the_seeded_incident_links_it(client: TestClient, seeded: Seeded) -> None:
    tree = get(client, f"/runs/{seeded.shop_runs[2]}")
    incidents = section_rows(tree, "Incidents")
    assert len(incidents) == 1 and "Price missing on 2 of 20 products" in incidents[0]
    assert section_rows(tree, "Loaded") == ["product 1 1 18 1 0"]


def test_unknown_run_is_404(client: TestClient) -> None:
    get(client, f"/runs/{uuid.uuid4()}", status=404)
    get(client, "/runs/not-a-uuid", status=404)


# Incidents


def test_incidents_default_to_open_and_acknowledged(client: TestClient, seeded: Seeded) -> None:
    tree = get(client, "/incidents")
    listed = rows(tree)
    assert len(listed) == 2
    seeded_row = row_for(tree, "Price missing on 2 of 20 products")
    assert seeded_row.css_first(f'a[href="/incidents/{seeded.incident_id}"]') is not None
    assert seeded_row.css_first(f'a[href="/runs/{seeded.shop_runs[2]}"]') is not None
    cells = [text(cell) for cell in seeded_row.css("td")]
    assert cells[:4] == ["shop-sim", "quality", "open", "1"]


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("status=closed", 0),
        ("status=all", 2),
        ("status=open", 2),
        ("kind=quality", 1),
        ("kind=fetch_error&status=all", 1),
        ("source_id=legistar-testcity", 0),
    ],
)
def test_incidents_filters_narrow_the_list(client: TestClient, query: str, count: int) -> None:
    tree = get(client, f"/incidents?{query}")
    assert len(tree.css("#results tbody tr")) == count


def test_incident_page_shows_details(client: TestClient, seeded: Seeded) -> None:
    tree = get(client, f"/incidents/{seeded.incident_id}")
    assert text(tree.css_first("h1")) == f"Incident #{seeded.incident_id}"
    assert text(tree.css_first(".notice")) == "Price missing on 2 of 20 products"
    assert kv(tree, "Occurrences") == "1"
    assert kv(tree, "Opened by run") == str(seeded.shop_runs[2])
    details = tree.css_first("main > pre")
    assert details is not None and '"check": "required_fields"' in details.text()


def test_unknown_incident_is_404_and_bad_ids_are_422(client: TestClient) -> None:
    get(client, "/incidents/999999", status=404)
    get(client, "/incidents/0", status=422)
    get(client, f"/incidents/{2**63}", status=422)


def test_pages_mark_their_nav_entry(client: TestClient, seeded: Seeded) -> None:
    for path, label in (
        ("/", "Health"),
        ("/sources/shop-sim", "Sources"),
        (f"/sites/{SHOP_SIM_HOST}", "Sites"),
        (f"/runs/{seeded.failed_run}", "Runs"),
        ("/incidents", "Incidents"),
    ):
        current = get(client, path).css('nav[aria-label="Main"] a[aria-current="page"]')
        assert [text(a) for a in current] == [label]
