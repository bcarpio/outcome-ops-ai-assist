.PHONY: test test-unit test-integration test-coverage clean

# Python interpreter (use venv if it exists, otherwise system python)
PYTHON := $(if $(wildcard ../../venv/bin/python),../../venv/bin/python,python3.12)
PYTEST := $(if $(wildcard ../../venv/bin/pytest),../../venv/bin/pytest,pytest)

# Install dependencies (from repo root)
install:
	$(PYTHON) -m pip install -q -r ../ingest-docs/requirements.txt

# Run all tests
test:
	$(PYTEST) . -v

# Run only unit tests
test-unit:
	$(PYTEST) unit/ -v

# Run only integration tests
test-integration:
	$(PYTEST) integration/ -v

# Run tests with coverage report
test-coverage:
	$(PYTEST) . --cov=../ingest-docs --cov-report=html --cov-report=term-missing

# Clean up test artifacts
clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '.pytest_cache' -delete
	find . -type d -name '.coverage' -delete
	find . -type d -name 'htmlcov' -delete
