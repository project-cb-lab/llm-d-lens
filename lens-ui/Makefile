# Project configuration
IMAGE ?= lens
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
PLATFORMS ?= linux/amd64,linux/arm64

# Container engine: docker buildx if present, else podman. Override with
# CONTAINER_TOOL=podman to force one.
CONTAINER_TOOL ?= $(shell docker buildx version >/dev/null 2>&1 && echo docker || echo podman)

.DEFAULT_GOAL := help

##@ General

.PHONY: help
help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Development

.PHONY: build
build: ## Build the frontend
	npm run build

.PHONY: test test-js test-python
test: test-js test-python ## Run Node and Python tests

test-js: ## Run frontend and Node API unit tests
	node --import tsx --test $$(find src server -type f \( -name '*.test.js' -o -name '*.test.jsx' -o -name '*.test.ts' \))
	npm run reuse:test

PYTHON ?= .venv/bin/python
test-python: ## Run Python backend tests
	$(PYTHON) -m pytest tests llm_d_bench

.PHONY: lint lint-js lint-python
lint: lint-js lint-python ## Run JavaScript and Python linters

lint-js:
	npm run lint

lint-python:
	ruff check llm_d_bench tests
	ruff format --check llm_d_bench tests

.PHONY: fmt
fmt: ## Format Python code
	ruff format llm_d_bench tests

.PHONY: pre-commit
pre-commit: ## Run pre-commit hooks on all files
	pre-commit run --all-files

##@ Container

.PHONY: image-build
image-build: ## Build multi-arch container image (local only)
ifeq ($(CONTAINER_TOOL),docker)
	docker buildx build \
		--platform $(PLATFORMS) \
		--tag $(IMAGE):$(VERSION) \
		--tag $(IMAGE):latest \
		.
else
	podman manifest rm $(IMAGE):$(VERSION) 2>/dev/null || true
	podman build \
		--platform $(PLATFORMS) \
		--manifest $(IMAGE):$(VERSION) \
		.
endif
