# mcpcheck — the usual entry points. `make` (or `make help`) lists them.
# Works with GNU make and BSD make (macOS); every target is one line of real commands.
UV ?= uv
PYTEST_FLAGS ?= -q -n 4

help: ## list these targets
	@awk -F':.*##' '/^[a-z][a-z-]*:.*##/ { printf "  make %-16s %s\n", $$1, $$2 }' Makefile

install: ## create the dev environment (uv sync); then: uvx pre-commit install
	$(UV) sync && echo 'done. Optional: uvx pre-commit install'

demo: ## run the one-command demo (conformant server, then a broken one)
	bash examples/demo.sh

test: ## run the whole test suite in parallel
	$(UV) run pytest $(PYTEST_FLAGS)

test-fast: ## run only the tests that need no socket
	$(UV) run pytest -q -m "not integration"

lint: ## ruff check + ruff format --check
	$(UV) run ruff check . && $(UV) run ruff format --check .

format: ## apply ruff's autofixes and formatting
	$(UV) run ruff check --fix . && $(UV) run ruff format .

typecheck: ## mypy (strict)
	$(UV) run mypy

check: lint typecheck test ## lint + typecheck + test, in that order
	@echo 'all checks passed'

build: ## build the sdist and wheel and validate them for PyPI
	$(UV) build && uvx twine@7.0.0 check --strict dist/*

clean: ## remove build, cache and report artefacts
	rm -rf dist build reports htmlcov .pytest_cache .ruff_cache .mypy_cache .coverage* src/*.egg-info

serve-reference: ## serve the bundled conformant reference server on a free port
	$(UV) run python -m tests.fixtures.reference_server --port 0

readme-options: ## regenerate the README options table from mcpcheck --help
	$(UV) run python scripts/gen_options_table.py README.md

.PHONY: help install demo test test-fast lint format typecheck check build clean serve-reference readme-options
