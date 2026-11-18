"""Golden comparison: parsed records vs human-owned expected rows (tests/fixtures/golden).

Pure and deterministic. Used by `scrapeline verify`, trusted tests and the agent tester.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scrapeline.pipeline.models import Record


@dataclass
class GoldenReport:
    expected: int
    actual: int
    matched: int
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    field_diffs: list[dict[str, Any]] = field(default_factory=list)
    field_accuracy: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.missing and not self.unexpected and not self.field_diffs

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"passed": self.passed}


def load_golden(path: Path, base_url: str) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    records: list[dict[str, Any]] = data["records"]
    return [
        {
            k: v.replace("{FIXTURE_BASE_URL}", base_url) if isinstance(v, str) else v
            for k, v in rec.items()
        }
        for rec in records
    ]


def compare(
    records: list[Record], golden: list[dict[str, Any]], *, max_diffs: int = 50
) -> GoldenReport:
    """Compare only the fields golden declares. Identity = external_id."""
    actual = {
        r.external_id: r.model_dump(mode="json") | {"external_id": r.external_id} for r in records
    }
    expected = {g["external_id"]: g for g in golden}
    report = GoldenReport(expected=len(expected), actual=len(actual), matched=0)
    report.missing = sorted(set(expected) - set(actual))
    report.unexpected = sorted(set(actual) - set(expected))
    checked = correct = 0
    for ext_id in sorted(set(expected) & set(actual)):
        ok = True
        for name, want in expected[ext_id].items():
            got = actual[ext_id].get(name)
            checked += 1
            if got == want:
                correct += 1
            else:
                ok = False
                if len(report.field_diffs) < max_diffs:
                    report.field_diffs.append(
                        {"external_id": ext_id, "field": name, "expected": want, "actual": got}
                    )
        report.matched += int(ok)
    total_fields = checked + sum(len(expected[e]) for e in report.missing)
    report.field_accuracy = round(correct / total_fields, 4) if total_fields else 0.0
    return report
