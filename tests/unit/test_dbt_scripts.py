"""The dbt helper scripts: staging generation from the registry and the sample lake."""

from __future__ import annotations

import importlib.util
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest
import yaml

from scrapeline.analytics.layout import TABLES
from scrapeline.catalog.datasets import DatasetRegistry, DatasetType
from tests.conftest import ROOT


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


gen = load_script("gen_dbt_staging")
sample = load_script("make_sample_lake")


def listing(version: int, fields: dict[str, Any]) -> DatasetType:
    return DatasetType.model_validate(
        {"name": "listing", "version": version, "description": "A listing.", "fields": fields}
    )


LISTING_V1 = listing(
    1,
    {
        "url": {"type": "url", "key": True},
        "title": {"type": "text", "required": True},
        "price": {"type": "money", "required": True},
        "status": {"type": "enum", "values": ["open", "closed"]},
        "listed_on": "date",
    },
)
LISTING_V2 = listing(
    2,
    {
        "url": {"type": "url", "key": True},
        "title": {"type": "text", "required": True},
        "price": "money",
        "status": {"type": "enum", "values": ["open", "closed", "pending"]},
        "seen_at": "datetime",
        "version": {"type": "text", "description": "Software version"},
    },
)


def test_committed_staging_files_are_current() -> None:
    assert gen.main(["--check"]) == 0, "run: uv run python scripts/gen_dbt_staging.py"


def test_sources_describe_every_lake_table() -> None:
    doc = yaml.safe_load(gen.lake_sources_yaml(TABLES))
    tables = {t["name"]: t for t in doc["sources"][0]["tables"]}
    assert tables.keys() == TABLES.keys()
    for name, table in TABLES.items():
        entry = tables[name]
        assert [(c["name"], c["data_type"]) for c in entry["columns"]] == [
            (c.name, c.type) for c in table.columns
        ]
        location = entry["config"]["external_location"]
        assert entry["config"]["meta"]["lake_path"] == f"{gen.LAKE_DIR}/{table.glob}"
        assert location.startswith(f"read_parquet('{gen.LAKE_DIR}/{table.glob}'")
        assert ("hive_partitioning = true" in location) == (table.mode == "append")


def test_staging_columns_union_versions_and_type_every_field() -> None:
    columns = {c.name: c for c in gen.staging_columns([LISTING_V1, LISTING_V2])}

    assert list(columns) == [
        "url",
        "title",
        "price_amount",
        "price_currency",
        "status",
        "seen_at",
        "version_value",
        "listed_on",
    ]
    assert columns["price_amount"].data_type == "DECIMAL(18, 4)"
    assert columns["price_amount"].expression == (
        "cast(payload->'price'->>'amount' as DECIMAL(18, 4))"
    )
    assert columns["price_currency"].expression == "payload->'price'->>'currency'"
    assert columns["seen_at"].expression == "cast(payload->>'seen_at' as TIMESTAMPTZ)"
    assert columns["listed_on"].data_type == "DATE"
    assert columns["status"].expression == "payload->>'status'"
    assert columns["version_value"].expression == "payload->>'version'"
    # Required only when every version requires it: v2 made the price optional.
    assert columns["title"].required and columns["url"].required
    assert not columns["price_amount"].required
    assert columns["status"].values == ("closed", "open", "pending")


def test_required_personal_data_gets_no_not_null_test() -> None:
    # The record store blanks pii fields unless the source's usage allows personal data.
    people = listing(
        1,
        {
            "url": {"type": "url", "key": True},
            "name": {"type": "text", "required": True, "pii": True},
            "fee": {"type": "money", "required": True, "pii": True},
        },
    )
    columns = gen.staging_columns([people])
    entries = {c.name: gen._column_entry(c) for c in columns}
    assert "data_tests" not in entries["name"]
    assert "data_tests" not in entries["fee_amount"]
    assert "data_tests" not in entries["fee_currency"]
    assert entries["url"]["data_tests"] == [{"not_null": {"config": {"where": gen.NOT_REMOVED}}}]
    assert "pii" in entries["name"]["description"]


def test_a_type_change_does_not_fail_on_old_payloads() -> None:
    v1 = listing(1, {"url": {"type": "url", "key": True}, "stock": "text", "price": "text"})
    v2 = listing(2, {"url": {"type": "url", "key": True}, "stock": "int", "price": "money"})
    columns = {c.name: c for c in gen.staging_columns([v1, v2])}
    assert columns["stock"].expression == (
        "case when dataset_version in (1) then try_cast(payload->>'stock' as BIGINT) "
        "else cast(payload->>'stock' as BIGINT) end"
    )
    assert "versions 1 had another type" in columns["stock"].description
    assert columns["url"].expression == "payload->>'url'"

    select = ", ".join(f"{c.expression} as {c.name}" for c in columns.values())
    con = duckdb.connect()
    result = con.execute(
        f"select {select} from (values "
        """(1, '{"url": "u1", "stock": "5 left", "price": "about 3"}'::json), """
        """(2, '{"url": "u2", "stock": "7", """
        """"price": {"amount": "2.50", "currency": "GBP"}}'::json)"""
        ") as v(dataset_version, payload) order by dataset_version"
    ).fetchall()
    assert [(r[1], r[2]) for r in result] == [(None, None), (7, Decimal("2.5"))]
    # The newest type still fails loudly on a value it cannot hold.
    with pytest.raises(duckdb.ConversionException):
        con.execute(
            f"select {columns['stock'].expression} "
            """from (values (2, '{"stock": "lots"}'::json)) as v(dataset_version, payload)"""
        ).fetchall()


