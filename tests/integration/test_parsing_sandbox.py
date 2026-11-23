"""Generic specs in the offline docker sandbox give the same result as the in-process run.

Uses the image named by SANDBOX_IMAGE (default scrapeline-sandbox:latest); rebuild it after
changing the pure parsing modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scrapeline.agents.tester import evaluate_spec
from scrapeline.catalog.datasets import DatasetType
from scrapeline.pipeline.locale import Locale
from scrapeline.settings import Settings
from tests.unit.test_parsing_declarative import (
    DE,
    LISTING,
    LISTING_PAGE,
    LISTING_SPEC,
    PRODUCT_PAGE,
    PRODUCT_SPEC,
    UK,
    write_pages,
)

pytestmark = pytest.mark.integration

PAGE_2 = PRODUCT_PAGE.replace("_1000", "_2000").replace("_999", "_1999").replace("_998", "_1998")
PRODUCT_PAGES = {
    "index.html": PRODUCT_PAGE.replace("$50.10", "£50.10"),
    "page-2.html": PAGE_2.replace("$50.10", "£50.10").replace("page-2.html", "#"),
}


@pytest.mark.parametrize(
    ("spec", "pages", "locale", "dataset", "records"),
    [
        (PRODUCT_SPEC, PRODUCT_PAGES, UK, None, 6),
        (LISTING_SPEC, {"index.html": LISTING_PAGE}, DE, LISTING, 2),
    ],
    ids=["product", "user-dataset"],
)
def test_generic_spec_in_docker_sandbox_matches_local(
    tmp_path: Path,
    spec: str,
    pages: dict[str, str],
    locale: Locale,
    dataset: DatasetType | None,
    records: int,
) -> None:
    settings = Settings(_env_file=None)
    pages_dir = write_pages(tmp_path / "pages", pages)
    reports = {
        mode: evaluate_spec(
            spec, pages_dir, None, settings, mode=mode, locale=locale, dataset=dataset
        )
        for mode in ("local", "docker")
    }
    local, docker = reports["local"], reports["docker"]
    assert docker.passed, docker.summary()
    assert (docker.records, docker.pages_parsed) == (records, len(pages))
    assert docker.golden["sample_records"] == local.golden["sample_records"]
    assert docker.golden["checks"] == local.golden["checks"]


def test_docker_sandbox_rejects_ambiguous_values_like_local(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    pages_dir = write_pages(tmp_path / "pages", {"index.html": PRODUCT_PAGE})
    spec = PRODUCT_SPEC.replace("next_page", "# next_page")
    docker = evaluate_spec(spec, pages_dir, None, settings, mode="docker", locale=UK)
    local = evaluate_spec(spec, pages_dir, None, settings, mode="local", locale=UK)
    assert (docker.records, docker.rejects) == (local.records, local.rejects) == (2, 1)
    assert not docker.passed
