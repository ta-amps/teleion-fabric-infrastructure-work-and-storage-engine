# Tessera: atomic work queue and atomic storage. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash
SUITE ?= conformance-draft
BACKEND ?= store
DB ?= .tessera/tessera.db

FACTORY_store := tessera.testing:create_store_backend
FACTORY_http := tessera.testing:create_http_backend
FACTORY_empty := tessera.testing:create_empty_backend

.PHONY: openapi-check help setup check lint types test test-worker suite suite-stub suite-http-local public lock-suite clean serve dev seed expand-demo bilateral-demo index index-check architecture

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install Python 3.12 and every dependency with uv
	uv sync --all-groups --all-extras

check: agent-start-check lint types test public lock-check index-check architecture openapi-check ## Lint, types, unit tests, leak scan; run before every commit

lint: ## ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

types: ## mypy strict on src/tessera
	uv run mypy

test: ## Unit tests and the conformance suite on the in-process store
	uv run pytest -q

test-worker: ## Actual local workerd/SQLite HTTP and MCP regression tests (npm ci first)
	npm run test:worker

suite: ## Conformance suite on one backend: make suite BACKEND=store|http
	TESSERA_BACKEND_FACTORY=$(FACTORY_$(BACKEND)) uv run pytest -q $(SUITE)

suite-stub: ## Prove the suite fails against an empty backend (it must)
	@if TESSERA_BACKEND_FACTORY=$(FACTORY_empty) uv run pytest -q -m fabric_all $(SUITE) >/dev/null 2>&1; \
	then echo "FAIL: the suite passed against an empty backend"; exit 1; \
	else echo "ok: the suite fails against an empty backend, as it must"; fi

suite-http-local: ## The whole suite over HTTP against a real local service with real keys
	uv run python scripts/suite_http_local.py

serve: ## The service alone on 127.0.0.1:7700 (no edge, no origin lock)
	uv run tessera --db $(DB) serve --port 7700

dev: ## Service behind the local Cloudflare edge; TUNNEL=1 adds a temporary public https address
	scripts/dev_stack.sh $(if $(TUNNEL),--tunnel,)

seed: ## Queues, one key per agent (private file, mode 600) and the first tasks
	uv run python tools/seed.py --db $(DB)

expand-demo: ## Run an end-to-end seed -> expand -> enrich -> review -> canonical noun+verb pass
	uv run python tools/swarm_expander.py --demo --dry-model

bilateral-demo: ## Run the two-agent bilateral park/resume + seed->canonical MCP/HTTP pass
	uv run python tools/opencode_bilateral.py --dry-model

index: ## Rebuild INDEX.md, KEYWORDS.md, llms.txt and llms-full.txt from the files
	python3 scripts/build_index.py

index-check: ## Fail when the indexes are stale
	python3 scripts/build_index.py --check

public: ## Secret and private-name scan of every tracked file
	python3 scripts/check_public.py .

lock-check: ## Once the suite is locked, its files must match conformance.lock
	@if [ -f conformance.lock ]; then sha256sum --quiet -c conformance.lock; else echo "suite not locked yet (conformance-draft/)"; fi

lock-suite: ## Taylor only: move the approved draft to conformance/ and write conformance.lock
	@test -d conformance-draft || { echo "nothing to lock"; exit 1; }
	@read -p "Lock the conformance suite as approved by Taylor? [y/N] " ok && [ "$$ok" = y ]
	git mv conformance-draft conformance
	find conformance -type f -name '*.py' -o -type f -name '*.md' | sort | xargs sha256sum > conformance.lock
	@echo "Locked. Set SUITE=conformance (the Makefile default changes when you edit it)."

clean: ## Remove caches and local databases
	rm -rf .pytest_cache .mypy_cache .ruff_cache .tessera build dist

architecture: ## architecture.json must match the code: paths, tests, layering rules
	python3 scripts/check_architecture.py . --summary

openapi-check: ## Generated access schema must match the HTTP route/model contract
	uv run python scripts/build_access_openapi.py --check

.PHONY: agent-start-check
agent-start-check: ## Check public agent onboarding instructions and HTTP boundaries
	node --test tests/agent_start.test.mjs
