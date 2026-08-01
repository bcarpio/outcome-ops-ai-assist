.PHONY: help setup install fmt lint validate check-ha-parity test test-unit test-coverage test-ui audit build-run-tests-image build-ui-image build-mcp-server-image build-snyk-image build-database-images build-runtime-layer build-terraform-layer build-all-layers build-platform-layer build-all-images pre-deploy deliver clean all ha-status ha-replay-dlq ha-list-secrets ha-sync-secrets ha-bootstrap ha-kms-migrate _ha-env-check

APP_NAME ?= outcome-ops-ai-assist
AWS_REGION ?= us-west-2

# Default target
help:
	@echo "OutcomeOps AI Assist - Build System"
	@echo ""
	@echo "Setup & Dependencies:"
	@echo "  make setup            Create Python virtual environment and install deps"
	@echo "  make install          Install Python dependencies (assumes venv exists)"
	@echo ""
	@echo "Code Quality & Validation:"
	@echo "  make fmt              Format terraform code"
	@echo "  make lint             Lint markdown files (check sizes for embedding)"
	@echo "  make validate         Validate Terraform & documentation sizes"
	@echo "  make validate-tf      Validate Terraform configuration only"
	@echo "  make validate-docs    Check documentation token counts (for embedding)"
	@echo ""
	@echo "Testing (Lambda Functions):"
	@echo "  make test             Run all Lambda function tests"
	@echo "  make test-unit        Run only unit tests"
	@echo "  make test-coverage    Run tests with coverage report"
	@echo "  make test-ui          Run UI tests (React/Vitest)"
	@echo ""
	@echo "Security:"
	@echo "  make audit            Run npm audit on UI dependencies"
	@echo ""
	@echo "Container Images:"
	@echo "  make build-run-tests-image      Build run-tests container (auto-creates ECR, ENVIRONMENT=dev|prd)"
	@echo "  make build-ui-image             Build chat UI container (auto-creates ECR, ENVIRONMENT=dev|prd)"
	@echo "  make build-mcp-server-image     Build OutcomeOps MCP server container (ENVIRONMENT=dev|prd)"
	@echo "  make build-snyk-image           Build Snyk MCP server container (ENVIRONMENT=dev|prd)"
	@echo "  make build-database-images      Build database-integration + database-sync container images (ENVIRONMENT=dev|prd)"
	@echo "  make build-all-images           Build every container image in one shot (ENVIRONMENT=dev|prd)"
	@echo ""
	@echo "Lambda Layers:"
	@echo "  make build-runtime-layer        Build the runtime tools layer (git/make/tar) for generate-code"
	@echo "  make build-terraform-layer      Build the terraform CLI layer for generate-code fmt/validate"
	@echo "  make build-all-layers           Build every Lambda layer in one shot"
	@echo "  make build-platform-layer       Build the platform-CA trust layer (certifi + platform_ca_bundle_ssm_param_arns)"
	@echo ""
	@echo "Combined Deployment:"
	@echo "  make pre-deploy                 Build all layers, all images, and the platform-CA layer (ENVIRONMENT=dev|prd)."
	@echo "                                  One-command catch-all before 'terraform apply'."
	@echo ""
	@echo "Combined:"
	@echo "  make all              Run fmt, validate, and all tests"
	@echo "  make clean            Clean all build artifacts"
	@echo ""

# ============================================================================
# Setup & Dependencies: Create venv and install Python packages
# ============================================================================

setup:
	@echo "Setting up Python virtual environment..."
	python3.12 -m venv venv
	@echo "Installing Python dependencies..."
	$(MAKE) install
	@echo "Setup complete! Activate venv with: source venv/bin/activate"

