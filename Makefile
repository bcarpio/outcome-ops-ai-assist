.PHONY: help setup install fmt lint validate test test-unit test-integration test-coverage ingest-docs ingest-docs-repo generate-code-maps generate-code-maps-repo code-maps-load build-runtime-image clean all

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
	@echo ""
	@echo "Data Loading:"
	@echo "  make ingest-docs           Ingest ADRs, READMEs, and docs/ from all repos"
	@echo "  make ingest-docs-repo      Ingest docs from single repo (REPO=name)"
	@echo "  make generate-code-maps    Generate code maps for all application repos"
	@echo "  make generate-code-maps-repo   Generate code maps for single repo (REPO=name)"
	@echo "  make code-maps-load        (deprecated) Use generate-code-maps instead"
	@echo ""
	@echo "Runtime Container:"
	@echo "  ENVIRONMENT=dev make build-runtime-image   Build & push Lambda runtime image to ECR"
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
	venv/bin/pip install -q -r lambda/ingest-docs/requirements.txt
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
	@echo "Linting markdown files for embedding size limits..."
	@echo "Token limit: 7000 tokens (~28KB file size)"
	@echo "Estimate: 1 token ≈ 4 bytes (rough approximation)"
	@echo ""
	@FAILED=0; \
	TOTAL=0; \
	for file in README.md docs/*.md docs/adr/*.md; do \
		if [ -f "$$file" ]; then \
			TOTAL=$$((TOTAL + 1)); \
			bytes=$$(wc -c < "$$file"); \
			tokens=$$((bytes / 4)); \
			if [ $$tokens -gt 7000 ]; then \
				printf "  ✗ %-50s %6d tokens (EXCEEDS LIMIT by %d)\n" "$$file" "$$tokens" "$$((tokens - 7000))"; \
				FAILED=$$((FAILED + 1)); \
			else \
				remaining=$$((7000 - tokens)); \
				printf "  ✓ %-50s %6d tokens (%d remaining)\n" "$$file" "$$tokens" "$$remaining"; \
			fi; \
		fi; \
	done; \
	echo ""; \
	if [ $$FAILED -gt 0 ]; then \
		echo "Documentation validation FAILED: $$FAILED of $$TOTAL files exceed 7000 token limit"; \
		echo ""; \
		echo "Files that are too large will cause issues:"; \
		echo "  - Bedrock Titan embeddings may fail or truncate"; \
		echo "  - Vector search quality degrades with large chunks"; \
		echo "  - Claude RAG context window limitations"; \
		echo ""; \
		echo "Solutions:"; \
		echo "  1. Split large files into smaller focused documents"; \
		echo "  2. Move detailed content to separate files and link them"; \
		echo "  3. Use docs/lambda-*.md pattern for function-specific docs"; \
		echo "  4. Create subdirectories for related content"; \
		echo ""; \
		exit 1; \
	else \
		echo "All $$TOTAL markdown files are within size limits ✓"; \
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

# ============================================================================
# Data Loading: Generate code maps and ingest documentation
# ============================================================================

ingest-docs:
	@echo "Ingesting documentation from all repos..."
	@if [ -z "$$ENVIRONMENT" ]; then \
		ENVIRONMENT=dev; \
	fi; \
	echo "Environment: $$ENVIRONMENT"; \
	ENVIRONMENT=$$ENVIRONMENT ./scripts/outcome-ops-assist ingest-docs

ingest-docs-repo:
	@if [ -z "$$REPO" ]; then \
		echo "Error: REPO variable not set. Usage: REPO=outcome-ops-ai-assist make ingest-docs-repo"; \
		exit 1; \
	fi
	@echo "Ingesting documentation from: $$REPO"
	@if [ -z "$$ENVIRONMENT" ]; then \
		ENVIRONMENT=dev; \
	fi; \
	echo "Environment: $$ENVIRONMENT"; \
	ENVIRONMENT=$$ENVIRONMENT ./scripts/outcome-ops-assist ingest-docs $$REPO

generate-code-maps:
	@echo "Generating code maps for all application repos..."
	@if [ -z "$$ENVIRONMENT" ]; then \
		ENVIRONMENT=dev; \
	fi; \
	echo "Environment: $$ENVIRONMENT"; \
	ENVIRONMENT=$$ENVIRONMENT ./scripts/outcome-ops-assist generate-code-maps

generate-code-maps-repo:
	@if [ -z "$$REPO" ]; then \
		echo "Error: REPO variable not set. Usage: REPO=outcome-ops-ai-assist make generate-code-maps-repo"; \
		exit 1; \
	fi
	@echo "Generating code maps for: $$REPO"
	@if [ -z "$$ENVIRONMENT" ]; then \
		ENVIRONMENT=dev; \
	fi; \
	echo "Environment: $$ENVIRONMENT"; \
	ENVIRONMENT=$$ENVIRONMENT ./scripts/outcome-ops-assist generate-code-maps $$REPO

# Deprecated - use generate-code-maps instead
code-maps-load:
	@echo "WARNING: code-maps-load is deprecated. Use 'make generate-code-maps' instead."
	@echo ""
	$(MAKE) generate-code-maps

# ============================================================================
# Runtime Container: Build and push Lambda runtime image
# ============================================================================

build-runtime-image:
	@if [ -z "$$ENVIRONMENT" ]; then \
		echo "Error: ENVIRONMENT variable not set. Usage: ENVIRONMENT=dev make build-runtime-image"; \
		exit 1; \
	fi
	@ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text); \
	if [ -z "$$ACCOUNT_ID" ]; then \
		echo "Error: Unable to determine AWS account ID. Ensure AWS CLI is configured."; \
		exit 1; \
	fi; \
	REPO_NAME="$$ENVIRONMENT-$(APP_NAME)-runtime"; \
	IMAGE_URI="$$ACCOUNT_ID.dkr.ecr.$(AWS_REGION).amazonaws.com/$$REPO_NAME"; \
	IMAGE_TAG=$${IMAGE_TAG:-$$(git rev-parse --short HEAD)}; \
	echo "Using image URI $$IMAGE_URI with tag $$IMAGE_TAG"; \
	if ! aws ecr describe-repositories --repository-names $$REPO_NAME --region $(AWS_REGION) >/dev/null 2>&1; then \
		echo "Error: ECR repository $$REPO_NAME not found in region $(AWS_REGION). Deploy Terraform first."; \
		exit 1; \
	fi; \
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $$ACCOUNT_ID.dkr.ecr.$(AWS_REGION).amazonaws.com; \
	docker build -f lambda/runtime-container/Dockerfile \
		-t $$IMAGE_URI:$$IMAGE_TAG .; \
	docker push $$IMAGE_URI:$$IMAGE_TAG; \
	mkdir -p dist; \
	echo "$$IMAGE_URI:$$IMAGE_TAG" > dist/runtime-image-uri.txt; \
	echo "Runtime image pushed to $$IMAGE_URI:$$IMAGE_TAG"; \
	echo ""; \
	echo "To deploy this image, update terraform/variables.tf:"; \
	echo "  runtime_image_tag = \"$$IMAGE_TAG\""

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
