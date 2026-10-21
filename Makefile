# SPDX-License-Identifier: GPL-3.0-only

PYTHON ?= python3
PY := $(CURDIR)/.venv/bin/python
HA_CONFIG := $(CURDIR)/.ha-config

.DEFAULT_GOAL := help
.PHONY: help py-dev py-lint py-fmt py-test py-test-e2e hassfest verify-container dev-config dev-clean run

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

py-dev: ## Create/sync the venv (.venv)
	uv venv --python "$(PYTHON)"
	uv sync --frozen

py-lint: ## Ruff + mypy
	uv run --frozen ruff check custom_components tests scripts
	uv run --frozen ruff format --check custom_components tests scripts
	uv run --frozen mypy

py-fmt: ## Format Python sources
	uv run --frozen ruff check --fix custom_components tests scripts
	uv run --frozen ruff format custom_components tests scripts

py-test: ## Integration tests (mocked portal)
	uv run --frozen pytest

py-test-e2e: ## Tests against the real portal (PROVIDENT_USERNAME/PASSWORD in env)
	uv run --frozen pytest -m e2e

# podman locally, docker in CI
CONTAINER_RUNTIME ?= $(shell command -v podman || command -v docker)

hassfest: ## Validate the integration with Home Assistant's own hassfest (container)
	$(CONTAINER_RUNTIME) run --rm -v "$(CURDIR)":/github/workspace:ro,Z ghcr.io/home-assistant/hassfest:latest

verify-container: ## End-to-end check inside a real Home Assistant container (podman/docker)
	$(PY) scripts/verify_container.py

dev-config: ## (Re)create a local HA config with the integration symlinked in
	rm -rf $(HA_CONFIG)
	mkdir -p $(HA_CONFIG)/custom_components
	ln -s $(CURDIR)/custom_components/provident $(HA_CONFIG)/custom_components/provident
	printf 'default_config:\n' > $(HA_CONFIG)/configuration.yaml
	printf 'logger:\n  logs:\n    custom_components.provident: debug\n' >> $(HA_CONFIG)/configuration.yaml

run: dev-config ## Run a local Home Assistant with the integration
	$(PY) -m homeassistant -v -c $(HA_CONFIG)

dev-clean: ## Remove venv and local HA config
	rm -rf .venv $(HA_CONFIG)
