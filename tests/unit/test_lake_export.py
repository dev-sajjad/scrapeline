"""Lake exporter pieces that need no database: paths, export ids, state, status, CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

from scrapeline import settings as settings_module
from scrapeline.analytics.export import (
    ParquetLakeExporter,
    TableState,
    lake_status,
    load_state,
    new_export_id,
    next_recent,
    partition_date,
    render_path,
    save_state,
)
from scrapeline.analytics.layout import LAKE_LAYOUT_VERSION, OVERLAP, STATE_PATH, TABLES
from scrapeline.catalog.datasets import default_registry
from scrapeline.cli import app
from scrapeline.errors import ConfigError
from scrapeline.settings import Settings

EID = "20261124T101500000000Z-a1b2c3"
NEW_YORK = ZoneInfo("America/New_York")


def test_record_versions_are_partitioned_by_dataset_and_the_utc_date_of_valid_from() -> None:
    row = {"dataset": "product", "valid_from": datetime(2026, 11, 20, 23, 30, tzinfo=NEW_YORK)}
    assert (
        render_path(TABLES["record_versions"], EID, row)
        == f"record_versions/dataset=product/date=2026-11-21/{EID}.parquet"
    )


def test_the_date_partition_follows_partition_time_not_the_watermark() -> None:
    row = {
        "started_at": datetime(2026, 11, 20, 23, 59, tzinfo=UTC),
        "finished_at": datetime(2026, 11, 21, 0, 1, tzinfo=UTC),
    }
    assert (
        render_path(TABLES["agent_runs"], EID, row) == f"agent_runs/date=2026-11-20/{EID}.parquet"
    )


def test_snapshot_tables_have_one_fixed_path() -> None:
    assert render_path(TABLES["sources"], EID, {}) == "snapshots/sources.parquet"


@pytest.mark.parametrize("dataset", ["../etc", "a/b", "", "prod uct"])
def test_unsafe_partition_values_are_refused(dataset: str) -> None:
    row = {"dataset": dataset, "valid_from": datetime(2026, 11, 20, tzinfo=UTC)}
    with pytest.raises(ConfigError, match="partition value"):
        render_path(TABLES["record_versions"], EID, row)


def test_a_missing_partition_time_is_refused() -> None:
    with pytest.raises(ConfigError, match="finished_at"):
        render_path(TABLES["runs"], EID, {"finished_at": None})


def test_naive_partition_times_are_read_as_utc() -> None:
    assert partition_date(datetime(2026, 11, 20, 23, 30)).isoformat() == "2026-11-20"


def test_rendered_paths_match_the_reader_globs(tmp_path: Path) -> None:
    when = datetime(2026, 11, 20, 12, tzinfo=UTC)
    for table in TABLES.values():
        row: dict[str, Any] = {"dataset": "product"}
        if table.partition_time:
            row[table.partition_time] = when
        path = tmp_path / render_path(table, EID, row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        assert list(tmp_path.glob(table.glob)) == [path], table.name
        path.unlink()


def test_export_ids_are_unique_and_sort_by_time() -> None:
    first = datetime(2026, 11, 24, 9, 59, 59, 999999, tzinfo=UTC)
    ids = [new_export_id(first), new_export_id(first), new_export_id(first + timedelta(hours=1))]
    assert len(set(ids)) == 3
    assert ids[0][:23] == "20261124T095959999999Z-" and ids[2] > ids[0]
    assert new_export_id(first.astimezone(NEW_YORK))[:22] == ids[0][:22]


def test_state_round_trips_and_is_written_atomically(tmp_path: Path) -> None:
    state = {
        "runs": TableState(
            watermark=datetime(2026, 11, 24, 10, tzinfo=UTC),
            recent={"r-1": datetime(2026, 11, 24, 9, 30, tzinfo=UTC)},
            export_id=EID,
            exported_at=datetime(2026, 11, 24, 10, 15, tzinfo=UTC),
            rows=4,
        ),
        "sources": TableState(export_id=EID, rows=2),
    }
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    stored = json.loads((tmp_path / STATE_PATH).read_text())
    assert stored["layout_version"] == LAKE_LAYOUT_VERSION
    assert stored["tables"]["runs"]["watermark"] == "2026-11-24T10:00:00+00:00"
    assert [p.name for p in (tmp_path / STATE_PATH).parent.iterdir()] == ["exports.json"]


def test_a_new_lake_has_no_state(tmp_path: Path) -> None:
    assert load_state(tmp_path) == {}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "unreadable"),
        (json.dumps({"layout_version": 99, "tables": {}}), "layout version"),
        (json.dumps({"layout_version": LAKE_LAYOUT_VERSION, "tables": {"runs": {}}}), "malformed"),
    ],
    ids=["corrupt", "other-layout", "malformed"],
)
def test_unusable_state_is_a_clean_error(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / STATE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(ConfigError, match=message):
        load_state(tmp_path)


def test_recent_keys_are_those_within_the_overlap_of_the_watermark() -> None:
    watermark = datetime(2026, 11, 24, 12, tzinfo=UTC)
    previous = {
        "old": watermark - OVERLAP,  # exactly at the horizon: never read again
        "kept": watermark - OVERLAP + timedelta(seconds=1),
    }
    fresh = {"new": watermark, "late": watermark - timedelta(minutes=30)}
    assert next_recent(previous, fresh, watermark) == {
        "kept": previous["kept"],
        "new": watermark,
        "late": fresh["late"],
    }


def test_unknown_tables_are_refused_before_the_database_is_touched(tmp_path: Path) -> None:
    unreachable = create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")
    exporter = ParquetLakeExporter(unreachable, tmp_path, default_registry())
    with pytest.raises(ConfigError, match="unknown lake table"):
        exporter.export(tables=["runs", "spaceships"])
    assert not (tmp_path / STATE_PATH).exists()


def test_status_counts_files_and_rows_on_disk(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs" / "date=2026-11-24"
    runs_dir.mkdir(parents=True)
    with duckdb.connect() as duck:
        for name, count in (("a", 2), ("b", 3)):
            target = runs_dir / f"{name}.parquet"
            duck.execute(f"COPY (SELECT range AS n FROM range({count})) TO '{target}'")
    (runs_dir / "c.parquet.tmp").write_bytes(b"half-written")
    save_state(tmp_path, {"runs": TableState(watermark=datetime(2026, 11, 24, tzinfo=UTC))})

    status = lake_status(tmp_path)

    assert status["layout_version"] == LAKE_LAYOUT_VERSION
    runs = status["tables"]["runs"]
    assert (runs["files"], runs["rows"], runs["recent_keys"]) == (2, 5, 0)
    assert runs["watermark"] == "2026-11-24T00:00:00+00:00"
    assert status["tables"]["sources"] == {"mode": "snapshot", "files": 0, "rows": 0}


def _use_lake(monkeypatch: pytest.MonkeyPatch, lake: Path) -> None:
    monkeypatch.setattr(
        settings_module, "get_settings", lambda: Settings(_env_file=None, lake_dir=lake)
    )


def test_cli_status_on_an_empty_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_lake(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["lake", "status"])
    assert result.exit_code == 0, result.output
    tables = json.loads(result.stdout)["tables"]
    assert set(tables) == set(TABLES)
    assert all(entry["files"] == 0 for entry in tables.values())


def test_cli_refuses_unknown_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_lake(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["lake", "export", "--table", "spaceships"])
    assert result.exit_code == 2
    assert "unknown table" in result.output


def test_cli_reports_unusable_state_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_lake(monkeypatch, tmp_path)
    (tmp_path / STATE_PATH).parent.mkdir(parents=True)
    (tmp_path / STATE_PATH).write_text("{not json")
    result = CliRunner().invoke(app, ["lake", "status"])
    assert result.exit_code == 1
    assert "unreadable lake state" in result.output and "Traceback" not in result.output
