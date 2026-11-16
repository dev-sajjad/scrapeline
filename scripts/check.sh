#!/usr/bin/env bash
# Local gate before commit. `set -o pipefail` so a failing tool is never hidden by a pipe.
set -euo pipefail
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
