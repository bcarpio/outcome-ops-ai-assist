.PHONY: help setup install fmt lint validate test test-unit test-integration test-coverage code-maps-load clean all

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
	@echo "  make code-maps-load   Generate code maps for all repos (0-day load)"
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

code-maps-load:
	@echo "Generating code maps for all repos (0-day load)..."
	@if [ -z "$$ENV" ]; then \
		echo "Error: ENV variable not set. Usage: ENV=dev make code-maps-load"; \
		exit 1; \
	fi
	@echo "Environment: $$ENV"
	python3 scripts/invoke-code-maps-per-repo.py

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