install:
	@echo "Installing Python dependencies..."
	@for req in lambda/*/requirements.txt; do \
		if [ -f "$$req" ]; then \
			venv/bin/pip install -q -r "$$req"; \
		fi; \
	done
	@echo "Dependencies installed"

# ============================================================================
# Infrastructure: Terraform Formatting and Validation
# ============================================================================

fmt:
	@echo "Formatting terraform code..."
	cd terraform && terraform fmt -recursive

# Alias for validate-docs (common in many projects)
lint: validate-docs

validate: validate-tf validate-docs check-ha-parity
	@echo ""
	@echo "All validation checks passed ✓"

validate-tf:
	@echo "Validating terraform configuration..."
	cd terraform && terraform validate

# ADR-022 primary/secondary parity guard. Compares env-var key sets, IAM
# statement names, and merge()'d locals for every module with a
# *_secondary mirror. Fails on drift -- the class of bug that is
# invisible until failover (six modules had silently drifted when this
# landed 2026-07-20). Pure stdlib; any python3 works.
check-ha-parity:
	@echo "Checking HA primary/secondary parity..."
	python3 scripts/check-ha-parity.py

validate-docs:
	@echo "Checking documentation links resolve..."
	@bash scripts/check-doc-links.sh
	@echo ""
	@echo "Validating file sizes for code map embedding..."
	@echo "Token limit: 12000 tokens (~48KB file size)"
	@echo "Estimate: 1 token ≈ 4 bytes (rough approximation)"
	@echo ""
	@FAILED=0; \
	TOTAL=0; \
	echo "Markdown files:"; \
	for file in README.md docs/*.md docs/adr/*.md; do \
		if [ -f "$$file" ]; then \
			TOTAL=$$((TOTAL + 1)); \
			bytes=$$(wc -c < "$$file"); \
			tokens=$$((bytes / 4)); \
			if [ $$tokens -gt 12000 ]; then \
				printf "  ✗ %-50s %6d tokens (EXCEEDS LIMIT by %d)\n" "$$file" "$$tokens" "$$((tokens - 12000))"; \
				FAILED=$$((FAILED + 1)); \
			else \
				remaining=$$((12000 - tokens)); \
				printf "  ✓ %-50s %6d tokens (%d remaining)\n" "$$file" "$$tokens" "$$remaining"; \
			fi; \
		fi; \
	done; \
	echo ""; \
	echo "Terraform files:"; \
	for file in terraform/*.tf; do \
		if [ -f "$$file" ]; then \
			TOTAL=$$((TOTAL + 1)); \
			bytes=$$(wc -c < "$$file"); \
			tokens=$$((bytes / 4)); \
			if [ $$tokens -gt 12000 ]; then \
				printf "  ✗ %-50s %6d tokens (EXCEEDS LIMIT by %d)\n" "$$file" "$$tokens" "$$((tokens - 12000))"; \
				FAILED=$$((FAILED + 1)); \
			else \
				remaining=$$((12000 - tokens)); \
				printf "  ✓ %-50s %6d tokens (%d remaining)\n" "$$file" "$$tokens" "$$remaining"; \
			fi; \
		fi; \
	done; \
	echo ""; \
	if [ $$FAILED -gt 0 ]; then \
		echo "File validation FAILED: $$FAILED of $$TOTAL files exceed 12000 token limit"; \
		echo ""; \
		echo "Files that are too large will cause issues:"; \
		echo "  - Bedrock Titan embeddings may fail or truncate"; \
		echo "  - Vector search quality degrades with large chunks"; \
		echo "  - Claude RAG context window limitations"; \
		echo ""; \
		echo "Solutions:"; \
		echo "  1. Split large files into smaller focused documents"; \
		echo "  2. For Terraform: group related resources into separate files"; \
		echo "  3. Use docs/lambda-*.md pattern for function-specific docs"; \
		echo "  4. Create subdirectories for related content"; \
		echo ""; \
		exit 1; \
	else \
		echo "All $$TOTAL files are within size limits ✓"; \
	fi

# ============================================================================
# Testing: Lambda Functions (Unit, Integration, Coverage)
# Following the Test Pyramid approach from ADR-003:
# - Unit tests: fast, many, isolated
# - Integration tests: moderate speed, fewer, testing interactions
# - Functional tests: slowest, limited scope
# ============================================================================

test: test-unit
	@echo "All Lambda tests completed successfully"

test-unit:
	@echo "Running unit tests..."
	$(MAKE) -C lambda/tests test-unit

test-coverage:
	@echo "Running tests with coverage report..."
	$(MAKE) -C lambda/tests test-coverage

test-ui:
	@echo "Running UI tests..."
	@NODE_MAJOR=$$(node -v | sed 's/v\([0-9]*\).*/\1/'); \
	if [ "$$NODE_MAJOR" -ge 20 ] 2>/dev/null; then \
		echo "Node $$NODE_MAJOR detected, running with coverage..."; \
		cd ui && npm run test:coverage; \
	else \
		echo "Node $$NODE_MAJOR detected (<20), running without coverage (coverage requires Node 20+)..."; \
		cd ui && npm test; \
	fi

