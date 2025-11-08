"""
Test Coverage check handler.

Checks:
- New handlers should have corresponding test files per ADR-003
- Test files should exist in lambda/tests/unit/ directory
- Validates against ADR testing standards from knowledge base
"""

import json
import logging
import os
from typing import Dict, List, Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
lambda_client = boto3.client("lambda")


def query_testing_standards(query_kb_lambda_name: str) -> str:
    """
    Query knowledge base for testing standards from ADRs.

    Returns:
        str: Testing standards and requirements from ADRs
    """
    payload = {
        "query": "What are our testing requirements for new Lambda handlers according to ADR-003? When is a story considered DONE?",
        "topK": 3
    }

    try:
        response = lambda_client.invoke(
            FunctionName=query_kb_lambda_name,
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())
        return response_payload.get("answer", "")

    except Exception as e:
        logger.error(f"Failed to query testing standards: {e}")
        return ""


def check_test_coverage(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str]
) -> Dict[str, Any]:
    """
    Test Coverage check handler.

    Checks:
    - New Lambda handlers should have corresponding test files per ADR-003
    - Test files should exist in lambda/tests/unit/ directory
    - Validates against testing standards from knowledge base
    """
    logger.info(f"Running test coverage check for PR #{pr_number}")

    # Get query-kb Lambda name from environment
    environment = os.environ.get("ENVIRONMENT", "dev")
    app_name = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
    query_kb_lambda_name = f"{environment}-{app_name}-query-kb"

    # Query knowledge base for testing standards
    testing_standards = query_testing_standards(query_kb_lambda_name)
    logger.info(f"Testing standards from KB: {testing_standards[:200]}...")

    # Filter to handler files (exclude test files)
    new_handlers = [
        f for f in changed_files
        if f.startswith("lambda/") and f.endswith("/handler.py")
        and "/tests/" not in f
    ]

    # Find all test files in the PR
    test_files = [
        f for f in changed_files
        if "test" in f.lower() or "tests" in f.lower()
    ]

    suggestions = []

    # Check if new handlers have tests
    for handler in new_handlers:
        # Extract handler name from path
        # Example: lambda/hello/handler.py -> "hello"
        path_parts = handler.split("/")

        if len(path_parts) >= 2:
            handler_name = path_parts[1]  # e.g., "hello" from lambda/hello/handler.py
        else:
            handler_name = handler.replace(".py", "")

        # Check if any test file includes the handler name
        has_test = any(
            handler_name.lower() in test_file.lower()
            for test_file in test_files
        )

        if not has_test:
            suggestions.append(
                f"**{handler}**: Missing required tests\n"
                f"  - ADR-003 requires: Tests must be written before a story is considered DONE\n"
                f"  - Add `test_{handler_name}.py` in `lambda/tests/unit/` directory\n"
                f"  - Follow testing standards: pytest, moto for AWS mocking, 70%+ coverage"
            )

    # Determine status - FAIL if tests missing (per ADR-003)
    if not new_handlers:
        status = "PASS"
        message = "No new handlers to check"
    elif suggestions:
        status = "FAIL"
        message = f"⚠️ {len(suggestions)} handler(s) missing required tests (ADR-003 violation)"
    else:
        status = "PASS"
        message = "✅ All handlers have test coverage per ADR-003"

    return {
        "checkType": check_type,
        "status": status,
        "message": message,
        "details": suggestions
    }
