.PHONY: test test-unit test-integration test-coverage clean help

# Python executable from virtual environment
PYTHON := ../../venv/bin/python3.12

help:
	@echo "OutcomeOps AI Assist Lambda Test Suite"
	@echo ""
	@echo "Available commands:"
	@echo "  make test              - Run all tests"
	@echo "  make test-unit         - Run only unit tests"
	@echo "  make test-integration  - Run only integration tests"
	@echo "  make test-coverage     - Run tests with coverage report"
	@echo "  make test-verbose      - Run tests with verbose output"
	@echo "  make clean             - Clean test artifacts"

test:
	@echo "Running all tests..."
	$(PYTHON) -m pytest -v

test-unit:
	@echo "Running unit tests..."
	$(PYTHON) -m pytest unit/ -v

test-integration:
	@echo "Running integration tests..."
	$(PYTHON) -m pytest integration/ -v

test-coverage:
	@echo "Running tests with coverage..."
	$(PYTHON) -m pytest --cov=../vector-query --cov=../ask-claude --cov=../query-kb \
		--cov-report=html --cov-report=term-missing
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
