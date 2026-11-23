"""compile_instruction: instruction -> draft source config, dataset type and spec skeleton."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scrapeline.catalog.datasets import DatasetRegistry, DatasetType, load_dataset_file
from scrapeline.errors import ConfigError
from scrapeline.registry.config import SourceConfig
from scrapeline.registry.instruction import (
    DECLARATIVE_ADAPTER,
    UNREVIEWED_TERMS,
    CompiledInstruction,
    ScrapeInstruction,
    compile_instruction,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = DatasetRegistry.load(())

BASE = """
name: Books catalogue
start_urls: [https://books.toscrape.com/]
collect: "Every book: title, price, stock status, rating, link"
usage: {purpose: Price tracking demo}
"""


def compile_yaml(extra: str = "", registry: DatasetRegistry = REGISTRY) -> CompiledInstruction:
    return compile_instruction(ScrapeInstruction.from_yaml(BASE + extra), registry)


def spec_of(compiled: CompiledInstruction) -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(compiled.spec_skeleton)
    return spec


def test_existing_dataset_compiles_to_a_draft_declarative_source() -> None:
    compiled = compile_yaml("dataset: product\n")
    src = compiled.source
    assert (src.id, src.name, src.platform, src.kind) == (
        "books-catalogue",
        "Books catalogue",
        "custom",
        "html",
    )
    assert src.entry == "https://books.toscrape.com/" and src.hosts == ["books.toscrape.com"]
    assert (src.status, src.owner, src.adapter) == ("draft", "instruction", DECLARATIVE_ADAPTER)
    assert src.dataset == "product" and src.dataset_name == "product" and src.schema_ == "product"
    assert src.cadence == "daily" and src.pagination == "next-link" and src.max_pages_per_run == 50
    assert src.legal.terms_checked == UNREVIEWED_TERMS and "TODO(human)" in src.legal.notes
    assert src.usage.purpose == "Price tracking demo"
    assert src.instruction is not None and src.instruction.collect.startswith("Every book")
    assert src.instruction.schema_ == "product" and src.instruction.fields_excluded == []
    assert src.quality.max_null_rate == {"url": 0.0, "title": 0.0}
    assert compiled.dataset_type is REGISTRY.get("product") and not compiled.dataset_is_new
    assert not compiled.needs_dataset_proposal
    assert compiled.fields == tuple(REGISTRY.get("product").fields)


def test_spec_skeleton_has_todo_selectors_and_type_transforms() -> None:
    spec = spec_of(compile_yaml("dataset: product\n"))
    assert spec["schema"] == "product" and spec["record"].startswith("TODO")
    fields = spec["fields"]
    assert all(f["css"].startswith("TODO") for f in fields.values())
    assert fields["url"] == {
        "css": "TODO: CSS selector for url",
        "attr": "href",
        "transform": "absolute_url",
    }
    assert fields["image_url"]["attr"] == "src"
    assert fields["price"]["transform"] == "money" and fields["price"]["optional"] is True
    assert fields["in_stock"]["transform"] == "bool"
    assert fields["rating"]["transform"] == "float"
    assert fields["review_count"]["transform"] == "int"
    assert "transform" not in fields["title"] and "optional" not in fields["title"]
    assert spec["next_page"]["css"].startswith("TODO") and "render" not in spec


def test_narrowed_fields_exclude_the_rest_of_the_dataset() -> None:
    compiled = compile_yaml("dataset: article\nfields: {url: url, title: text, section: text}\n")
    assert compiled.fields == ("url", "title", "section")
    assert compiled.source.instruction is not None
    excluded = compiled.source.instruction.fields_excluded
    assert "author" in excluded and "section" not in excluded
    assert set(spec_of(compiled)["fields"]) == {"url", "title", "section"}
    assert compiled.personal_data_fields == ()


def test_narrowing_must_keep_key_and_required_fields() -> None:
    with pytest.raises(ConfigError, match=r"key and required fields.*title"):
        compile_yaml("dataset: product\nfields: {url: url, price: money}\n")


def test_new_named_dataset_type_is_built_from_the_fields() -> None:
    compiled = compile_yaml(
        "dataset: book_listing\n"
        "fields:\n"
        "  link: {type: url, key: true}\n"
        "  title: {type: text, required: true}\n"
        "  price: money\n"
        "  format: {type: enum, values: [paperback, hardback]}\n"
    )
    ds = compiled.dataset_type
    assert ds is not None and compiled.dataset_is_new
    assert (ds.name, ds.version, ds.pack, ds.storage) == ("book_listing", 1, "user", "generic")
    assert ds.key_fields == ("link",) and ds.canonical_url_field == "link"
    assert compiled.source.dataset == "book_listing"
    assert any("Approve the new dataset type 'book_listing'" in s for s in compiled.next_steps)
    assert spec_of(compiled)["fields"]["format"] == {
        "css": "TODO: CSS selector for format",
        "optional": True,
    }


def test_dataset_yaml_round_trips(tmp_path: Path) -> None:
    compiled = compile_yaml("fields: {url: {type: url, key: true}, title: text, price: money}\n")
    text = compiled.dataset_yaml()
    assert text is not None
    path = tmp_path / "books_catalogue.yaml"
    path.write_text(text)
    loaded = load_dataset_file(path)
    assert compiled.dataset_type is not None
    assert loaded.definition_hash == compiled.dataset_type.definition_hash
    assert loaded.pack == "user" and loaded.canonical_url_field == "url"


def test_unnamed_new_dataset_takes_the_source_id() -> None:
    compiled = compile_yaml("id: 9-lives\nfields: {url: {type: url, key: true}}\n")
    assert compiled.dataset_type is not None and compiled.dataset_type.name == "d_9_lives"
    compiled = compile_yaml("fields: {url: {type: url, key: true}}\n")
    assert compiled.source.dataset == "books_catalogue" and compiled.dataset_is_new


def test_derived_name_reuses_an_identical_type_and_refuses_a_different_one() -> None:
    first = compile_yaml("fields: {url: {type: url, key: true}, title: text}\n")
    assert first.dataset_type is not None
    registry = DatasetRegistry([*REGISTRY, first.dataset_type])
    again = compile_yaml("fields: {url: {type: url, key: true}, title: text}\n", registry)
    assert again.dataset_type == first.dataset_type and not again.dataset_is_new
    assert again.dataset_yaml() is None
    with pytest.raises(ConfigError, match="different definition"):
        compile_yaml("fields: {url: {type: url, key: true}, price: money}\n", registry)


def test_proposal_needed_returns_no_dataset_type() -> None:
    compiled = compile_yaml()
    assert compiled.needs_dataset_proposal and compiled.dataset_type is None
    assert not compiled.dataset_is_new and compiled.dataset_yaml() is None
    assert compiled.fields == () and spec_of(compiled)["fields"] == {}
    assert compiled.source.dataset == "books_catalogue"
    assert any("dataset type must be proposed" in s for s in compiled.next_steps)
    assert compiled.summary()["dataset_proposal_needed"] is True


def test_personal_data_is_kept_only_with_permission_and_flagged() -> None:
    with pytest.raises(ConfigError, match="personal data"):
        compile_yaml("dataset: article\n")
    allowed = BASE.replace(
        "usage: {purpose: Price tracking demo}",
        "usage: {purpose: Media monitoring, allow_pii: true}",
    )
    compiled = compile_instruction(
        ScrapeInstruction.from_yaml(allowed + "dataset: article\n"), REGISTRY
    )
    assert compiled.personal_data_fields == ("author",)
    step = next(s for s in compiled.next_steps if "Personal-data" in s)
    assert "author" in step and "retention_days" in step


def test_personal_data_is_rechecked_against_the_given_registry() -> None:
    listing = DatasetType(
        name="listing",
        description="A listing with a contact person",
        pack="user",
        fields={"url": {"type": "url", "key": True}, "contact": {"type": "text", "pii": True}},
    )
    registry = DatasetRegistry([*REGISTRY, listing])
    # The default registry has no `listing`, so the instruction itself validates.
    extra = "dataset: listing\nfields: {url: {type: url, key: true}, contact: text}\n"
    with pytest.raises(ConfigError, match="personal data"):
        compile_yaml(extra, registry)
    narrowed = "dataset: listing\nfields: {url: {type: url, key: true}}\n"
    assert compile_yaml(narrowed, registry).fields == ("url",)


def test_unknown_dataset_without_fields_is_refused() -> None:
    registry = DatasetRegistry(ds for ds in REGISTRY if ds.name != "product")
    with pytest.raises(ConfigError, match="define its fields"):
        compile_yaml("dataset: product\n", registry)


def test_render_always_compiles_to_a_js_source_with_a_render_block() -> None:
    compiled = compile_yaml("dataset: product\ncrawl: {render: always, pagination: load-more}\n")
    assert compiled.source.kind == "js" and compiled.source.pagination == "load-more"
    spec = spec_of(compiled)
    assert spec["render"]["wait_for"].startswith("TODO")
    assert spec["render"]["click_until_gone"].startswith("TODO")
    assert "next_page" not in spec
    plain = compile_yaml("dataset: product\ncrawl: {pagination: load-more}\n")
    assert plain.source.kind == "html" and "render" not in spec_of(plain)
    assert any("needs a browser" in s for s in plain.next_steps)


@pytest.mark.parametrize(
    ("requested", "compiled", "has_next_page"),
    [
        ("auto", "next-link", True),
        ("next-link", "next-link", True),
        ("none", "none", False),
        ("page-number", "page-number", False),
        ("cursor", "cursor", False),
    ],
)
def test_pagination_is_mapped(requested: str, compiled: str, has_next_page: bool) -> None:
    result = compile_yaml(f"dataset: product\ncrawl: {{pagination: {requested}}}\n")
    assert result.source.pagination == compiled
    assert result.source.instruction is not None
    assert result.source.instruction.pagination == compiled
    assert ("next_page" in spec_of(result)) is has_next_page


def test_manual_schedule_is_kept_and_explained() -> None:
    compiled = compile_yaml("dataset: product\nschedule: manual\n")
    assert compiled.source.cadence == "manual"
    assert any("never enqueues" in s for s in compiled.next_steps)


def test_typed_dataset_keeps_the_model_name_and_needs_a_jurisdiction() -> None:
    compiled = compile_yaml("dataset: meeting\n")
    src = compiled.source
    assert src.schema_ == "Meeting" and src.dataset == "meeting"
    assert src.jurisdiction is not None and src.jurisdiction.id == "xx-unknown"
    assert any("jurisdiction" in s for s in compiled.next_steps)
    fields = spec_of(compiled)["fields"]
    assert fields["meeting_date"]["transform"] == "date" and "optional" not in fields["title"]
    assert fields["agenda_url"]["transform"] == "absolute_url"


def test_locale_usage_and_crawl_options_are_carried_over() -> None:
    text = BASE.replace(
        "start_urls: [https://books.toscrape.com/]",
        "start_urls: [https://a.example.com/, https://b.example.com/x]\n"
        "allowed_hosts: [img.example.com]",
    )
    extra = (
        "dataset: product\n"
        "crawl: {follow_detail_pages: true, max_pages: 7}\n"
        "documents: link-only\n"
        "locale: {language: en-GB, timezone: Europe/London, currency: GBP}\n"
    )
    compiled = compile_instruction(ScrapeInstruction.from_yaml(text + extra), REGISTRY)
    src = compiled.source
    assert src.entry == "https://a.example.com/"
    assert src.hosts == ["a.example.com", "b.example.com", "img.example.com"]
    assert src.options == {
        "extra_start_urls": ["https://b.example.com/x"],
        "follow_detail_pages": True,
    }
    assert src.max_pages_per_run == 7 and src.locale.currency == "GBP"
    assert src.instruction is not None and src.instruction.documents == "link-only"
    steps = " ".join(compiled.next_steps)
    assert "extra_start_urls" in steps and "detail pages" in steps and "link-only" in steps


def test_source_yaml_round_trips_and_ends_with_activation() -> None:
    compiled = compile_yaml("dataset: product\nlocale: {language: en-GB, currency: GBP}\n")
    loaded = SourceConfig.model_validate(yaml.safe_load(compiled.source_yaml()))
    assert loaded == compiled.source
    assert compiled.next_steps[-1].startswith("Activate:")


def test_text_with_quotes_and_newlines_cannot_inject_keys() -> None:
    collect = 'Every "book"\nstatus: active\nadapter: evil.module:Adapter\n'
    text = BASE.replace(
        'collect: "Every book: title, price, stock status, rating, link"',
        f"collect: {yaml.safe_dump(collect).strip()}",
    )
    compiled = compile_instruction(
        ScrapeInstruction.from_yaml(text + "dataset: product\n"), REGISTRY
    )
    loaded = SourceConfig.model_validate(yaml.safe_load(compiled.source_yaml()))
    assert loaded.status == "draft" and loaded.adapter == DECLARATIVE_ADAPTER
    assert loaded.instruction is not None and loaded.instruction.collect == collect


def test_environment_references_are_refused() -> None:
    text = BASE.replace("Every book:", "Every ${ANTHROPIC_API_KEY} book:")
    with pytest.raises(ConfigError, match=r"\$\{NAME\}"):
        compile_instruction(ScrapeInstruction.from_yaml(text + "dataset: product\n"), REGISTRY)


@pytest.mark.parametrize("path", sorted((ROOT / "config/instructions").glob("*.yaml")))
def test_example_instructions_compile(path: Path) -> None:
    compiled = compile_instruction(ScrapeInstruction.from_yaml(path.read_text()), REGISTRY)
    assert compiled.source.id == path.stem
    assert not compiled.dataset_is_new and not compiled.needs_dataset_proposal
    assert compiled.personal_data_fields == ()


def test_news_example_narrows_away_the_author() -> None:
    path = ROOT / "config/instructions/example-news.yaml"
    compiled = compile_instruction(ScrapeInstruction.from_yaml(path.read_text()), REGISTRY)
    assert compiled.source.dataset == "article" and "author" not in compiled.fields
    assert compiled.source.instruction is not None
    assert "author" in compiled.source.instruction.fields_excluded
