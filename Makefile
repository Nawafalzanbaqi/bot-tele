# ---------------------------------------------------------------------------
# MediaHub developer task runner.
#
# Every target is safe to run repeatedly. `make help` lists what is available.
# On Windows use Git Bash / WSL, or run the underlying commands directly.
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
.PHONY: help install format lint typecheck test test-unit test-integration check \
        up down logs shell migrate migration clean

PYTHON ?= python
COMPOSE ?= docker compose

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Create a local environment with dev dependencies + git hooks
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"
	pre-commit install

format: ## Apply Black and Ruff autofixes
	black src tests
	ruff check --fix src tests

lint: ## Static lint checks (no writes)
	ruff check src tests
	black --check src tests

typecheck: ## Strict type checking
	mypy

test: ## Full test suite with coverage
	pytest

test-unit: ## Only the fast, isolated tests
	pytest tests/unit -m "not integration"

test-integration: ## Only adapter / HTTP tests
	pytest tests/integration

check: lint typecheck test ## Everything CI runs

up: ## Start the stack (API + Postgres + Redis) in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack and remove containers
	$(COMPOSE) down

logs: ## Tail API logs
	$(COMPOSE) logs -f api

shell: ## Open a shell inside the API container
	$(COMPOSE) exec api /bin/bash

migrate: ## Apply all pending database migrations
	$(COMPOSE) exec api alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add x"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

clean: ## Remove caches and build artefacts
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage coverage.xml build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
