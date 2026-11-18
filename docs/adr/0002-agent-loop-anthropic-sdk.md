# ADR-0002: Agent loop on the Anthropic Python SDK tool runner

Status: accepted · Date: 2026-11-13

## Context
The platform needs agents that generate, test and repair scrapers. Candidates: LangGraph, Claude Agent SDK, hand-written loop, Anthropic SDK `tool_runner`.

## Decision
Use the Anthropic Python SDK: `client.beta.messages.tool_runner()` with `@beta_tool` functions for agent loops, `client.messages.parse()` with Pydantic models for structured outputs. Tools are explicit, narrow functions (read sample page, write one adapter file, run tests in sandbox, read test report). Model per role set in `.env`; default `claude-opus-5-5`, switched to `claude-haiku-5-5` only where an eval shows no quality loss.

## Alternatives
- **LangGraph**: state-graph abstraction for a flow that is linear with one bounded loop (test → repair, max 3). Adds a dependency and hides a loop that maintainers should be able to read directly. Rejected.
- **Claude Agent SDK**: ships file/bash tools and subagents. Gives generated code broad filesystem and shell access by default; harder to show cost per run and to restrict the agent to one adapter file. Rejected for now; may revisit for a developer-assist agent.
- **Manual loop**: full control, but re-implements what the tool runner already does (tool dispatch, result feeding, iteration cap). Rejected.

## Consequences
One LLM dependency. Every agent run has explicit tools, an iteration cap, token/cost logging to `ops.agent_run`, and opens a PR rather than merging. Beta API surface; pin the SDK version.
