"""`scrapeline agent generate --instruction <file>`, with the generator stubbed out (no LLM)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from scrapeline.cli import app
from scrapeline.registry.config import SourceConfig

ROOT = Path(__file__).resolve().parents[2]
BOOKS = ROOT / "config/instructions/books-toscrape.yaml"


class FakeResult:
    passed = True

    def as_dict(self) -> dict[str, Any]:
        return {"passed": True}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_generate(source_id: str, pages: Path, **kwargs: Any) -> FakeResult:
        seen.append({"source_id": source_id, "pages": pages, **kwargs})
        return FakeResult()

    monkeypatch.setattr("scrapeline.agents.generator.generate_site", fake_generate)
    monkeypatch.setattr("scrapeline.store.db.get_engine", lambda: None)
    return seen


def generate(tmp_path: Path, *args: str | Path) -> Result:
    argv = ["agent", "generate", *map(str, args), "--pages", str(tmp_path), "--driver", "noop"]
    return CliRunner().invoke(app, argv)


def source_of(call: dict[str, Any]) -> SourceConfig:
    return SourceConfig.model_validate(yaml.safe_load(call["source_yaml"]))


def test_instruction_file_builds_the_draft_source(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    result = generate(tmp_path, "--instruction", BOOKS, "--apply")
    assert result.exit_code == 0, result.output
    call = calls[0]
    assert call["source_id"] == "books-toscrape" and call["apply"] is True
    assert call["instruction"].startswith("Every book in the catalogue")
    assert "Fields to extract: url, title, price" in call["instruction"]
    cfg = source_of(call)
    assert (cfg.id, cfg.dataset, cfg.status, cfg.owner) == (
        "books-toscrape",
        "product",
        "draft",
        "agent",
    )


def test_instruction_file_without_apply_writes_no_source(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    assert generate(tmp_path, "books-toscrape", "--instruction", BOOKS).exit_code == 0
    assert calls[0]["source_yaml"] is None and calls[0]["apply"] is False


@pytest.mark.parametrize(
    "args",
    [
        ("--instruction", BOOKS, "--entry", "https://books.toscrape.com/"),
        ("--instruction", BOOKS, "--instruction-text", "every book on the page"),
        ("other-id", "--instruction", BOOKS),
        ("--instruction-text", "every book on the page"),
        ("some-id",),
        ("city-x", "--instruction-text", "All meetings", "--entry", "http://localhost/", "--apply"),
    ],
    ids=[
        "entry-and-file",
        "text-and-file",
        "id-mismatch",
        "no-source-id",
        "no-instruction",
        "local-entry",
    ],
)
def test_conflicting_or_missing_arguments_are_refused(
    tmp_path: Path, calls: list[dict[str, Any]], args: tuple[str | Path, ...]
) -> None:
    assert generate(tmp_path, *args).exit_code != 0
    assert calls == []


def test_apply_waits_for_a_new_dataset_type_to_be_approved(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    file = tmp_path / "new.yaml"
    file.write_text(
        "name: Shelf watch\nstart_urls: [https://shop.example.com/]\n"
        "collect: Every book with link and title\nusage: {purpose: Demo}\n"
        "fields: {url: {type: url, key: true}, title: text}\n"
    )
    result = generate(tmp_path, "--instruction", file, "--apply")
    assert result.exit_code != 0 and "instruction apply" in result.output
    assert calls == []
    assert generate(tmp_path, "--instruction", file).exit_code == 0
    assert calls[0]["source_id"] == "shelf-watch"


def test_free_text_flags_still_build_a_meeting_draft(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    result = generate(
        tmp_path,
        "city-x",
        "--instruction-text",
        "All council meetings with agendas",
        "--entry",
        "https://www.example.gov/meetings",
        "--apply",
    )
    assert result.exit_code == 0, result.output
    call = calls[0]
    assert call["source_id"] == "city-x"
    assert call["instruction"] == "All council meetings with agendas"
    cfg = source_of(call)
    assert cfg.schema_ == "Meeting" and cfg.dataset == "meeting" and cfg.owner == "agent"
    assert cfg.jurisdiction is not None and cfg.hosts == ["www.example.gov"]


def test_free_text_apply_still_needs_an_entry(tmp_path: Path, calls: list[dict[str, Any]]) -> None:
    result = generate(tmp_path, "city-x", "--instruction-text", "All meetings", "--apply")
    assert result.exit_code != 0 and calls == []
