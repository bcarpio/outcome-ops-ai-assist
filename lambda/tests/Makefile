.PHONY: test test-unit test-integration test-coverage clean

# Install dependencies (from repo root)
install:
	pip install -r ../ingest-docs/requirements.txt

# Run all tests
test:
	pytest . -v

# Run only unit tests
test-unit:
	pytest unit/ -v

# Run only integration tests
test-integration:
	pytest integration/ -v

# Run tests with coverage report
test-coverage:
	pytest . --cov=../ingest-docs --cov-report=html --cov-report=term-missing

# Clean up test artifacts
clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '.pytest_cache' -delete
	find . -type d -name '.coverage' -delete
	find . -type d -name 'htmlcov' -delete
