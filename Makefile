.PHONY: help setup install fmt lint validate test test-unit test-integration test-coverage test-ui audit build-run-tests-image build-ui-image build-mcp-server-image build-snyk-image build-database-images build-runtime-layer build-terraform-layer build-all-layers build-platform-layer build-all-images pre-deploy clean all ha-status ha-replay-dlq ha-list-secrets ha-bootstrap

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
	@echo "  make test-integration Run only integration tests"
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

validate: validate-tf validate-docs
	@echo ""
	@echo "All validation checks passed ✓"

validate-tf:
	@echo "Validating terraform configuration..."
	cd terraform && terraform validate

validate-docs:
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

test: test-unit test-integration
	@echo "All Lambda tests completed successfully"

test-unit:
	@echo "Running unit tests..."
	$(MAKE) -C lambda/tests test-unit

test-integration:
	@echo "Running integration tests..."
	$(MAKE) -C lambda/tests test-integration

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

audit:
	@echo "Running npm audit on UI dependencies..."
	cd ui && npm audit --audit-level=high

# ============================================================================
# HA Operator Tooling
# Wrapper targets around scripts/ha-* for operator convenience.
# Required env vars: ENV, APP, PRIMARY, SECONDARY (optional for status).
# ============================================================================

ha-status:
	@./scripts/ha-status

ha-replay-dlq:
	@./scripts/ha-replay-dlq

ha-list-secrets:
	@./scripts/ha-list-secrets

ha-bootstrap:
	@./scripts/ha-bootstrap.py $(ARGS)

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
	./scripts/build-run-tests-image.sh --env $$ENVIRONMENT

# Build chat UI container image (nginx + React)
# Usage: ENVIRONMENT=dev make build-ui-image
#        ENVIRONMENT=prd make build-ui-image
build-ui-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-ui-image"; \
		echo "       ENVIRONMENT=prd make build-ui-image"; \
		exit 1; \
	fi
	@echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"
	@cd terraform && \
		terraform init -input=false > /dev/null 2>&1 && \
		(terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
		terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.chat_ui -auto-approve
	@echo ""
	@echo "Building chat UI container image for environment: $$ENVIRONMENT"
	./scripts/build-ui-image.sh --env $$ENVIRONMENT

# Build OutcomeOps MCP server container image
# Usage: ENVIRONMENT=dev make build-mcp-server-image
#        ENVIRONMENT=prd make build-mcp-server-image
build-mcp-server-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set."; \
		echo "Usage: ENVIRONMENT=dev make build-mcp-server-image"; \
		echo "       ENVIRONMENT=prd make build-mcp-server-image"; \
		exit 1; \
	fi
	@echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"
	@cd terraform && \
		terraform init -input=false > /dev/null 2>&1 && \
		(terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
		terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.mcp_outcomeops -auto-approve
	@echo ""
	@echo "Building OutcomeOps MCP server container image for environment: $$ENVIRONMENT"
	$(eval ECR_REPO := $(shell cd terraform && terraform output -raw mcp_server_ecr_url 2>/dev/null))
	$(eval AWS_ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text))
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT).dkr.ecr.$(AWS_REGION).amazonaws.com
	# Build context is `lambda/` so the image can also COPY shared/
	# for audit-writer access. Dockerfile path is relative to the
	# new context.
	docker build -f lambda/mcp-server/Dockerfile -t $(ECR_REPO):latest lambda/
	docker push $(ECR_REPO):latest

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
	@echo "Ensuring ECR repository exists for environment: $$ENVIRONMENT"
	@cd terraform && \
		terraform init -input=false > /dev/null 2>&1 && \
		(terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
		terraform apply -var-file=$$ENVIRONMENT.tfvars -target=aws_ecr_repository.mcp_snyk -auto-approve
	@echo ""
	@echo "Building Snyk MCP server container image for environment: $$ENVIRONMENT"
	$(eval ECR_REPO := $(shell cd terraform && terraform output -raw mcp_snyk_ecr_url 2>/dev/null))
	$(eval AWS_ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text))
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT).dkr.ecr.$(AWS_REGION).amazonaws.com
	docker build -t $(ECR_REPO):latest containers/mcp-snyk/
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
	@echo "Ensuring ECR repositories exist for environment: $$ENVIRONMENT"
	@cd terraform && \
		terraform init -input=false > /dev/null 2>&1 && \
		(terraform workspace select $$ENVIRONMENT 2>/dev/null || terraform workspace new $$ENVIRONMENT) && \
		terraform apply -var-file=$$ENVIRONMENT.tfvars \
			-target=aws_ecr_repository.database_integration \
			-target=aws_ecr_repository.database_sync \
			-auto-approve
	@echo ""
	@echo "Building database container images for environment: $$ENVIRONMENT"
	./scripts/build-database-images.sh --env $$ENVIRONMENT

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
pre-deploy: build-all-layers build-all-images build-platform-layer
	@echo ""
	@echo "All pre-terraform builds complete. Next: terraform apply."

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
