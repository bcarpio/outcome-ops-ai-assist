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

# Fake placeholder credentials so boto3 client construction succeeds in
# environments without a credential source (GH Actions runners, sandboxes
# without ~/.aws/credentials). moto's mock_aws intercepts the actual API
# call but botocore's signature pipeline requires SOME credential to
# exist. These values are never used to authenticate against real AWS.
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ.setdefault('AWS_SESSION_TOKEN', 'testing')
# Belt-and-suspenders: prevent boto3 from falling back to EC2 instance
# metadata if the env-var credentials are somehow ignored.
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')

# Add parent directory to path so tests can import from lambda modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# ---------------------------------------------------------------------------
# Autouse moto wrap — catches unmocked boto3 clients that escape
# per-test MagicMock-replacement patterns.
#
# Reason this exists: several test files (test_file_ingestion_confluence,
# test_chat, test_process_documents, etc.) patch the specific boto3
# clients they expect to be called (s3_client, ssm_client, dynamodb)
# but the handlers under test internally construct OTHER clients
# (comprehend for pii.redact, sts for caller-identity lookups, kms for
# decrypt) that the per-test patches miss. On a dev box with real
# credentials those unmocked calls succeed against real AWS — masking
# the gap. On a GH Actions runner without credentials they fail with
# NoCredentialsError. ADR-012 documents this as the "real-SES-leak"
# pattern; the fix is autouse moto wrapping at the conftest level so
# every test runs inside a moto context regardless of per-test mock
# coverage.
#
# Nested mock_aws() calls (in tests that wrap their own moto context)
# are safe — moto's mock_aws is reentrant.
#
# Function-scoped so each test gets a fresh moto backend state. Slight
# overhead vs session-scoped but avoids per-test state leakage.
# ---------------------------------------------------------------------------

import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _aws_mocks_catch_all(request):
    """Catch-all moto wrap. Per-test explicit moto fixtures are still
    honored — moto's mock_aws is reentrant.

    Skipped for tests that explicitly mark themselves
    ``@pytest.mark.no_aws_mock`` — used for tests that depend on
    boto3 NOT being wrapped (e.g., tests that assert specific
    NoCredentialsError behavior).
    """
    if "no_aws_mock" in request.keywords:
        yield
        return
    with mock_aws():
        yield
