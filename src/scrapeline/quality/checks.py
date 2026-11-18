"""Per-run data quality checks (pandas) -> ops.check_result.

A 200 with zero rows is a quality failure, not success - except an incremental
run whose watermark window legitimately contains no changes, which is a `warn`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from scrapeline.adapters.rawdoc import Reject
from scrapeline.pipeline.models import SCHEMAS, Record
from scrapeline.registry.config import QualityConfig

CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    value: float | None
    threshold: str
    details: dict[str, Any] = field(default_factory=dict)


def records_frame(records: Sequence[Record], schema: str) -> pd.DataFrame:
    model = SCHEMAS[schema]
    rows = [r.model_dump() | {"external_id": r.external_id} for r in records if type(r) is model]
    return pd.DataFrame(rows, columns=["external_id", *model.model_fields.keys()])


def run_checks(
    records: Sequence[Record],
    rejects: Sequence[Reject],
    cfg: QualityConfig,
    *,
    schema: str = "Meeting",
    zero_is_ok: bool = False,
    duplicates_within_raw: int = 0,
) -> list[CheckResult]:
    """`records` should be deduplicated per identity (latest wins).

    `duplicates_within_raw` counts identities emitted twice from the *same* raw document,
    which is a parser bug. The same identity across pages/runs is normal (overlap windows,
    reprocess) and is resolved by the loader, so it is not counted.
    """
    df = records_frame(records, schema)
    primary_type = SCHEMAS[schema].record_type
    n = len(df)
    n_rejects = sum(1 for r in rejects if r.record_type == primary_type)
    results: list[CheckResult] = []

    # Record count range
    if n < cfg.min_records:
        status: CheckStatus = "warn" if zero_is_ok and n == 0 else "fail"
    elif n > cfg.max_records:
        status = "fail"
    else:
        status = "pass"
    results.append(
        CheckResult(
            "record_count",
            status,
            float(n),
            f"[{cfg.min_records}, {cfg.max_records}]",
            {"zero_is_ok": zero_is_ok},
        )
    )

    # Null rates per field
    for column, max_rate in sorted(cfg.max_null_rate.items()):
        if column not in df.columns:
            continue
        rate = float(df[column].isna().mean()) if n else 0.0
        if rate <= max_rate:
            status = "pass"
        else:
            status = "fail" if max_rate < 0.1 else "warn"
        results.append(CheckResult(f"null_rate.{column}", status, rate, f"<= {max_rate}"))

    # Duplicate identities inside one raw document (parser emitted the same record twice)
    dup_rate = duplicates_within_raw / (n + duplicates_within_raw) if n else 0.0
    results.append(
        CheckResult(
            "duplicate_rate",
            "pass" if duplicates_within_raw == 0 else "fail",
            dup_rate,
            "== 0",
            {"duplicates_within_raw": duplicates_within_raw},
        )
    )

    # Reject rate
    total = n + n_rejects
    reject_rate = n_rejects / total if total else 0.0
    sample = [r.reason for r in rejects[:5]]
    results.append(
        CheckResult(
            "reject_rate",
            "pass" if reject_rate <= cfg.max_reject_rate else "fail",
            reject_rate,
            f"<= {cfg.max_reject_rate}",
            {"rejects": n_rejects, "sample_reasons": sample},
        )
    )
    return results


def worst(results: Sequence[CheckResult]) -> CheckStatus:
    order: dict[CheckStatus, int] = {"pass": 0, "warn": 1, "fail": 2}
    return max((r.status for r in results), key=lambda s: order[s], default="pass")
