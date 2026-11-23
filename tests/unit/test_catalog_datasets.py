from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from scrapeline.catalog.datasets import (
    DatasetRegistry,
    DatasetType,
    FieldDef,
    default_registry,
    load_dataset_file,
    resolve_model,
)
from scrapeline.errors import DatasetError
from scrapeline.pipeline.identity import content_hash
from scrapeline.pipeline.models import Meeting

BOOK = {
    "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
    "title": "  A Light in the   Attic ",
    "price": {"amount": "51.77", "currency": "GBP"},
    "in_stock": True,
}


def test_builtin_packs_and_typed_civic_types_are_registered() -> None:
    names = default_registry().names()
    assert {"product", "article", "job_posting"} <= set(names)
    assert {"meeting", "body", "document", "agenda_item"} <= set(names)


def test_typed_lookup_accepts_class_and_dataset_names() -> None:
    assert resolve_model("Meeting") is Meeting
    assert resolve_model("meeting") is Meeting
    assert default_registry().get("Meeting").storage == "typed"


def test_generic_record_identity_cleaning_and_money() -> None:
    product = resolve_model("product").model_validate(BOOK)
    assert product.record_type == "product" and product.schema_version == 1
    assert product.external_id == BOOK["url"] == product.canonical_url
    assert product.title == "A Light in the Attic"  # type: ignore[attr-defined]
    dumped = product.model_dump(mode="json")
    assert dumped["price"] == {"amount": "51.77", "currency": "GBP"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"url": None}, "key field(s) missing"),
        ({"url": "ftp://example.org/x"}, "http(s)"),
        ({"title": None}, "title"),
        ({"colour": "red"}, "Extra inputs are not permitted"),
        ({"price": {"amount": "1", "currency": "pounds"}}, "currency"),
    ],
)
def test_generic_record_rejects_with_a_readable_reason(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message.replace("(", r"\(").replace(")", r"\)")):
        resolve_model("product").model_validate({**BOOK, **change})


def test_composite_key_is_a_stable_hash() -> None:
    ds = DatasetType(
        name="price_point",
        description="test",
        fields={
            "shop": FieldDef(type="text", key=True),
            "sku": FieldDef(type="text", key=True),
            "price": FieldDef(type="money"),
        },
    )
    first = ds.external_id({"shop": "a", "sku": "1"})
    assert first is not None and first.startswith("k:")
    assert first == ds.external_id({"shop": " a ", "sku": "1"})
    assert first != ds.external_id({"shop": "a", "sku": "2"})
    assert ds.external_id({"shop": "a"}) is None


def test_key_implies_required_and_enum_needs_values() -> None:
    assert FieldDef.model_validate({"type": "url", "key": True}).required
    assert FieldDef.model_validate("money").type == "money"
    with pytest.raises(ValidationError, match="values"):
        FieldDef(type="enum")


def test_dataset_contract_validation() -> None:
    with pytest.raises(ValidationError, match="key field"):
        DatasetType(name="nokey", description="x", fields={"a": FieldDef()})
    with pytest.raises(ValidationError, match="reserved"):
        DatasetType(
            name="bad", description="x", fields={"external_id": FieldDef(type="text", key=True)}
        )
    with pytest.raises(ValidationError, match="canonical_url_field"):
        DatasetType(
            name="bad",
            description="x",
            canonical_url_field="title",
            fields={"title": FieldDef(type="text", key=True)},
        )


def test_redefining_a_version_differently_is_refused() -> None:
    a = DatasetType(name="thing", description="v1", fields={"id_": FieldDef(key=True)})
    b = DatasetType(name="thing", description="v1", fields={"id_": FieldDef(type="int", key=True)})
    with pytest.raises(DatasetError, match="defined twice"):
        DatasetRegistry([a, b])
    assert DatasetRegistry([a, a]).get("thing") == a


def test_descriptions_are_not_part_of_the_contract() -> None:
    a = DatasetType(name="thing", description="v1", fields={"id_": FieldDef(key=True)})
    b = DatasetType(
        name="thing", description="typo fixed", fields={"id_": FieldDef(key=True, description="x")}
    )
    assert a.definition_hash == b.definition_hash


def test_key_values_are_coerced_before_identity() -> None:
    ds = DatasetType(
        name="t",
        description="x",
        fields={"n": FieldDef(type="int", key=True), "d": FieldDef(type="date", key=True)},
    )
    assert ds.external_id({"n": "01", "d": "2026-01-02"}) == ds.external_id(
        {"n": 1, "d": date(2026, 1, 2)}
    )
    with pytest.raises(ValueError, match="not a valid int"):
        ds.external_id({"n": "one", "d": "2026-01-02"})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"price": FieldDef(type="money", key=True)}, "must be one of"),
        ({"json": FieldDef(key=True)}, "reserved"),
        ({"hash_exclude": FieldDef(key=True)}, "reserved"),
    ],
)
def test_unsafe_key_types_and_shadowing_names_are_refused(
    fields: dict[str, FieldDef], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        DatasetType(name="bad", description="x", fields=fields)


def test_datetimes_are_stored_in_utc() -> None:
    article = resolve_model("article").model_validate(
        {"url": "https://example.org/a", "title": "T", "published_at": "2026-03-29T09:00:00+02:00"}
    )
    assert article.published_at == datetime(2026, 3, 29, 7, tzinfo=UTC)  # type: ignore[attr-defined]
    assert article.published_at.utcoffset() == timedelta(0)  # type: ignore[attr-defined]


def test_new_optional_fields_and_version_bumps_do_not_change_the_hash() -> None:
    v1 = DatasetType(name="thing", description="x", fields={"url": FieldDef(type="url", key=True)})
    v2 = DatasetType(
        name="thing",
        version=2,
        description="x",
        fields={"url": FieldDef(type="url", key=True), "colour": FieldDef()},
    )
    row = {"url": "https://example.org/1"}
    assert content_hash(v1.record_model().model_validate(row)) == content_hash(
        v2.record_model().model_validate(row)
    )


def test_latest_version_wins_unless_pinned() -> None:
    v1 = DatasetType(name="thing", description="v1", fields={"id_": FieldDef(key=True)})
    v2 = DatasetType(name="thing", version=2, description="v2", fields={"id_": FieldDef(key=True)})
    registry = DatasetRegistry([v1, v2])
    assert registry.get("thing").version == 2
    assert registry.get("thing", 1).version == 1


def test_price_formatting_does_not_create_a_new_version() -> None:
    model = resolve_model("product")
    a = model.model_validate({**BOOK, "price": {"amount": "5.00", "currency": "GBP"}})
    b = model.model_validate({**BOOK, "price": {"amount": "5.0", "currency": "GBP"}})
    c = model.model_validate({**BOOK, "price": {"amount": Decimal("5.01"), "currency": "GBP"}})
    assert content_hash(a) == content_hash(b) != content_hash(c)


def test_user_dataset_files_load_from_extra_dirs(tmp_path: Path) -> None:
    (tmp_path / "listing.yaml").write_text(
        "name: listing\ndescription: Property listing\n"
        "fields:\n  url: {type: url, key: true}\n  rent: money\n"
    )
    registry = DatasetRegistry.load([tmp_path])
    assert registry.get("listing").fields["rent"].type == "money"
    (tmp_path / "broken.yaml").write_text("name: broken\n")
    with pytest.raises(DatasetError, match=r"broken\.yaml"):
        load_dataset_file(tmp_path / "broken.yaml")
