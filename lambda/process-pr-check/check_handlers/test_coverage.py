"""
Test Coverage check handler.

Checks:
- New handlers should have corresponding test files
- Test files should exist in lambda/tests/unit/ directory
"""

import logging
from typing import Dict, List, Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def check_test_coverage(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str]
) -> Dict[str, Any]:
    """
    Test Coverage check handler.

    Checks:
    - New Lambda handlers should have corresponding test files
    - Test files should exist in lambda/tests/unit/ directory
    """
    logger.info(f"Running test coverage check for PR #{pr_number}")

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
                f"{handler}: No test file found. "
                f"Consider adding test_{{handler_name}}.py in lambda/tests/unit/ directory."
            )

    # Determine status
    if not new_handlers:
        status = "PASS"
        message = "No new handlers to check"
    elif suggestions:
        status = "WARN"
        message = f"{len(suggestions)} handler(s) may be missing tests"
    else:
        status = "PASS"
        message = "All handlers have test coverage"

    return {
        "checkType": check_type,
        "status": status,
        "message": message,
        "details": suggestions
    }
