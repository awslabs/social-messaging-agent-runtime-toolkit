# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

.PHONY: help install dev test lint fmt check clean tf-plan tf-apply tf-destroy ui-dev ui-build ui-test ui-smoke ui-preview ui-install ui-deploy ui-plan ui-apply ui-destroy ui-invalidate backend-test backend-smoke

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Python ──────────────────────────────────────────────────────────

install: ## Install project dependencies
	uv sync --frozen

dev: ## Run local dev REPL (no deployed stack needed; LLM blueprints still need Bedrock credentials)
	uv run socialagent dev

test: ## Run tests (excludes the slow wheel-build suite)
	uv run --frozen pytest -m "not slow"

test-arch: ## Run the AGENTS.md layering rules only
	uv run --frozen pytest -m architecture -v

test-packaging: ## Verify a pip-installed `socialagent init` scaffolds a project that boots
	uv run --frozen pytest tests/test_cli_packaging.py -v

test-e2e: ## Run every CLI command for real as a subprocess (offline; add SOCIALAGENT_E2E_BEDROCK=1 for the live-model test)
	uv run --frozen pytest -m e2e -v

test-all: ## Run every test, including the slow packaging suite
	uv run --frozen pytest

backend-test: ## Run admin console backend tests
	cd socialagent-admin-console/backend && \
	  python3 -m pip install -r requirements-dev.txt --quiet && \
	  python3 -m pytest

backend-smoke: ## Smoke test the admin backend API (requires API_URL + ADMIN_TOKEN)
	@bash socialagent-admin-console/scripts/smoke_admin_api.sh

lint: ## Lint with ruff, and fail if uv.lock is out of date with pyproject.toml
	uv lock --check
	uv run --frozen ruff check src/ tests/ packages/

fmt: ## Format with ruff
	uv run ruff format src/ tests/ packages/

fmt-check: ## Check formatting without writing (what CI runs)
	uv run --frozen ruff format --check src/ tests/ packages/

check: lint fmt-check test ## Lint + format check + test (mirrors CI)

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info .pytest_cache .ruff_cache .terraform-validate
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ── Admin Console (UI) ──────────────────────────────────────────────

UI_DIR := socialagent-admin-console

ui-dev: ## Start admin console dev server
	cd $(UI_DIR) && npm run dev

ui-build: ## Build admin console for production
	cd $(UI_DIR) && npm run build

ui-test: ## Run admin console tests
	cd $(UI_DIR) && npm run test

ui-smoke: ## Start the admin console dev server headless and check it serves the app
	cd $(UI_DIR) && npm run smoke:dev

ui-preview: ## Preview production build locally
	cd $(UI_DIR) && npm run preview

ui-install: ## Install admin console dependencies
	cd $(UI_DIR) && npm install

# ── Admin Console Terraform ─────────────────────────────────────────

UI_TF_DIR := socialagent-admin-console/infra-terraform
UI_ENV    ?= dev

ui-deploy: ## Full deploy: infra → build with Cognito IDs → upload → invalidate
	terraform -chdir=$(UI_TF_DIR) init -backend-config=envs/backend-$(UI_ENV).hcl
	@echo "── Step 1/4: Creating infrastructure (Cognito, API, S3, CloudFront)..."
	terraform -chdir=$(UI_TF_DIR) apply -var-file=envs/$(UI_ENV).tfvars
	@echo "── Step 2/4: Building UI with Cognito config..."
	$(eval VITE_USER_POOL_ID := $(shell terraform -chdir=$(UI_TF_DIR) output -raw cognito_user_pool_id))
	$(eval VITE_USER_POOL_CLIENT_ID := $(shell terraform -chdir=$(UI_TF_DIR) output -raw cognito_client_id))
	$(eval VITE_IDENTITY_POOL_ID := $(shell terraform -chdir=$(UI_TF_DIR) output -raw cognito_identity_pool_id))
	$(eval VITE_API_URL := $(shell terraform -chdir=$(UI_TF_DIR) output -raw api_url))
	cd $(UI_DIR) && VITE_USER_POOL_ID=$(VITE_USER_POOL_ID) VITE_USER_POOL_CLIENT_ID=$(VITE_USER_POOL_CLIENT_ID) VITE_IDENTITY_POOL_ID=$(VITE_IDENTITY_POOL_ID) VITE_AWS_REGION=us-east-1 VITE_API_URL=$(VITE_API_URL)api npm run build
	@echo "── Step 3/4: Uploading UI to S3..."
	terraform -chdir=$(UI_TF_DIR) apply -var-file=envs/$(UI_ENV).tfvars -auto-approve
	@echo "── Step 4/4: Invalidating CloudFront cache..."
	@DIST_ID=$$(terraform -chdir=$(UI_TF_DIR) output -raw cloudfront_distribution_id 2>/dev/null) && \
	if [ -n "$$DIST_ID" ]; then \
		aws cloudfront create-invalidation --distribution-id $$DIST_ID --paths "/*" > /dev/null; \
		echo "✅ Deploy complete: $$(terraform -chdir=$(UI_TF_DIR) output -raw cloudfront_url)"; \
	fi

ui-plan: ## Plan admin console infrastructure
	terraform -chdir=$(UI_TF_DIR) init -backend-config=envs/backend-$(UI_ENV).hcl
	terraform -chdir=$(UI_TF_DIR) plan -var-file=envs/$(UI_ENV).tfvars

ui-apply: ## Apply admin console infrastructure (no UI build)
	terraform -chdir=$(UI_TF_DIR) apply -var-file=envs/$(UI_ENV).tfvars

