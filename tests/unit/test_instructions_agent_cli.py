"""`scrapeline agent generate --instruction <file>`, with the generator stubbed out (no LLM)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from scrapeline.agents.generator import SKELETON
from scrapeline.catalog.datasets import default_registry
from scrapeline.cli import app
from scrapeline.registry.config import SourceConfig
from scrapeline.registry.instruction import GENERATOR_DATASETS

ROOT = Path(__file__).resolve().parents[2]
BOOKS = ROOT / "config/instructions/books-toscrape.yaml"
MEETINGS = """
id: {id}
name: City hall meetings
start_urls: [https://www.example.gov/meetings]
collect: "Every council meeting with its date, title, location and agenda link"
dataset: meeting
usage: {{purpose: Civic meeting archive}}
"""


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


def said(result: Result) -> str:
    """The CLI output as one line: rich wraps error boxes at the terminal width."""
    return " ".join(result.output.replace("│", " ").split())


def source_of(call: dict[str, Any]) -> SourceConfig:
    return SourceConfig.model_validate(yaml.safe_load(call["source_yaml"]))


def meetings(tmp_path: Path, source_id: str = "city-hall") -> Path:
    path = tmp_path / f"{source_id}.yaml"
    path.write_text(MEETINGS.format(id=source_id))
    return path


def test_instruction_file_builds_the_draft_source(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    result = generate(tmp_path, "--instruction", meetings(tmp_path), "--apply")
    assert result.exit_code == 0, result.output
    call = calls[0]
    assert call["source_id"] == "city-hall" and call["apply"] is True
    assert call["instruction"].startswith("Every council meeting")
    assert "Dataset type: meeting. Fields to extract: " in call["instruction"]
    cfg = source_of(call)
    assert (cfg.id, cfg.dataset, cfg.schema_, cfg.status, cfg.owner) == (
        "city-hall",
        "meeting",
        "Meeting",
        "draft",
        "agent",
    )


def test_instruction_file_without_apply_writes_no_source(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    assert generate(tmp_path, "city-hall", "--instruction", meetings(tmp_path)).exit_code == 0
    assert calls[0]["source_yaml"] is None and calls[0]["apply"] is False


@pytest.mark.parametrize("apply", [False, True])
def test_datasets_the_generator_cannot_write_are_refused_before_it_runs(
    tmp_path: Path, calls: list[dict[str, Any]], apply: bool
) -> None:
    # The generator only writes Meeting specs; a product spec would fail every paid attempt.
    result = generate(tmp_path, "--instruction", BOOKS, *(["--apply"] if apply else []))
    assert result.exit_code != 0 and calls == []
    assert "writes only meeting specs" in said(result)
    assert "scrapeline instruction apply" in said(result)


def test_generator_datasets_match_the_generator_skeleton() -> None:
    schema = yaml.safe_load(SKELETON)["schema"]
    assert GENERATOR_DATASETS == {default_registry().canonical_name(schema)}


def test_apply_refuses_a_site_dir_used_by_another_source(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    # config/sources/fixture-static-list.yaml owns adapters/sites/fixture_static_list/.
    file = meetings(tmp_path, "fixture_static_list")
    result = generate(tmp_path, "--instruction", file, "--apply")
    assert result.exit_code != 0 and "'fixture-static-list' already uses" in said(result)
    assert calls == []
    assert generate(tmp_path, "--instruction", file).exit_code == 0  # nothing is written


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


def test_new_dataset_types_are_never_generated_or_committed(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    file = tmp_path / "new.yaml"
    file.write_text(
        "name: Shelf watch\nstart_urls: [https://shop.example.com/]\n"
        "collect: Every book with link and title\nusage: {purpose: Demo}\n"
        "fields: {url: {type: url, key: true}, title: text}\n"
    )
    for extra in (["--apply"], []):
        result = generate(tmp_path, "--instruction", file, *extra)
        assert result.exit_code != 0 and "scrapeline instruction apply" in said(result)
    assert calls == []


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
