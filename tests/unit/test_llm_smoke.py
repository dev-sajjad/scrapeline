"""Smoke test for the beta SDK surface we depend on (tool_runner, @beta_tool, usage fields).

Opt-in: `uv run pytest -m llm tests/unit/test_llm_smoke.py` (costs a fraction of a cent).
Run after every SDK upgrade (CLAUDE.md §3).
"""

from __future__ import annotations

import pytest

from scrapeline.agents.llm import AnthropicDriver
from scrapeline.settings import Settings

pytestmark = pytest.mark.llm


def test_tool_runner_calls_tool_and_reports_usage() -> None:
    settings = Settings()
    if settings.anthropic_api_key is None:
        pytest.skip("ANTHROPIC_API_KEY not set")
    from anthropic import beta_tool

    calls: list[int] = []

    def add(a: int, b: int) -> str:
        """Add two integers.

        Args:
            a: First integer.
            b: Second integer.
        """
        calls.append(a + b)
        return str(a + b)

    driver = AnthropicDriver(settings, settings.model_cheap, effort="low", max_usd=0.05)
    out = driver(
        system="Use the add tool for arithmetic, then answer with the number only.",
        user="What is 19 + 23?",
        tools=[beta_tool(add)],
        max_iterations=3,
    )
    assert out.error is None, out.error
    assert calls == [42]
    assert "42" in out.final_text
    assert out.usage.input_tokens > 0 and out.usage.output_tokens > 0
    assert out.usage.cost(out.model) < 0.05
