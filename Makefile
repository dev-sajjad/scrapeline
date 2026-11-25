.PHONY: setup up up-core down panel shell image sandbox-image sandbox-check test test-int test-browser lint fmt type check lint-imports migrate datasets eval eval-baseline api demo demo-history lake dbt dbt-docs

setup:          ## one-time: python 3.12 venv + deps + chromium for local runs
	uv python install 3.12
	uv sync --extra serve --extra analytics
	uv run playwright install chromium
	uv run pre-commit install

up:             ## start postgres, fixture sites and the control panel on http://localhost:8000
	docker compose --profile panel up -d --wait --build

up-core:        ## start only postgres + fixture sites (works on Kiri, Docker Desktop, OrbStack, Colima)
	docker compose up -d --wait

down:
	docker compose --profile panel --profile prod down

panel:          ## control panel from this checkout on http://localhost:8000, reloads on change
	uv run uvicorn scrapeline.serve.panel.app:create_app --factory --port 8000 --reload --timeout-graceful-shutdown 3

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

check:         ## lint + types + unit tests (pipefail-safe gate)
	bash scripts/check.sh

migrate:
	uv run alembic upgrade head

datasets:       ## register dataset types (built-in packs + config/datasets) in the catalog
	uv run scrapeline dataset sync

lint-imports:   ## enforce layer contracts (.importlinter)
	uv run lint-imports

eval:           ## agent evals: repair + generation (costs money; needs ANTHROPIC_API_KEY)
	uv run scrapeline eval run --suite repair --driver anthropic
	uv run scrapeline eval run --suite generation --driver anthropic

eval-baseline:  ## free baseline (no LLM) for both suites
	uv run scrapeline eval run --suite repair --driver noop
	uv run scrapeline eval run --suite generation --driver noop

api:            ## read API on :8000
	uv run uvicorn scrapeline.serve.api:app --port 8000

demo:           ## end-to-end demo (no API key needed)
	bash scripts/demo.sh

demo-history:   ## simulated shop over three days: prices, stock, removals in the history
	bash scripts/demo_history.sh

lake:           ## export Postgres to the Parquet lake in data/lake
	uv run scrapeline lake export

dbt:            ## build and test the dbt project over the lake
	uv run --extra analytics dbt build --project-dir dbt --profiles-dir dbt

dbt-docs:       ## generate dbt docs (catalog and lineage)
	uv run --extra analytics dbt docs generate --project-dir dbt --profiles-dir dbt