# ============================================================================
# Security: Dependency Auditing
# ============================================================================

# `make audit` runs BOTH the UI + Python audits. Use audit-ui or audit-py
# to run just one when you only touched one side of the tree.
audit: audit-ui audit-py

audit-ui:
	@echo "Running npm audit on UI dependencies (high+, with allowlist)..."
	@# Allowlist -- advisories NOT applicable to this deploy AND with no available
	@# fix. Keep it tiny + justified; any other high/critical fails the gate.
	@#   GHSA-qwww-vcr4-c8h2  react-router RSC-mode CSRF. This UI is a client SPA
	@#   (BrowserRouter/Routes, no RSC/server mode) so the vulnerable endpoint is
	@#   never served. react-router-dom has NO fixed 7.x (only a downgrade to
	@#   7.11.0, which reintroduces the 7.18.0 DoS fix) and no 8.x line exists for
	@#   -dom. Revisit if react-router-dom ships a patched 7.x.
	@cd ui && r=$$(npm audit --json | jq -c '[.vulnerabilities[] | select(.severity=="high" or .severity=="critical") | .via[] | select(type=="object") | .url | sub(".*/"; "")] | unique - ["GHSA-qwww-vcr4-c8h2"]'); \
	if [ "$$r" = "[]" ]; then echo "npm audit OK (only allowlisted advisories remain)."; else echo "FAIL: high/critical advisories beyond allowlist: $$r"; npm audit --audit-level=high; exit 1; fi

# Fast parallel pip-audit across every lambda/*/requirements.txt.
# Serial `pip-audit -r <file>` on 58 requirements files takes 5-15
# minutes (each call resolves deps + hits the OSV DB). 8-way parallel
# closes it to under a minute. -not paths skip venvs and *-dev.txt
# to keep the scan on production surfaces only.
audit-py:
	@echo "Running pip-audit on lambda/*/requirements.txt (8-way parallel)..."
	@if ! command -v pip-audit >/dev/null 2>&1; then \
		echo "ERROR: pip-audit not on PATH. Install with: pip install pip-audit"; \
		exit 1; \
	fi
	@# --strict off: tree-sitter-abap 0.0.1 is installed from a private
	@# source (git/local), not PyPI, so pip-audit can't look it up. That's
	@# expected, not a vulnerability. Without --strict, unresolvable deps
	@# get a warning line instead of failing the whole scan. Real vulns
	@# still show up as "found N known vulnerabilities" and exit non-zero.
	@find lambda -name "requirements*.txt" -type f \
		-not -path "*/.venv/*" \
		-not -path "*/venv/*" \
		-not -path "*/__pycache__/*" \
		-not -name "requirements-dev.txt" \
	| xargs -P 8 -I {} sh -c \
	    'echo "=== {} ==="; pip-audit -r {} 2>&1 || echo "FAIL: {}"' \
	; echo "audit-py done."

# ============================================================================
# HA Operator Tooling
#
# Wrapper targets around scripts/ha-*. ENV is the only required input
# (no default — different envs have different tfvars). PRIMARY,
# SECONDARY, and APP are resolved automatically from
# terraform/${ENV}.tfvars; pass them as env vars to override.
#
# Usage:
#     ENV=prd make ha-status
#     ENV=prd make ha-list-secrets
#     ENV=prd ARGS="--execute" make ha-sync-secrets
#     ENV=prd ARGS="--dry-run" make ha-bootstrap
#     ENV=prd ARGS="--plan" make ha-kms-migrate
#     ENV=prd REGION=us-east-2 QUEUE=... make ha-replay-dlq
# ============================================================================

HA_TFVARS = terraform/$(ENV).tfvars