ui-destroy: ## Destroy admin console infrastructure
	terraform -chdir=$(UI_TF_DIR) init -backend-config=envs/backend-$(UI_ENV).hcl
	terraform -chdir=$(UI_TF_DIR) destroy -var-file=envs/$(UI_ENV).tfvars

ui-invalidate: ## Invalidate CloudFront cache
	@DIST_ID=$$(terraform -chdir=$(UI_TF_DIR) output -raw cloudfront_distribution_id 2>/dev/null) && \
	aws cloudfront create-invalidation --distribution-id $$DIST_ID --paths "/*"

# ── Terraform ───────────────────────────────────────────────────────

TF_DIR := infra-terraform
# Use: make tf-plan ENV=<project>-<env>  (e.g. ENV=my-bot-dev)
ENV    ?= my-bot-dev
STAGE  ?= dev

# The contributor path drives the same stack a developer deploys, from the same
# input: a project's socialagent.<env>.yaml. ENV=<project>-<env> names the
# project under projects/ and the environment; the CLI writes the tfvars, selects
# the workspace and runs Terraform. There is no hand-written tfvars any more.
# A project name may contain hyphens (my-bot-dev → project my-bot, env dev), so
# the environment is the last dash-separated word and the project is the rest.
STAGE_OF = $(lastword $(subst -, ,$(ENV)))
PROJECT = $(patsubst %-$(STAGE_OF),%,$(ENV))
PROJECT_DIR = projects/$(PROJECT)

tf-plan: ## Plan the stack of a project (ENV=<project>-<env>)
	@test -f $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml || { echo "ERROR: $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml not found."; exit 1; }
	cd $(PROJECT_DIR) && uv run socialagent deploy --plan --env $(STAGE_OF)

tf-apply: ## Deploy the stack of a project (ENV=<project>-<env>)
	@test -f $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml || { echo "ERROR: $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml not found."; exit 1; }
	cd $(PROJECT_DIR) && uv run socialagent deploy --env $(STAGE_OF)

tf-destroy: ## Destroy the stack of a project (ENV=<project>-<env>)
	@test -f $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml || { echo "ERROR: $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml not found."; exit 1; }
	cd $(PROJECT_DIR) && uv run socialagent destroy --env $(STAGE_OF)

tf-fmt: ## Format Terraform files
	terraform -chdir=$(TF_DIR) fmt -recursive

TF_VALIDATE_DIR := $(CURDIR)/.terraform-validate

# tf-validate-roots stops after init and validate, so it runs on Terraform 1.7,
# the minimum the three roots' versions.tf declare, and CI runs it on 1.7.5 to
# keep that minimum true. tf-validate adds the mocked plan tests, which need 1.11.
tf-validate-roots: ## Validate the three Terraform roots without the mocked plan tests (Terraform 1.7 or later), no AWS needed
	@# `validate` needs modules and providers installed. -backend=false skips
	@# backend init, but it does NOT override a .terraform/ left behind by an
	@# earlier real `init` — Terraform would still try to lock remote state and
	@# fail without credentials. TF_DATA_DIR points init at a throwaway directory
	@# per root, so validation never reads or writes the real backend cache and
	@# works on a fresh checkout with no credentials.
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/stack terraform -chdir=$(TF_DIR) init -backend=false -input=false >/dev/null
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/stack terraform -chdir=$(TF_DIR) validate
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/bootstrap terraform -chdir=$(TF_DIR)/bootstrap init -backend=false -input=false >/dev/null
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/bootstrap terraform -chdir=$(TF_DIR)/bootstrap validate
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/console terraform -chdir=$(UI_DIR)/infra-terraform init -backend=false -input=false >/dev/null
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/console terraform -chdir=$(UI_DIR)/infra-terraform validate

tf-validate: tf-validate-roots ## Validate Terraform configuration (all three roots) and run the stack's mocked plan tests, no AWS needed
	@# Plans the stack with mocked providers (no credentials, no state), in the
	@# data directory tf-validate-roots initialised. Needs Terraform 1.11 or
	@# later, for override_during, which is also why the files sit in tftest/
	@# and not in the tests/ that init and validate parse.
	@# The second init installs the modules the run blocks there load on their
	@# own (`module { source = ... }`), which a plain init does not.
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/stack terraform -chdir=$(TF_DIR) init -backend=false -input=false -test-directory=tftest >/dev/null
	TF_DATA_DIR=$(TF_VALIDATE_DIR)/stack terraform -chdir=$(TF_DIR) test -test-directory=tftest

e2e: ## Run E2E tests against a deployed stack (ENV=<project>-<env>)
	cd $(PROJECT_DIR) && uv run python ../../scripts/e2e_test.py --env $(STAGE_OF)

# From the project's YAML outputs (`socialagent config sync` writes them), not from
# infra-terraform/.terraform, which no target maintains any more.
MEMORY_ID ?= $(shell uv run python -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["outputs"].get("memory_id",""))' $(PROJECT_DIR)/socialagent.$(STAGE_OF).yaml 2>/dev/null)

clear-memory: ## Clear all AgentCore Memory events
	@if [ -z "$(MEMORY_ID)" ]; then echo "ERROR: Could not resolve MEMORY_ID."; exit 1; fi
	@echo "Clearing AgentCore Memory..."
	@python3 scripts/clear_memory.py $(MEMORY_ID)

reset: clear-memory ## Clear memory (redeploy separately with make tf-apply)
	@echo "✅ Memory cleared"
