"""Data and storage pages on an empty database, and with hostile or broken stored content.

These tests write rows, so they use `clean_db` and live apart from the seeded page tests.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from scrapeline.serve.panel.app import create_app
from scrapeline.settings import Settings
from scrapeline.store import tables as t
from scrapeline.store.raw import FilesystemRawStore
from tests.integration.panel.conftest import panel_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
SCRIPT = "<script>alert('xss')</script>"
HOSTILE_PAGE = (
    f"<!doctype html><html><body><h1>{SCRIPT}</h1>"
    "<img src=x onerror=alert(1)><a href='javascript:alert(2)'>x</a></body></html>"
).encode()


def main(html: str) -> str:
    return re.split(r"<main\b[^>]*>", html, maxsplit=1)[1].split("</main>", 1)[0]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return panel_settings(tmp_path)


@pytest.fixture
def client(clean_db: Engine, settings: Settings) -> TestClient:
    return TestClient(create_app(engine=clean_db, settings=settings))


@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("/data", "not&nbsp;synced"),  # dataset types still come from the registry files
        ("/data/product", "No records yet."),
        ("/data/meeting", "No records yet."),
        ("/storage", "Nothing fetched yet."),
        ("/marts", "No warehouse yet"),
        ("/marts/price-history", "No warehouse yet"),
        ("/marts/scraper-health", "No warehouse yet"),
    ],
)
def test_pages_render_on_an_empty_database(client: TestClient, path: str, says: str) -> None:
    page = client.get(path)
    assert page.status_code == 200, page.text
    assert says in page.text


def test_storage_on_an_empty_database_says_what_to_run(client: TestClient) -> None:
    body = main(client.get("/storage").text)
    assert "Run: make lake" in body and "make dbt" in body
    assert "(not created yet)" in body  # the raw store directory does not exist yet
    assert '<th scope="row" class="mono">data.records</th>' in body


def test_nothing_to_find_on_an_empty_database(client: TestClient) -> None:
    assert client.get("/data/product/1").status_code == 404
    assert client.get("/data/meeting/1").status_code == 404
    assert client.get("/storage/raw/" + "a" * 64).status_code == 404


@dataclass(frozen=True)
class Stored:
    record_id: int
    sha256: str


def _source_and_run(engine: Engine) -> uuid.UUID:
    run_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            t.sources.insert().values(
                id="hostile",
                name=SCRIPT,
                platform="fixture",
                status="active",
                config_version=1,
                config={},
                dataset="product",
            )
        )
        conn.execute(
            t.run.insert().values(
                id=run_id, source_id="hostile", kind="incremental", status="succeeded"
            )
        )
    return run_id


def _fetch(
    engine: Engine,
    run_id: uuid.UUID,
    sha256: str,
    key: str | None,
    content_type: str,
    nbytes: int,
    fetched_at: datetime = NOW,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            t.fetch_log.insert().values(
                run_id=run_id,
                source_id="hostile",
                url=f"https://hostile.example/{sha256[:8]}?q={SCRIPT}",
                status=200,
                nbytes=nbytes,
                content_type=content_type,
                raw_sha256=sha256,
                raw_key=key,
                fetched_at=fetched_at,
            )
        )


@pytest.fixture
def hostile(clean_db: Engine, settings: Settings) -> Stored:
    """A record whose scraped values and raw page are hostile."""
    run_id = _source_and_run(clean_db)
    ref = FilesystemRawStore(settings.raw_store_dir).put("hostile", HOSTILE_PAGE, "html", NOW)
    _fetch(clean_db, run_id, ref.sha256, ref.key, "text/html", len(HOSTILE_PAGE))
    payload = {
        "url": "javascript:alert(3)",
        "title": SCRIPT,
        "price": {"amount": "1.00", "currency": "<b>GBP</b>"},
        "in_stock": True,
    }
    lineage = {
        "source_id": "hostile",
        "content_hash": "f" * 64,
        "schema_version": 1,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "fetched_at": NOW,
        "run_id": run_id,
        "raw_sha256": ref.sha256,
    }
    with clean_db.begin() as conn:
        record_id = conn.execute(
            t.records.insert()
            .values(
                dataset="product",
                external_id=f"<i>{SCRIPT}</i>",
                canonical_url="javascript:alert(4)",
                payload=payload,
                **lineage,
            )
            .returning(t.records.c.id)
        ).scalar_one()
        conn.execute(
            t.record_versions.insert().values(
                record_id=record_id,
                version=1,
                schema_version=1,
                change_type="insert",
                payload=payload,
                content_hash="f" * 64,
                valid_from=NOW,
                run_id=run_id,
                raw_sha256=ref.sha256,
            )
        )
    return Stored(record_id, ref.sha256)


def _assert_inert(html: str) -> None:
    """Hostile values may appear as escaped text, never as markup or live links."""
    body = main(html)
    assert "<script" not in body and "<img" not in body
    assert "<b>" not in body and "<i>" not in body
    assert 'href="javascript:' not in body and "href='javascript:" not in body


def test_hostile_values_are_escaped_in_lists_and_lineage(
    client: TestClient, hostile: Stored
) -> None:
    listing = client.get("/data/product")
    assert listing.status_code == 200
    _assert_inert(listing.text)
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in listing.text
    lineage = client.get(f"/data/product/{hostile.record_id}")
    assert lineage.status_code == 200
    _assert_inert(lineage.text)
    assert "javascript:alert(4)" in lineage.text  # shown as text, not linked
    sources = client.get("/data")
    _assert_inert(sources.text)


def test_hostile_raw_html_is_shown_as_text_and_downloaded(
    client: TestClient, hostile: Stored
) -> None:
    page = client.get(f"/storage/raw/{hostile.sha256}")
    assert page.status_code == 200
    _assert_inert(page.text)
    preview = main(page.text).split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert "&lt;h1&gt;&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;&lt;/h1&gt;" in preview
    assert "<img" not in page.text
    download = client.get(f"/storage/raw/{hostile.sha256}/download")
    assert download.content == HOSTILE_PAGE
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "script-src 'self'" in download.headers["content-security-policy"]
    storage = client.get("/storage")
    _assert_inert(storage.text)


def test_binary_raw_objects_are_not_previewed(
    clean_db: Engine, settings: Settings, client: TestClient
) -> None:
    run_id = _source_and_run(clean_db)
    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 8
    ref = FilesystemRawStore(settings.raw_store_dir).put("hostile", pdf, "pdf", NOW)
    _fetch(clean_db, run_id, ref.sha256, ref.key, "application/pdf", len(pdf))
    body = main(client.get(f"/storage/raw/{ref.sha256}").text)
    assert "Binary content is not previewed" in body and "<pre>" not in body
    assert "2.0 KiB" in body
    download = client.get(f"/storage/raw/{ref.sha256}/download")
    assert download.content == pdf
    assert download.headers["content-disposition"] == f'attachment; filename="{ref.sha256}.pdf"'


def test_storage_counts_raw_files_as_they_are_on_disk(
    clean_db: Engine, settings: Settings, client: TestClient
) -> None:
    # An unchanged page fetched twice a day for three days: one file per day, as the
    # date is part of the raw key.
    run_id = _source_and_run(clean_db)
    store = FilesystemRawStore(settings.raw_store_dir)
    page = b"<html><body>An unchanged catalogue</body></html>"
    for day in (18, 19, 20):
        fetched_at = NOW.replace(day=day)
        ref = store.put("hostile", page, "html", fetched_at)
        for _ in range(2):
            _fetch(clean_db, run_id, ref.sha256, ref.key, "text/html", len(page), fetched_at)
    on_disk = [path for path in settings.raw_store_dir.rglob("*") if path.is_file()]
    assert len(on_disk) == 3
    raw = main(client.get("/storage").text).split('<h2 id="raw">', 1)[1].split("<h2", 1)[0]
    assert "<dt>Objects</dt><dd>3</dd>" in raw
    assert f"<dt>Size</dt><dd>{3 * len(page)} B</dd>" in raw
    row = raw.split('<a href="/sources/hostile">hostile</a></th>', 1)[1].split("</tr>", 1)[0]
    assert re.findall(r'<td class="num">([^<]*)</td>', row) == ["3", f"{3 * len(page)} B", "6"]


def test_a_missing_raw_file_is_reported(clean_db: Engine, client: TestClient) -> None:
    run_id = _source_and_run(clean_db)
    sha = hashlib.sha256(b"never stored").hexdigest()
    _fetch(clean_db, run_id, sha, f"hostile/2026/11/20/{sha}.html", "text/html", 12)
    page = client.get(f"/storage/raw/{sha}")
    assert page.status_code == 200 and "The file is not in the raw store" in page.text
    assert client.get(f"/storage/raw/{sha}/download").status_code == 404


def test_a_raw_key_outside_the_store_is_never_read(
    clean_db: Engine, settings: Settings, client: TestClient, tmp_path: Path
) -> None:
    secret = b"secret outside the raw store"
    sha = hashlib.sha256(secret).hexdigest()
    (tmp_path / f"{sha}.txt").write_bytes(secret)  # next to the store root, not inside it
    run_id = _source_and_run(clean_db)
    _fetch(clean_db, run_id, sha, f"../{sha}.txt", "text/plain", len(secret))
    page = client.get(f"/storage/raw/{sha}")
    assert page.status_code == 200 and secret.decode() not in page.text
    assert client.get(f"/storage/raw/{sha}/download").status_code == 404


def test_a_tampered_raw_file_fails_its_hash_check(
    clean_db: Engine, settings: Settings, client: TestClient
) -> None:
    run_id = _source_and_run(clean_db)
    ref = FilesystemRawStore(settings.raw_store_dir).put("hostile", b"original", "txt", NOW)
    path = settings.raw_store_dir / ref.key
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    _fetch(clean_db, run_id, ref.sha256, ref.key, "text/plain", 8)
    page = client.get(f"/storage/raw/{ref.sha256}")
    assert page.status_code == 200
    body = main(page.text)
    assert "hash mismatch" in body
    assert "<pre>" not in body and "tampered" not in body  # unverified bytes are not shown
    download = client.get(f"/storage/raw/{ref.sha256}/download")
    assert download.status_code == 500 and "integrity check" in download.text
