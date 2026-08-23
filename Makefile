# Clinical Data Atlas — pipeline & site automation.
# Run `make` or `make help` to list targets.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BASE_URL ?= /clinical-data-atlas/
BUILD ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
PORT ?= 8080
PHASE ?= 0
SITE_URL ?= https://nielspac177.github.io/clinical-data-atlas/
FG_VERSION := 1.80.0
FG_URL := https://unpkg.com/3d-force-graph@$(FG_VERSION)/dist/3d-force-graph.min.js

# What a failing browser test leaves behind for the CI artifact upload.
# The directory is gitignored and pytest-playwright empties it per run.
E2E_CAPTURE := --screenshot=only-on-failure --tracing=retain-on-failure \
  --output=tests/e2e/artifacts

.PHONY: help setup vendor harvest normalize enrich graph diff validate refresh site serve test test-live e2e og dod clean

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install all dependency groups with uv
	uv sync --all-groups

vendor: ## Download and checksum-verify the vendored 3d-force-graph bundle
	mkdir -p site/vendor && curl -fsSL "$(FG_URL)" -o site/vendor/3d-force-graph.min.js && (cd site/vendor && shasum -a 256 -c SHA256SUMS)

harvest: ## Run harvesters (all sources, or SOURCE=<name> for one)
	uv run atlas harvest $(if $(SOURCE),--source $(SOURCE))

normalize: ## Normalize raw records into the canonical schema
	uv run atlas normalize $(if $(SOURCE),--source $(SOURCE))

enrich: ## Classify and resolve records into data/catalog/enriched.jsonl
	uv run atlas enrich

graph: ## Build data/graph/graph.json, search-index.json, stats.json
	uv run atlas graph

diff: ## Write a changelog entry for the catalog versus the one at HEAD
	uv run atlas diff

validate: ## Validate the catalog against the schema and the graph contract
	uv run atlas validate

refresh: ## Run the full pipeline end to end (harvest -> ... -> diff)
	uv run atlas refresh $(if $(SOURCE),--sources $(SOURCE))

site: ## Build the static site into _site/
	uv run python -m atlas.sitebuild --base-url "$(BASE_URL)" --build "$(BUILD)" --out _site && touch _site/.nojekyll && test -d _site && ! grep -rl -e '__BUILD__' -e '__BASE_URL__' -e '__SITE_URL__' -e '__UPDATED__' -e '__MAINTAINER__' -e '__REPO_URL__' --exclude-dir=vendor _site/

serve: site ## Build the site, then serve _site/ locally on $(PORT)
	uv run python -m http.server $(PORT) --directory _site

test: ## Run the unit test suite (no network, no browser)
	uv run pytest -q -m "not live and not e2e"

test-live: ## Run tests marked "live" (hits the real network)
	ATLAS_LIVE=1 ATLAS_OFFLINE=0 uv run pytest -q -m live

e2e: ## Install Chromium and run browser tests (they build and serve the site)
	uv run --group e2e playwright install chromium && \
	uv run --group e2e pytest -q -m e2e tests/e2e $(E2E_CAPTURE)

og: ## Re-render site/assets/img/og.png from og.svg (commit the result)
	uv run --group e2e playwright install chromium && \
	uv run --group e2e python -m atlas.tools.og_png

dod: ## Check phase PHASE's definition-of-done against URL
	uv run atlas dod --phase $(PHASE) --url $(SITE_URL)

clean: ## Remove build artifacts and caches
	rm -rf _site .pytest_cache .ruff_cache .cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
