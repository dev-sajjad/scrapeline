"""`scrapeline dataset sync` records the registry in catalog.dataset_type."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from scrapeline.catalog.datasets import default_registry
from scrapeline.cli import app
from scrapeline.store import db
from scrapeline.store import tables as t

pytestmark = pytest.mark.integration


def test_dataset_sync_is_idempotent_and_refuses_redefinitions(
    clean_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "get_engine", lambda url=None: clean_db)
    known = len(list(default_registry()))
    first = CliRunner().invoke(app, ["dataset", "sync"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output) == {"added": known, "known": known}
    assert json.loads(CliRunner().invoke(app, ["dataset", "sync"]).output)["added"] == 0
    with clean_db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.dataset_type)).scalar() == known

    with clean_db.begin() as conn:
        conn.execute(
            t.dataset_type.update()
            .where(t.dataset_type.c.name == "product")
            .values(definition_hash="0" * 64)
        )
    refused = CliRunner().invoke(app, ["dataset", "sync"])
    assert refused.exit_code == 1 and "bump its version" in refused.output
