"""
Pytest configuration and shared fixtures for all tests.

This file is automatically loaded by pytest and makes fixtures
available to all test files.
"""

import sys
import os

# Set AWS region for tests (required by boto3 clients even with moto mocking)
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-2')
os.environ.setdefault('AWS_REGION', 'us-west-2')

# Add parent directory to path so tests can import from lambda modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Add ingest-docs directory to path for integration tests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'ingest-docs')))
