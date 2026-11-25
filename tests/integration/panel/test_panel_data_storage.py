"""Storage pages over the seeded database: raw store, database tables, lake and raw objects."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from scrapeline.analytics.export import ParquetLakeExporter
from scrapeline.catalog.datasets import default_registry
from scrapeline.serve.panel.app import create_app
from scrapeline.store import tables as t
from tests.integration.panel.conftest import Seeded
from tests.integration.panel.test_panel_data_records import counting_queries

pytestmark = pytest.mark.integration


def main(html: str) -> str:
    return re.split(r"<main\b[^>]*>", html, maxsplit=1)[1].split("</main>", 1)[0]


def section(html: str, anchor: str) -> str:
    return main(html).split(f'<h2 id="{anchor}">', 1)[1].split("<h2", 1)[0]


def fetch_of(seeded: Seeded, url_suffix: str) -> Any:
    f = t.fetch_log
    with seeded.engine.connect() as conn:
        return conn.execute(
            select(f).where(f.c.url.endswith(url_suffix), f.c.raw_sha256.is_not(None))
        ).one()


def test_storage_explains_the_raw_store(client: TestClient, seeded: Seeded) -> None:
    page = client.get("/storage")
    assert page.status_code == 200
    raw = section(page.text, "raw")
    assert str(seeded.settings.raw_store_dir) in raw
    assert "&lt;source&gt;/&lt;yyyy&gt;/&lt;mm&gt;/&lt;dd&gt;/&lt;sha256&gt;.&lt;ext&gt;" in raw
    by_source = dict(
        re.findall(r'<a href="/sources/([\w-]+)">[\w-]+</a></th>\s*<td class="num">(\d+)</td>', raw)
    )
    # Six distinct shop pages over three days; the failed day 4 stored nothing.
    assert by_source == {"legistar-testcity": "2", "shop-sim": "6"}
    day3 = fetch_of(seeded, "/day-3/")
    assert f'<a class="mono" href="/storage/raw/{day3.raw_sha256}">' in raw


def test_storage_lists_database_tables_by_schema(client: TestClient) -> None:
    database = section(client.get("/storage").text, "database")
    for table in ("data.records", "data.record_versions", "raw.fetch_log", "public.meetings"):
        assert f'<th scope="row" class="mono">{table}</th>' in database
    assert "ops.run" in database and "catalog.dataset_type" in database


def test_storage_before_any_export_or_dbt_build(client: TestClient) -> None:
    page = client.get("/storage")
    assert "The lake has not been exported yet. Run: make lake" in section(page.text, "lake")
    assert "Build it with: make dbt" in section(page.text, "warehouse")


def test_storage_shows_the_exported_lake(seeded: Seeded, tmp_path: Path) -> None:
    # Exporting reads the seed and writes files only, into a lake of this test's own.
    settings = seeded.settings.model_copy(update={"lake_dir": tmp_path / "lake"})
    report = ParquetLakeExporter(seeded.engine, settings.lake_dir, default_registry()).export()
    assert report.rows["record_versions"] == 32
    client = TestClient(create_app(engine=seeded.engine, settings=settings))
    lake = section(client.get("/storage").text, "lake")
    assert str(settings.lake_dir) in lake
    row = lake.split('<th scope="row" class="mono">record_versions</th>', 1)[1].split("</tr>")[0]
    cells = re.findall(r'<td class="num">([\d,]+)</td>', row)
    assert int(cells[0]) >= 1 and cells[1] == "32"  # files, rows
    assert "append" in row and "never" not in row
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC</span>", row)  # not a raw ISO string
    assert "32 rows written" in row
    snapshot = lake.split('<th scope="row" class="mono">sources</th>', 1)[1].split("</tr>")[0]
    assert "snapshot" in snapshot and '<td class="num">2</td>' in snapshot


def test_raw_object_shows_fetches_citations_and_escaped_html(
    client: TestClient, seeded: Seeded
) -> None:
    day3 = fetch_of(seeded, "/day-3/")
    page = client.get(f"/storage/raw/{day3.raw_sha256}")
    assert page.status_code == 200
    body = main(page.text)
    assert "http://shop-sim.test/shop-sim/day-3/" in body
    assert f'href="/runs/{seeded.shop_runs[2]}"' in body
    assert f'href="/storage/raw/{day3.raw_sha256}/download"' in body
    with seeded.engine.connect() as conn:
        records = conn.execute(
            select(func.count())
            .select_from(t.records)
            .where(t.records.c.raw_sha256 == day3.raw_sha256)
        ).scalar_one()
        version_rows = conn.execute(
            select(func.count())
            .select_from(t.record_versions)
            .where(t.record_versions.c.raw_sha256 == day3.raw_sha256)
        ).scalar_one()
    assert records > 0 and version_rows > 0
    assert f"{records} row{'s' if records != 1 else ''} in <code>records</code>" in body
    assert f"{version_rows} version{'s' if version_rows != 1 else ''} in" in body
    assert 'href="/data/product/' in body
    # The page's HTML is shown as text, never as markup.
    preview = body.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert preview.startswith("&lt;!doctype html&gt;")
    assert "&lt;li class=&#34;product&#34; data-sku=&#34;SIM-0002&#34;&gt;" in preview
    assert "<li" not in preview and "<a " not in preview


def test_raw_json_from_an_api_is_previewed(client: TestClient, seeded: Seeded) -> None:
    bodies = fetch_of(seeded, "/bodies")
    body = main(client.get(f"/storage/raw/{bodies.raw_sha256}").text)
    assert "application/json" in body and "BodyName" in body.split("<pre>", 1)[1]


def test_download_is_an_attachment_with_the_exact_bytes(client: TestClient, seeded: Seeded) -> None:
    day3 = fetch_of(seeded, "/day-3/")
    response = client.get(f"/storage/raw/{day3.raw_sha256}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert (
        response.headers["content-disposition"] == f'attachment; filename="{day3.raw_sha256}.html"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert hashlib.sha256(response.content).hexdigest() == day3.raw_sha256
    stored = (seeded.settings.raw_store_dir / day3.raw_key).read_bytes()
    assert response.content == stored


@pytest.mark.parametrize(
    "sha",
    [
        "abc",
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "0" * 64,  # well formed, but no fetch stored it
    ],
)
def test_unknown_raw_objects_are_not_found(client: TestClient, sha: str) -> None:
    assert client.get(f"/storage/raw/{sha}").status_code == 404
    assert client.get(f"/storage/raw/{sha}/download").status_code == 404


def assert_fits_a_phone(html: str) -> None:
    """Tables scroll in the shell's wrapper, and `.empty` is only the empty-state box."""
    body = main(html)
    assert body.count("<table") == body.count('<div class="table-wrap">\n<table'), body
    assert '<span class="empty"' not in body and "<caption" not in body


def test_data_and_storage_pages_use_the_shell_layout(client: TestClient, seeded: Seeded) -> None:
    day3 = fetch_of(seeded, "/day-3/")
    with seeded.engine.connect() as conn:
        lamp = conn.execute(
            select(t.records.c.id).where(t.records.c.external_id == "SIM-0007")
        ).scalar_one()
        meeting = conn.execute(select(func.min(t.meetings.c.id))).scalar_one()
    for path in (
        "/data",
        "/data/product",
        f"/data/product/{lamp}",
        "/data/meeting",
        f"/data/meeting/{meeting}",
        "/storage",
        f"/storage/raw/{day3.raw_sha256}",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert_fits_a_phone(page.text)


def test_storage_pages_use_a_fixed_number_of_queries(client: TestClient, seeded: Seeded) -> None:
    day3 = fetch_of(seeded, "/day-3/")
    for path in ("/storage", f"/storage/raw/{day3.raw_sha256}"):
        with counting_queries(seeded.engine) as statements:
            assert client.get(path).status_code == 200
        assert len(statements) <= 6, (path, statements)
