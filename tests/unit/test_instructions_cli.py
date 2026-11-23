"""`scrapeline instruction validate|apply`, writing only into temporary directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from scrapeline.catalog.datasets import load_dataset_file
from scrapeline.cli import app
from scrapeline.registry.config import load_source_file
from scrapeline.registry.instruction import DECLARATIVE_ADAPTER

ROOT = Path(__file__).resolve().parents[2]
BOOKS = ROOT / "config/instructions/books-toscrape.yaml"

NEW_DATASET = """
id: shelf-watch
name: Shelf watch
start_urls: [https://shop.example.com/books]
collect: "Every listed book with its link, title and price"
dataset: shelf_item
fields:
  url: {type: url, key: true}
  title: {type: text, required: true}
  price: money
usage: {purpose: Price tracking}
"""


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {name: tmp_path / name for name in ("sources", "datasets", "sites")}


def invoke(*args: str | Path) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(app, [str(a) for a in args])
    return result.exit_code, json.loads(result.stdout)


def apply(file: Path, dirs: dict[str, Path], *extra: str) -> tuple[int, dict[str, Any]]:
    return invoke(
        "instruction",
        "apply",
        file,
        "--sources-dir",
        dirs["sources"],
        "--datasets-dir",
        dirs["datasets"],
        "--sites-dir",
        dirs["sites"],
        *extra,
    )


def write(tmp_path: Path, text: str, name: str = "instruction.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_validate_prints_the_summary_and_writes_nothing(tmp_path: Path) -> None:
    code, out = invoke("instruction", "validate", BOOKS, "--datasets-dir", tmp_path)
    assert code == 0 and out["valid"] is True
    assert out["source_id"] == "books-toscrape" and out["dataset"] == "product"
    assert out["status"] == "draft" and out["dataset_is_new"] is False
    assert "price" in out["fields"] and "sku" in out["fields_excluded"]
    assert out["next_steps"][-1].startswith("Activate:")
    assert list(tmp_path.iterdir()) == []


def test_validate_reports_an_invalid_instruction(tmp_path: Path) -> None:
    bad = write(tmp_path, NEW_DATASET.replace("https://shop", "ftp://shop"))
    code, out = invoke("instruction", "validate", bad, "--datasets-dir", tmp_path / "none")
    assert code == 1 and "http(s)" in out["error"]


def test_apply_writes_source_dataset_and_spec(tmp_path: Path, dirs: dict[str, Path]) -> None:
    code, out = apply(write(tmp_path, NEW_DATASET), dirs)
    assert code == 0 and out["applied"] is True and out["dataset_is_new"] is True
    source_path = dirs["sources"] / "shelf-watch.yaml"
    dataset_path = dirs["datasets"] / "shelf_item.yaml"
    spec_path = dirs["sites"] / "shelf_watch" / "spec.yaml"
    assert sorted(out["written"]) == sorted(map(str, (source_path, dataset_path, spec_path)))
    cfg = load_source_file(source_path)
    assert cfg.status == "draft" and cfg.dataset == "shelf_item"
    assert cfg.adapter == DECLARATIVE_ADAPTER and cfg.owner == "instruction"
    dataset = load_dataset_file(dataset_path)
    assert dataset.pack == "user" and dataset.key_fields == ("url",)
    spec = yaml.safe_load(spec_path.read_text())
    assert spec["schema"] == "shelf_item" and set(spec["fields"]) == {"url", "title", "price"}


def test_apply_with_an_existing_dataset_writes_no_dataset_file(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    code, out = apply(BOOKS, dirs)
    assert code == 0 and not dirs["datasets"].exists()
    assert len(out["written"]) == 2
    assert load_source_file(dirs["sources"] / "books-toscrape.yaml").dataset == "product"


def test_apply_refuses_to_overwrite_without_force(tmp_path: Path, dirs: dict[str, Path]) -> None:
    file = write(tmp_path, NEW_DATASET)
    assert apply(file, dirs)[0] == 0
    source_path = dirs["sources"] / "shelf-watch.yaml"
    spec_path = dirs["sites"] / "shelf_watch" / "spec.yaml"
    source_path.write_text(source_path.read_text() + "# edited by a human\n")
    spec_path.write_text("# real selectors\n")

    code, out = apply(write(tmp_path, NEW_DATASET.replace("daily", "weekly")), dirs)
    assert code == 1 and "refusing to overwrite" in out["error"]
    assert out["existing"] == [str(source_path)]
    assert source_path.read_text().endswith("# edited by a human\n")

    code, out = apply(file, dirs, "--force")
    assert code == 0 and out["kept"] == [str(spec_path)]
    assert str(source_path) in out["written"] and not out["dataset_is_new"]
    assert "edited by a human" not in source_path.read_text()
    assert spec_path.read_text() == "# real selectors\n"


def test_apply_cannot_be_injected_through_instruction_text(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    collect = "Every \"book\" 'title'\nstatus: active\nadapter: evil.module:Adapter\nowner: x\n"
    text = NEW_DATASET.replace(
        'collect: "Every listed book with its link, title and price"',
        f"collect: {json.dumps(collect)}",
    ).replace("name: Shelf watch", 'name: "Shelf\\nstatus: active"')
    code, _ = apply(write(tmp_path, text), dirs)
    assert code == 0
    cfg = load_source_file(dirs["sources"] / "shelf-watch.yaml")
    assert cfg.status == "draft" and cfg.adapter == DECLARATIVE_ADAPTER
    assert cfg.owner == "instruction" and cfg.name == "Shelf\nstatus: active"
    assert cfg.instruction is not None and cfg.instruction.collect == collect
    spec = yaml.safe_load((dirs["sites"] / "shelf_watch" / "spec.yaml").read_text())
    assert spec["schema"] == "shelf_item"


@pytest.mark.parametrize("reference", ["${HOME}", "${ANTHROPIC_API_KEY:-unset}"])
def test_apply_refuses_environment_references(
    tmp_path: Path, dirs: dict[str, Path], reference: str
) -> None:
    text = NEW_DATASET.replace("Every listed book", f"Every {reference} book")
    code, out = apply(write(tmp_path, text), dirs)
    assert code == 1 and 'contain "${"' in out["error"]
    assert not dirs["sources"].exists()


def test_apply_refuses_credentials_and_local_hosts(tmp_path: Path, dirs: dict[str, Path]) -> None:
    for url in ("https://user:secret@shop.example.com/books", "http://127.0.0.1/admin"):
        text = NEW_DATASET.replace("https://shop.example.com/books", url)
        code, out = apply(write(tmp_path, text), dirs)
        assert code == 1 and "start URL" in out["error"]
    assert not dirs["sources"].exists() and not dirs["sites"].exists()


def test_apply_writes_a_draft_when_a_dataset_proposal_is_needed(
    tmp_path: Path, dirs: dict[str, Path]
) -> None:
    text = NEW_DATASET.split("dataset:")[0] + "usage: {purpose: Price tracking}\n"
    code, out = apply(write(tmp_path, text), dirs)
    assert code == 0 and out["dataset_proposal_needed"] is True
    assert not dirs["datasets"].exists()
    assert load_source_file(dirs["sources"] / "shelf-watch.yaml").dataset == "shelf_watch"