def test_staging_columns_refuse_clashing_names() -> None:
    clash = listing(
        1, {"url": {"type": "url", "key": True}, "price": "money", "price_amount": "decimal"}
    )
    with pytest.raises(SystemExit, match="price_amount"):
        gen.staging_columns([clash])


def test_dataset_model_selects_one_dataset_from_staged_versions() -> None:
    sql = gen.dataset_model_sql("listing", gen.staging_columns([LISTING_V1]))
    assert "from {{ ref('stg_lake__record_versions') }}" in sql
    assert sql.rstrip().endswith("where dataset = 'listing'")
    assert "    cast(payload->>'listed_on' as DATE) as listed_on" in sql


def test_render_covers_generic_datasets_only() -> None:
    files = gen.render(DatasetRegistry.load(()), TABLES)
    models = sorted(p.name for p in files if p.suffix == ".sql")
    assert models == [
        "stg_article__versions.sql",
        "stg_job_posting__versions.sql",
        "stg_product__versions.sql",
    ]
    tests = yaml.safe_load(files[gen.DATASETS_FILE])
    product = next(m for m in tests["models"] if m["name"] == "stg_product__versions")
    title = next(c for c in product["columns"] if c["name"] == "title")
    assert title["data_tests"] == [{"not_null": {"config": {"where": gen.NOT_REMOVED}}}]


def test_generator_writes_then_removes_stale_models(tmp_path: Path) -> None:
    assert gen.main(["--dbt-dir", str(tmp_path)]) == 0
    assert gen.main(["--check", "--dbt-dir", str(tmp_path)]) == 0
    stale = tmp_path / gen.DATASETS_DIR / "stg_gone__versions.sql"
    stale.write_text(f"-- {gen.GENERATED}\nselect 1\n")
    hand_written = tmp_path / gen.DATASETS_DIR / "stg_mine__versions.sql"
    hand_written.write_text("select 1\n")

    assert gen.main(["--check", "--dbt-dir", str(tmp_path)]) == 1
    assert gen.main(["--dbt-dir", str(tmp_path)]) == 0
    assert not stale.exists()
    assert hand_written.exists()


@pytest.fixture(scope="module")
def lake() -> Any:
    return sample.build(DatasetRegistry.load(()))


def test_sample_versions_follow_record_store_rules(lake: Any) -> None:
    by_record: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in lake.rows["record_versions"]:
        by_record[row["record_id"]].append(row)
    for versions in by_record.values():
        assert [v["version"] for v in versions] == list(range(1, len(versions) + 1))
        assert versions[0]["change_type"] == "insert"
        valid_from = [v["valid_from"] for v in versions]
        assert valid_from == sorted(valid_from)
        for v in versions:
            removed = v["change_type"] == "removed"
            assert (v["payload"] is None) == removed == (v["content_hash"] is None)

    blender = next(vs for vs in by_record.values() if vs[0]["external_id"].endswith("/p/blender"))
    assert [v["change_type"] for v in blender] == ["insert", "removed", "restored"]


def test_sample_run_stats_match_the_versions_they_wrote(lake: Any) -> None:
    counter = {
        "insert": "inserted",
        "update": "updated",
        "removed": "removed",
        "restored": "restored",
    }
    for run in lake.rows["runs"]:
        written: dict[str, int] = defaultdict(int)
        for v in lake.rows["record_versions"]:
            if v["run_id"] == run["run_id"]:
                written[counter[v["change_type"]]] += 1
        loaded = run["stats"]["loaded"].get("product", {})
        assert {k: n for k, n in loaded.items() if k != "unchanged" and n} == dict(written)


def test_sample_runs_have_mixed_outcomes(lake: Any) -> None:
    outcomes = {(r["source_id"], r["status"], r["error"] is not None) for r in lake.rows["runs"]}
    assert ("shop-sim", "failed", True) in outcomes  # blocked
    assert ("books-toscrape", "failed", False) in outcomes  # failed its checks
    assert ("shop-sim", "succeeded", False) in outcomes


def test_sample_lake_files_follow_the_layout(tmp_path: Path, lake: Any) -> None:
    files = sample.write(lake, tmp_path)
    con = duckdb.connect()
    for table in TABLES.values():
        matched = sorted(tmp_path.glob(table.glob))
        assert matched, table.name
        for path in matched:
            schema = con.execute(
                f"describe select * from read_parquet('{path}', hive_partitioning = false)"
            ).fetchall()
            assert [(name, type_) for name, type_, *_ in schema] == [
                (c.name, "TIMESTAMP WITH TIME ZONE" if c.type == "TIMESTAMPTZ" else c.type)
                for c in table.columns
            ]
    assert len(files) == sum(len(sorted(tmp_path.glob(t.glob))) for t in TABLES.values())

    # Overlap: the run finishing within an hour of the first export's cutoff is in both.
    runs = con.execute(
        f"select run_id, count(*) from read_parquet('{tmp_path}/runs/**/*.parquet') "
        "group by run_id having count(*) > 1"
    ).fetchall()
    assert len(runs) == 1
    assert con.execute(
        f"select count(*) from read_parquet('{tmp_path}/record_versions/**/*.parquet')"
    ).fetchone() == (len(lake.rows["record_versions"]),)


def test_sample_lake_refuses_a_directory_with_files(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("real data")
    assert sample.main([str(tmp_path)]) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]
