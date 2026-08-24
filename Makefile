PYTHON ?= python
PIP := $(PYTHON) -m pip

.PHONY: help install install-dev lint format typecheck test test-cov run docker-build docker-up docker-down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install runtime dependencies
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

install-dev: install ## Install with dev tooling
	$(PIP) install -e ".[dev]"

lint: ## Run ruff checks
	$(PYTHON) -m ruff check src cli tests

format: ## Apply ruff formatting
	$(PYTHON) -m ruff format src cli tests
	$(PYTHON) -m ruff check --fix src cli tests

typecheck: ## Run mypy static type checking
	$(PYTHON) -m mypy src cli

test: ## Run the test suite
	$(PYTHON) -m pytest

test-cov: ## Run tests with coverage report
	$(PYTHON) -m pytest --cov=src --cov=cli --cov-report=term-missing

run: ## Start the dashboard API locally
	$(PYTHON) -m uvicorn src.dashboard.app:create_app --factory --reload

docker-build: ## Build the container image
	docker build -t devopspipeline:latest .

docker-up: ## Start the full compose stack
	docker compose up --build -d

docker-down: ## Stop the compose stack
	docker compose down

clean: ## Remove caches and build artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} +
