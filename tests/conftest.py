from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "tests" / "snapshots"


def public_resolver(host: str) -> list[str]:
    """Deterministic DNS for unit tests: every host is a public address."""
    return ["93.184.215.14"]


@pytest.fixture
def snapshots() -> Path:
    return SNAPSHOTS
