.PHONY: test test-unit test-coverage clean help

# Python executable - use venv if available, otherwise use system python
VENV_PYTHON := ../../venv/bin/python3.12
PYTHON := $(shell test -f $(VENV_PYTHON) && echo $(VENV_PYTHON) || echo python3)

help:
	@echo "OutcomeOps AI Assist Lambda Test Suite"
	@echo ""
	@echo "Available commands:"
	@echo "  make test              - Run all tests"
	@echo "  make test-unit         - Run only unit tests"
	@echo "  make test-coverage     - Run tests with coverage report"
	@echo "  make test-verbose      - Run tests with verbose output"
	@echo "  make clean             - Clean test artifacts"

test:
	@echo "Running all tests..."
	$(PYTHON) -m pytest -v

test-unit:
	@echo "Running unit tests..."
	$(PYTHON) -m pytest unit/ -v

# test-coverage excludes the symbol-graph latency benchmark (@pytest.mark.slow):
# coverage instrumentation distorts timing, so its p50/p99 assertions are
# meaningless and flaky under --cov (a loaded runner can even trip the per-test
# timeout). The benchmark still runs un-instrumented via `make test-unit`;
# production latency is policed by the CloudWatch alarm in terraform.
test-coverage:
	@echo "Running tests with coverage..."
	$(PYTHON) -m pytest -m "not slow" \
		--cov=.. --cov-config=.coveragerc \
		--cov-report=html --cov-report=term-missing --cov-report=xml:../coverage.xml --junitxml=../junit.xml
	@echo "Coverage report generated in htmlcov/index.html"

test-verbose:
	@echo "Running tests with verbose output..."
	$(PYTHON) -m pytest -vv --tb=long

test-watch:
	@echo "Running tests in watch mode..."
	$(PYTHON) -m pytest-watch -v

clean:
	@echo "Cleaning test artifacts..."
	rm -rf __pycache__ .pytest_cache htmlcov .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "Clean complete"
