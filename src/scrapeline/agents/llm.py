"""Thin wrapper over the Anthropic SDK (ADR-0002). SDK calls stay visible; this adds:
model ids from settings, prompt files, usage + cost accounting, budget stop, ops.agent_run logging.

The deterministic platform never imports this module.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine

from scrapeline.errors import AgentError
from scrapeline.ops.logging import get_logger
from scrapeline.settings import Settings
from scrapeline.store import tables as t

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# USD per million tokens: (input, output, cache_read). Cache writes bill at 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


def load_prompt(role: str, version: int) -> tuple[str, str]:
    """Return (prompt_version_id, text) for agents/prompts/<role>.v<N>.md."""
    path = PROMPTS_DIR / f"{role}.v{version}.md"
    if not path.exists():
        raise AgentError(f"missing prompt file {path.name}")
    return f"{role}.v{version}", path.read_text()


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def cost(self, model: str) -> float:
        price_in, price_out, price_cache = PRICES.get(model, PRICES["claude-opus-5-5"])
        usd = (
            self.input_tokens * price_in
            + self.output_tokens * price_out
            + self.cache_read_tokens * price_cache
            + self.cache_write_tokens * price_in * 1.25
        ) / 1_000_000
        return round(usd, 6)


@dataclass
class ToolCall:
    seq: int
    tool: str
    args: dict[str, Any]
    ok: bool
    error: str | None
    duration_ms: int


@dataclass
class DriverResult:
    iterations: int
    usage: Usage
    final_text: str
    stopped: str  # end_turn | max_iterations | budget_exceeded | refusal | error
    model: str
    error: str | None = None


class AgentDriver(Protocol):
    """Runs one agent loop over the given tools. Real: Anthropic tool runner. Tests: scripted."""

    name: str

    def __call__(
        self, *, system: str, user: str, tools: Sequence[Any], max_iterations: int
    ) -> DriverResult: ...


@dataclass
class AnthropicDriver:
    """Claude via `client.beta.messages.tool_runner` with @beta_tool functions."""

    settings: Settings
    model: str
    effort: str = "high"
    max_usd: float = 2.0
    name: str = "anthropic"
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            key = self.settings.anthropic_api_key
            self._client = anthropic.Anthropic(api_key=key.get_secret_value() if key else None)
        return self._client

    def __call__(
        self, *, system: str, user: str, tools: Sequence[Any], max_iterations: int
    ) -> DriverResult:
        import anthropic

        client = self._get_client()
        usage = Usage()
        iterations = 0
        final_text = ""
        stopped = "end_turn"
        try:
            runner = client.beta.messages.tool_runner(
                model=self.model,
                max_tokens=16000,
                # Stable prefix first (tools -> system) so prompt caching hits across attempts.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=list(tools),
                messages=[{"role": "user", "content": user}],
                max_iterations=max_iterations,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                # Server-side refusal fallback (routes by refusal category).
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            for message in runner:
                iterations += 1
                usage.add(message.usage)
                if message.stop_reason == "refusal":
                    stopped = "refusal"
                texts = [b.text for b in message.content if getattr(b, "type", "") == "text"]
                if texts:
                    final_text = texts[-1]
                if usage.cost(self.model) > self.max_usd:
                    stopped = "budget_exceeded"
                    log.warning("agent.budget_exceeded", cost=usage.cost(self.model))
                    break
            if iterations >= max_iterations and stopped == "end_turn":
                stopped = "max_iterations"
        except anthropic.APIStatusError as exc:
            return DriverResult(
                iterations,
                usage,
                final_text,
                "error",
                self.model,
                f"api {exc.status_code}: {exc.message}"[:500],
            )
        except anthropic.APIConnectionError as exc:
            return DriverResult(iterations, usage, final_text, "error", self.model, repr(exc)[:500])
        return DriverResult(iterations, usage, final_text, stopped, self.model)


@dataclass
class AgentRunLog:
    """Writes ops.agent_run + ops.agent_tool_call. No-op without an engine."""

    engine: Engine | None
    role: str
    source_id: str
    prompt_version: str
    case_id: str | None = None
    eval_run_id: uuid.UUID | None = None

    def record(
        self,
        *,
        attempt: int,
        result: DriverResult,
        outcome: str,
        calls: list[ToolCall],
        started_at: datetime,
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        if self.engine is None:
            return run_id
        with self.engine.begin() as conn:
            conn.execute(
                t.agent_run.insert().values(
                    id=run_id,
                    role=self.role,
                    source_id=self.source_id,
                    case_id=self.case_id,
                    eval_run_id=self.eval_run_id,
                    model=result.model,
                    prompt_version=self.prompt_version,
                    attempt=attempt,
                    iterations=result.iterations,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cache_read_tokens=result.usage.cache_read_tokens,
                    cache_write_tokens=result.usage.cache_write_tokens,
                    cost_usd=result.usage.cost(result.model),
                    outcome=outcome,
                    tools_called=[c.tool for c in calls],
                    error=result.error,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            )
            if calls:
                conn.execute(
                    t.agent_tool_call.insert(),
                    [
                        {
                            "agent_run_id": run_id,
                            "seq": c.seq,
                            "tool": c.tool,
                            "args": c.args,
                            "ok": int(c.ok),
                            "error": c.error,
                            "duration_ms": c.duration_ms,
                        }
                        for c in calls
                    ],
                )
        return run_id


def timed(fn: Callable[[], str]) -> tuple[str, int]:
    started = time.monotonic()
    out = fn()
    return out, int((time.monotonic() - started) * 1000)


@dataclass
class NoopDriver:
    """Baseline driver: makes no LLM call and no edit. Measures what happens without an agent."""

    name: str = "noop"
    model: str = "none"

    def __call__(
        self, *, system: str, user: str, tools: Sequence[Any], max_iterations: int
    ) -> DriverResult:
        return DriverResult(0, Usage(), "", "end_turn", self.model)
