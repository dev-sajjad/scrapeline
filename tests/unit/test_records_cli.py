"""`scrapeline dataset list/show` read the schema registry; no database needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scrapeline.catalog import datasets
from scrapeline.catalog.datasets import DatasetRegistry, default_registry
from scrapeline.cli import app


def test_dataset_list_names_every_registered_type() -> None:
    result = CliRunner().invoke(app, ["dataset", "list"])
    assert result.exit_code == 0, result.output
    listed = {(d["name"], d["version"]): d for d in json.loads(result.output)}
    assert set(listed) == {(ds.name, ds.version) for ds in default_registry()}
    assert listed[("product", 1)]["storage"] == "generic"
    assert listed[("product", 1)]["key_fields"] == ["url"]
    assert listed[("meeting", 1)]["storage"] == "typed"


def test_dataset_show_accepts_typed_class_names_and_lists_personal_data() -> None:
    article = json.loads(CliRunner().invoke(app, ["dataset", "show", "article"]).output)
    assert article["pii_fields"] == ["author"] and article["fields"]["url"]["key"] is True
    assert article["definition_hash"] == default_registry().get("article").definition_hash
    meeting = CliRunner().invoke(app, ["dataset", "show", "Meeting", "--version", "1"])
    assert meeting.exit_code == 0 and json.loads(meeting.output)["name"] == "meeting"


def test_dataset_show_refuses_unknown_names() -> None:
    result = CliRunner().invoke(app, ["dataset", "show", "spaceship"])
    assert result.exit_code == 2
    assert "unknown dataset" in result.output


@pytest.mark.parametrize(
    "command",
    [["dataset", "list"], ["dataset", "show", "product"], ["dataset", "sync"]],
    ids=["list", "show", "sync"],
)
def test_a_malformed_user_dataset_file_is_a_clean_error(
    command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "gadget.yaml").write_text(
        "name: gadget\ndescription: test\nfields:\n  sku: {type: spaceship, key: true}\n"
    )
    monkeypatch.setattr(datasets, "default_registry", lambda: DatasetRegistry.load([tmp_path]))
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # not an unhandled DatasetError
    assert "gadget.yaml" in result.output and "Traceback" not in result.output