# awk -F'"' splits each line on double-quote. The pattern matches an
# uncommented `key = "value"` line (^[[:space:]]* ensures # is not at
# col 0). The print emits the captured value; `exit` stops after the
# first match.
HA_PRIMARY   := $(or $(PRIMARY),$(shell awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' $(HA_TFVARS) 2>/dev/null))
HA_SECONDARY := $(or $(SECONDARY),$(shell awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' $(HA_TFVARS) 2>/dev/null))
HA_APP       := $(or $(APP),$(shell awk -F'"' '/^[[:space:]]*app_name[[:space:]]*=/ {print $$2; exit}' $(HA_TFVARS) 2>/dev/null),outcome-ops-ai-assist)

_ha-env-check:
	@if [ -z "$(ENV)" ]; then \
		echo "ERROR: ENV is required. Example: make ha-status ENV=prd" >&2; \
		exit 2; \
	fi
	@if [ ! -f "$(HA_TFVARS)" ]; then \
		echo "ERROR: $(HA_TFVARS) not found. Either create it or pass PRIMARY/SECONDARY explicitly." >&2; \
		exit 2; \
	fi
	@if [ -z "$(HA_PRIMARY)" ]; then \
		echo "ERROR: could not resolve primary region. Set PRIMARY=... or add aws_region in $(HA_TFVARS)." >&2; \
		exit 2; \
	fi

ha-status: _ha-env-check
	@ENV=$(ENV) APP=$(HA_APP) PRIMARY=$(HA_PRIMARY) SECONDARY=$(HA_SECONDARY) ./scripts/ha-status

ha-list-secrets: _ha-env-check
	@ENV=$(ENV) APP=$(HA_APP) PRIMARY=$(HA_PRIMARY) SECONDARY=$(HA_SECONDARY) ./scripts/ha-list-secrets

ha-sync-secrets: _ha-env-check
	@ENV=$(ENV) APP=$(HA_APP) PRIMARY=$(HA_PRIMARY) SECONDARY=$(HA_SECONDARY) ./scripts/ha-sync-secrets $(ARGS)

ha-bootstrap: _ha-env-check
	@ENV=$(ENV) APP=$(HA_APP) PRIMARY=$(HA_PRIMARY) SECONDARY=$(HA_SECONDARY) ./scripts/ha-bootstrap.py $(ARGS)

ha-kms-migrate: _ha-env-check
	@ENV=$(ENV) APP=$(HA_APP) PRIMARY=$(HA_PRIMARY) SECONDARY=$(HA_SECONDARY) ./scripts/ha-kms-migrate.py $(ARGS)

ha-replay-dlq: _ha-env-check
	@if [ -z "$(REGION)" ] || [ -z "$(QUEUE)" ]; then \
		echo "ERROR: ha-replay-dlq requires REGION and QUEUE. Example:" >&2; \
		echo "  ENV=prd REGION=us-east-2 QUEUE=prd-outcome-ops-ai-assist-replication-applier-dlq make ha-replay-dlq" >&2; \
		exit 2; \
	fi
	@ENV=$(ENV) APP=$(HA_APP) REGION=$(REGION) QUEUE=$(QUEUE) ./scripts/ha-replay-dlq $(ARGS)

# ============================================================================
# Container Images
# Build before 'terraform apply' for container-based Lambdas
# ============================================================================

# Build run-tests container image for multi-language test execution
# Automatically creates ECR repository if it doesn't exist (terraform targeted apply)
# Usage: ENVIRONMENT=dev make build-run-tests-image
#        ENVIRONMENT=prd make build-run-tests-image
build-run-tests-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-run-tests-image"; \
		echo "       ENVIRONMENT=prd make build-run-tests-image"; \
		exit 1; \
	fi
	@echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"
	@cd terraform && \
		terraform init -input=false > /dev/null 2>&1 && \
		(terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
		terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.run_tests -auto-approve
	@echo ""
	@echo "Building run-tests container image for environment: $$ENVIRONMENT"
	@AR=$$(awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 AWS_REGION=$${AR:-$(AWS_REGION)} ./scripts/build-run-tests-image.sh --env $$ENVIRONMENT

# Build chat UI container image (nginx + React)
# Auto-detects secondary_region in ${ENVIRONMENT}.tfvars and, when set,
# also bootstraps the secondary-region ECR repo and pushes the same
# image to it (single build, dual push). Required for HA bootstrap so
# the secondary UI Fargate can pull on first task start.
# Usage: ENVIRONMENT=dev make build-ui-image
#        ENVIRONMENT=prd make build-ui-image
build-ui-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-ui-image"; \
		echo "       ENVIRONMENT=prd make build-ui-image"; \
		exit 1; \
	fi
	@DEPLOY_UI=$$(awk -F'=' '/^[[:space:]]*deploy_ui[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 if [ "$$DEPLOY_UI" = "false" ]; then \
	   echo "Skipping build-ui-image — deploy_ui=false in terraform/$$ENVIRONMENT.tfvars (headless deploy)."; \
	   exit 0; \
	 fi
	@echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"
	@SR=$$(awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 EXTRA=""; \
	 if [ -n "$$SR" ]; then \
	   EXTRA="-target=aws_ecr_repository.chat_ui_secondary"; \
	   echo "HA enabled (secondary_region=$$SR); bootstrapping secondary ECR too."; \
	 fi; \
	 cd terraform && \
	   terraform init -input=false > /dev/null 2>&1 && \
	   (terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
	   terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.chat_ui $$EXTRA -auto-approve
	@echo ""
	@echo "Building chat UI container image for environment: $$ENVIRONMENT"
	@AR=$$(awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 SR=$$(awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 AWS_REGION=$${AR:-$(AWS_REGION)} SECONDARY_REGION=$$SR ./scripts/build-ui-image.sh --env $$ENVIRONMENT

# Build OutcomeOps MCP server container image
# Auto-detects secondary_region in ${ENVIRONMENT}.tfvars and, when set,
# also bootstraps the secondary-region ECR repo and pushes the same
# image to it. The secondary MCP Fargate (HA mode) pulls from its own
# region's registry.
# Usage: ENVIRONMENT=dev make build-mcp-server-image
#        ENVIRONMENT=prd make build-mcp-server-image
build-mcp-server-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-mcp-server-image"; \
		echo "       ENVIRONMENT=prd make build-mcp-server-image"; \
		exit 1; \
	fi
	@MCP_ENABLED=$$(awk -F'=' '/^[[:space:]]*mcp_server_enabled[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 if [ "$$MCP_ENABLED" = "false" ]; then \
	   echo "Skipping build-mcp-server-image — mcp_server_enabled=false in terraform/$$ENVIRONMENT.tfvars."; \
	   exit 0; \
	 fi
	@echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"
	@SR=$$(awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 EXTRA=""; \
	 if [ -n "$$SR" ]; then \
	   EXTRA="-target=aws_ecr_repository.mcp_outcomeops_secondary"; \
	   echo "HA enabled (secondary_region=$$SR); bootstrapping secondary ECR too."; \
	 fi; \
	 cd terraform && \
	   terraform init -input=false > /dev/null 2>&1 && \
	   (terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
	   terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.mcp_outcomeops $$EXTRA -auto-approve
	@echo ""
	@echo "Building OutcomeOps MCP server container image for environment: $$ENVIRONMENT"
	$(eval ECR_REPO := $(shell cd terraform && terraform output -raw mcp_server_ecr_url 2>/dev/null))
	$(eval ECR_REPO_SECONDARY := $(shell cd terraform && terraform output -raw mcp_server_ecr_url_secondary 2>/dev/null))
	$(eval AWS_ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text))
	$(eval BUILD_SECONDARY_REGION := $(shell awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' terraform/$(ENVIRONMENT).tfvars 2>/dev/null))
	$(eval BUILD_AWS_REGION := $(shell awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' terraform/$(ENVIRONMENT).tfvars 2>/dev/null))
	$(eval BUILD_AWS_REGION := $(or $(BUILD_AWS_REGION),$(AWS_REGION)))
	$(eval MCP_IMAGE_TAG := $(shell date +%Y%m%d-%H%M%S)-$(shell git rev-parse --short HEAD 2>/dev/null || echo unknown))
	@if [ -z "$(ECR_REPO)" ] || [ "$(ECR_REPO)" = "null" ]; then \
	   echo "Skipping build-mcp-server-image — mcp_server_ecr_url output is empty (mcp_server_enabled=false?)."; \
	   exit 0; \
	 fi; \
	 aws ecr get-login-password --region $(BUILD_AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT).dkr.ecr.$(BUILD_AWS_REGION).amazonaws.com && \
	 docker build -f lambda/mcp-server/Dockerfile -t $(ECR_REPO):$(MCP_IMAGE_TAG) lambda/ && \
	 docker push $(ECR_REPO):$(MCP_IMAGE_TAG) && \
	 echo "Storing image tag in SSM ($(BUILD_AWS_REGION)): /$(ENVIRONMENT)/$(APP_NAME)/mcp-outcomeops/image-tag = $(MCP_IMAGE_TAG)" && \
	 aws ssm put-parameter --name "/$(ENVIRONMENT)/$(APP_NAME)/mcp-outcomeops/image-tag" --value "$(MCP_IMAGE_TAG)" --type String --overwrite --region $(BUILD_AWS_REGION) > /dev/null
	@if [ -n "$(BUILD_SECONDARY_REGION)" ] && [ -n "$(ECR_REPO_SECONDARY)" ] && [ "$(ECR_REPO_SECONDARY)" != "null" ]; then \
	   echo ""; \
	   echo "HA multi-region push: ${BUILD_SECONDARY_REGION}"; \
	   aws ecr get-login-password --region $(BUILD_SECONDARY_REGION) \
	     | docker login --username AWS --password-stdin $(AWS_ACCOUNT).dkr.ecr.$(BUILD_SECONDARY_REGION).amazonaws.com; \
	   docker tag $(ECR_REPO):$(MCP_IMAGE_TAG) $(ECR_REPO_SECONDARY):$(MCP_IMAGE_TAG); \
	   docker push $(ECR_REPO_SECONDARY):$(MCP_IMAGE_TAG); \
	   aws ssm put-parameter --name "/$(ENVIRONMENT)/$(APP_NAME)/mcp-outcomeops/image-tag" --value "$(MCP_IMAGE_TAG)" --type String --overwrite --region $(BUILD_SECONDARY_REGION) > /dev/null; \
	 fi

# Build Snyk MCP server container image
# Usage: ENVIRONMENT=dev make build-snyk-image
#        ENVIRONMENT=prd make build-snyk-image
build-snyk-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-snyk-image"; \
		echo "       ENVIRONMENT=prd make build-snyk-image"; \
		exit 1; \
	fi
	@SNYK_ENABLED=$$(awk -F'=' '/^[[:space:]]*mcp_snyk_enabled[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 if [ "$$SNYK_ENABLED" != "true" ]; then \
	   echo "Skipping build-snyk-image — mcp_snyk_enabled=$$SNYK_ENABLED in terraform/$$ENVIRONMENT.tfvars (only builds when true)."; \
	   exit 0; \
	 fi; \
	 echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"; \
	 cd terraform && \
	   terraform init -input=false > /dev/null 2>&1 && \
	   (terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
	   terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.mcp_snyk -auto-approve
	@SNYK_ENABLED=$$(awk -F'=' '/^[[:space:]]*mcp_snyk_enabled[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 [ "$$SNYK_ENABLED" != "true" ] && exit 0; \
	 echo ""; \
	 echo "Building Snyk MCP server container image for environment: $$ENVIRONMENT"
	$(eval ECR_REPO := $(shell cd terraform && terraform output -raw mcp_snyk_ecr_url 2>/dev/null))
	$(eval AWS_ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text))
	$(eval BUILD_AWS_REGION := $(shell awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' terraform/$(ENVIRONMENT).tfvars 2>/dev/null))
	$(eval BUILD_AWS_REGION := $(or $(BUILD_AWS_REGION),$(AWS_REGION)))
	@if [ -z "$(ECR_REPO)" ] || [ "$(ECR_REPO)" = "null" ]; then \
	   echo "Skipping build-snyk-image — mcp_snyk_ecr_url output is empty (mcp_snyk_enabled=false?)."; \
	   exit 0; \
	 fi; \
	 aws ecr get-login-password --region $(BUILD_AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT).dkr.ecr.$(BUILD_AWS_REGION).amazonaws.com && \
	 docker build -t $(ECR_REPO):latest containers/mcp-snyk/ && \
	 docker push $(ECR_REPO):latest

build-sonarqube-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-sonarqube-image"; \
		echo "       ENVIRONMENT=prd make build-sonarqube-image"; \
		exit 1; \
	fi
	@SQ_ENABLED=$$(awk -F'=' '/^[[:space:]]*mcp_sonarqube_enabled[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 if [ "$$SQ_ENABLED" != "true" ]; then \
	   echo "Skipping build-sonarqube-image — mcp_sonarqube_enabled=$$SQ_ENABLED in terraform/$$ENVIRONMENT.tfvars (only builds when true)."; \
	   exit 0; \
	 fi; \
	 echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"; \
	 cd terraform && \
	   terraform init -input=false > /dev/null 2>&1 && \
	   (terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
	   terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.mcp_sonarqube -auto-approve
	@SQ_ENABLED=$$(awk -F'=' '/^[[:space:]]*mcp_sonarqube_enabled[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 [ "$$SQ_ENABLED" != "true" ] && exit 0; \
	 echo ""; \
	 echo "Building SonarQube MCP server container image for environment: $$ENVIRONMENT"
	$(eval ECR_REPO := $(shell cd terraform && terraform output -raw mcp_sonarqube_ecr_url 2>/dev/null))
	$(eval AWS_ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text))
	$(eval BUILD_AWS_REGION := $(shell awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' terraform/$(ENVIRONMENT).tfvars 2>/dev/null))
	$(eval BUILD_AWS_REGION := $(or $(BUILD_AWS_REGION),$(AWS_REGION)))
	@if [ -z "$(ECR_REPO)" ] || [ "$(ECR_REPO)" = "null" ]; then \
	   echo "Skipping build-sonarqube-image — mcp_sonarqube_ecr_url output is empty (mcp_sonarqube_enabled=false?)."; \
	   exit 0; \
	 fi; \
	 aws ecr get-login-password --region $(BUILD_AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT).dkr.ecr.$(BUILD_AWS_REGION).amazonaws.com && \
	 docker build -t $(ECR_REPO):latest containers/mcp-sonarqube/ && \
	 docker push $(ECR_REPO):latest

# ============================================================================
# Lambda layers — installed packages / binaries published as Lambda layer
# versions, distinct from container images. Each script builds inside the
# Amazon Linux 2 / Python 3.12 image so the binaries are ABI-compatible
# with the Lambda runtime. The layer is then packaged + published by
# terraform on the next apply.
# ============================================================================

# Build the runtime tools layer (git, make, tar, etc.) used by generate-code.
build-runtime-layer:
	./scripts/build-runtime-layer.sh

# Build the terraform CLI layer used by generate-code for fmt/validate.
build-terraform-layer:
	./scripts/build-terraform-layer.sh

# Build all Lambda layers in one shot. Run before `terraform apply` after
# updating any layer's requirements.txt or build script.
build-all-layers:
	./scripts/build-all-layers.sh

# Build and push the database integration + sync container images.
# These ship as container images (not Lambda layers) because the schema
# introspection engine + DB drivers total well over Lambda's 250 MB unzipped
# layer cap. Container images cap at 10 GB so there's plenty of headroom.
# Usage:
#   ENVIRONMENT=dev make build-database-images
#   ENVIRONMENT=prd make build-database-images
build-database-images:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-database-images"; \
		echo "       ENVIRONMENT=prd make build-database-images"; \
		exit 1; \
	fi
	@WS_ENABLED=$$(awk -F'=' '/^[[:space:]]*enable_workspaces[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 DB_ENABLED=$$(awk -F'=' '/^[[:space:]]*enable_database_integration[[:space:]]*=/ {gsub(/[[:space:]]/, "", $$2); print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 if [ "$$WS_ENABLED" = "false" ] || [ "$$DB_ENABLED" != "true" ]; then \
	   echo "Skipping build-database-images — needs BOTH enable_workspaces (not false) AND enable_database_integration=true in terraform/$$ENVIRONMENT.tfvars (the DB ECR repos + Lambda only exist then)."; \
	   exit 0; \
	 fi
	@echo "Ensuring ECR repositories exist for environment: $$ENVIRONMENT"
	@SR=$$(awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 EXTRA=""; \
	 if [ -n "$$SR" ]; then \
	   EXTRA="-target=aws_ecr_repository.database_integration_secondary -target=aws_ecr_repository.database_sync_secondary"; \
	   echo "HA enabled (secondary_region=$$SR); bootstrapping secondary ECR repos too."; \
	 fi; \
	 cd terraform && \
	   terraform init -input=false > /dev/null 2>&1 && \
	   (terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
	   terraform apply -var-file=$$ENVIRONMENT.tfvars \
	     -target=aws_ecr_repository.database_integration \
	     -target=aws_ecr_repository.database_sync \
	     $$EXTRA \
	     -auto-approve
	@echo ""
	@echo "Building database container images for environment: $$ENVIRONMENT"
	@AR=$$(awk -F'"' '/^[[:space:]]*aws_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 SR=$$(awk -F'"' '/^[[:space:]]*secondary_region[[:space:]]*=/ {print $$2; exit}' terraform/$$ENVIRONMENT.tfvars 2>/dev/null); \
	 AWS_REGION=$${AR:-$(AWS_REGION)} SECONDARY_REGION=$$SR ./scripts/build-database-images.sh --env $$ENVIRONMENT

# Build the platform-CA Lambda Layer (Mozilla certifi + customer roots from
# platform_ca_bundle_ssm_param_arns). The layer is attached to every platform
# Lambda that egresses to the UI ALB (chat, workspace-management). Always
# succeeds: empty platform_ca_bundle_ssm_param_arns produces a passthrough
# layer that ships only the certifi roots, so layer-attachment stays
# uniform between BYO-internal-CA and default-public-cert deploys. See
# scripts/build-platform-ca-layer.sh + terraform/lambda-platform-ca-layer.tf.
build-platform-layer:
	./scripts/build-platform-ca-layer.sh

# Build every customer-image container in one shot. Each underlying target
# already validates ENVIRONMENT and ensures its own ECR repo, so this is
# just an ordering wrapper -- a field engineer should not have to remember
# the full per-container list. Parallels build-all-layers.
build-all-images:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-all-images"; \
		echo "       ENVIRONMENT=prd make build-all-images"; \
		exit 1; \
	fi
	ENVIRONMENT=$$ENVIRONMENT $(MAKE) build-run-tests-image
	ENVIRONMENT=$$ENVIRONMENT $(MAKE) build-ui-image
	ENVIRONMENT=$$ENVIRONMENT $(MAKE) build-mcp-server-image
	ENVIRONMENT=$$ENVIRONMENT $(MAKE) build-snyk-image
	ENVIRONMENT=$$ENVIRONMENT $(MAKE) build-database-images

# Single catch-all before `terraform apply`. Builds every Lambda layer,
# every container image, and the platform-CA layer. Idempotent (each
# underlying target skips when nothing changed). Run once after any code
# or tfvars change before applying terraform. Replaces the field
# engineer's prior 5-7 separate make invocations -- see
# docs/getting-started.md. CI keeps its per-image steps for parallelism +
# per-step logs; pre-deploy is the human-driven shortcut.
pre-deploy: build-all-layers build-all-images
	@echo ""
	@echo "All pre-terraform builds complete. Next: terraform apply."

# ============================================================================
# Delivery: ship a HISTORY-FREE snapshot to a customer
# ============================================================================
# Scrubbed customer names live only in git HISTORY; delivering a history-free
# snapshot means a customer never receives it. Default: a tarball in dist/.
#   make deliver                                   # tarball of HEAD -> dist/
#   make deliver REF=origin/main                   # tarball of a specific ref
#   make deliver CUSTOMER_REMOTE=<git-url>         # push ONE orphan commit to
#                                                  # the customer's repo (main)
# The script refuses to ship if any scrubbed name is still in the delivered tree.
deliver:
	@bash scripts/build-delivery-snapshot.sh

# ============================================================================
# Utilities: Clean up build artifacts
# ============================================================================

clean:
	@echo "Cleaning build artifacts..."
	$(MAKE) -C lambda/tests clean
	rm -rf terraform/.terraform
	rm -rf terraform/*.out
	rm -rf terraform/.terraform.lock.hcl
	@echo "Clean complete"

# ============================================================================
# Combined: Run complete build pipeline
# Format → Validate → Test
# ============================================================================

all: fmt validate test
	@echo ""
	@echo "Build pipeline complete:"
	@echo "  ✓ Terraform code formatted"
	@echo "  ✓ Terraform configuration validated"
	@echo "  ✓ All Lambda tests passed"
