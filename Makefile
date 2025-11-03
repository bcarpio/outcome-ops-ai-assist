.PHONY: help setup install fmt validate test test-unit test-integration test-coverage code-maps-load clean all

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

validate: validate-tf validate-docs
	@echo "All validation checks passed"

validate-tf:
	@echo "Validating terraform configuration..."
	cd terraform && terraform validate

validate-docs:
	@echo "Validating documentation sizes (must be <7000 tokens for embedding)..."
	@for file in docs/*.md README.md; do \
		if [ -f "$$file" ]; then \
			bytes=$$(wc -c < "$$file"); \
			tokens=$$((bytes / 4)); \
			if [ $$tokens -gt 7000 ]; then \
				echo "  ✗ $$file: $$tokens tokens (exceeds 7000 token limit)"; \
				exit 1; \
			else \
				printf "  ✓ %-40s %5d tokens\n" "$$file:" "$$tokens"; \
			fi; \
		fi; \
	done

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
