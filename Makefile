.PHONY: setup up down test test-int lint type fmt migrate fixtures eval demo

setup:        ## one-time: python 3.12 venv + deps + playwright browsers
	uv python install 3.12
	uv sync --all-extras
	uv run playwright install chromium

up:           ## start postgres, minio, fixture sites
	docker compose up -d

down:
	docker compose down

test:         ## fast unit tests (no docker, no llm)
	uv run pytest

test-int:     ## integration tests against docker services
	uv run pytest -m integration

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

type:
	uv run mypy src

migrate:
	uv run alembic upgrade head

eval:         ## run all agent evals (costs money; prints usage)
	uv run scrapeline eval run --suite all

demo:         ## five-minute demo script
	bash scripts/demo.sh
