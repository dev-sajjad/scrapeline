from __future__ import annotations

import pytest
from sqlalchemy import Engine, select

from scrapeline.adapters.declarative import spec_path_for
from scrapeline.agents.repairer import repair_case
from scrapeline.agents.tester import evaluate_spec
from scrapeline.ops.runs import sync_source
from scrapeline.registry import load_sources
from scrapeline.settings import PROJECT_ROOT, Settings
from scrapeline.store import tables as t
from tests.unit.test_agents import ScriptedDriver

pytestmark = pytest.mark.integration
DRIFT = PROJECT_ROOT / "tests/sites/drift"
SOURCE = "fixture-static-list"


def test_exhausted_repair_marks_source_broken_and_logs_runs(clean_db: Engine) -> None:
    cfg = load_sources(PROJECT_ROOT / "config/sources")["fixture-static-list"]
    with clean_db.begin() as conn:
        sync_source(conn, cfg)
    driver = ScriptedDriver(lambda s: s, run_tests=True)
    res = repair_case(
        cfg.id,
        DRIFT / "06-table-to-cards",
        driver=driver,
        settings=Settings(_env_file=None),
        engine=clean_db,
        sandbox_mode="local",
    )
    assert not res.passed and res.attempts == 3
    with clean_db.connect() as conn:
        runs = conn.execute(select(t.agent_run)).all()
        calls = conn.execute(select(t.agent_tool_call)).all()
        status = conn.execute(select(t.sources.c.status)).scalar_one()
        inc = conn.execute(select(t.incident)).one()
    assert len(runs) == 3 and all(
        r.prompt_version == "repairer.v1" and r.cost_usd > 0 for r in runs
    )
    assert len(calls) == 12 and {c.tool for c in calls} >= {"run_tester", "write_adapter"}
    assert status == "broken" and inc.kind == "repair_exhausted"


def test_tester_in_docker_sandbox_matches_local() -> None:
    settings = Settings(_env_file=None)
    spec = spec_path_for(SOURCE).read_text()
    golden = PROJECT_ROOT / "tests/fixtures/golden/static-list.json"
    for case in ("01-title-class-renamed", "09-cosmetic-only"):
        local, docker = (
            evaluate_spec(spec, DRIFT / case, golden, settings, mode=mode, source_id=SOURCE)
            for mode in ("local", "docker")
        )
        assert local.locale_origin == docker.locale_origin == "source"
        assert (local.passed, local.records, local.golden["field_accuracy"]) == (
            docker.passed,
            docker.records,
            docker.golden["field_accuracy"],
        )
