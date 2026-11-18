.PHONY: setup up down shell image sandbox-image sandbox-check test test-int test-browser lint fmt type check migrate eval demo

setup:          ## one-time: python 3.12 venv + deps + chromium for local runs
	uv python install 3.12
	uv sync
	uv run playwright install chromium
	uv run pre-commit install

up:             ## start postgres + fixture sites (works on Kiri, Docker Desktop, OrbStack, Colima)
	docker compose up -d --wait

down:
	docker compose down

image:          ## build the app/worker image (Playwright base)
	docker compose --profile app build app

shell: image    ## shell inside the app image, wired to compose services
	docker compose --profile app run --rm --entrypoint bash app

sandbox-image:  ## build the offline sandbox image for agent-generated parsers
	docker build -t scrapeline-sandbox:latest -f docker/sandbox/Dockerfile .

sandbox-check: sandbox-image  ## prove the sandbox has no network and a read-only FS on this runtime
	bash scripts/sandbox_check.sh

test:           ## fast unit tests (no docker, no network, no llm)
	uv run pytest

test-int:       ## integration tests against compose services
	uv run pytest -m integration

test-browser:   ## Playwright tests against fixture sites
	uv run pytest -m browser

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

type:
	uv run mypy src

check: lint type test

migrate:
	uv run alembic upgrade head

eval:           ## agent evals (costs money; prints usage)
	uv run scrapeline eval run --suite repair

demo:
	bash scripts/demo.sh
