"""
Unit tests for test_coverage check handler.

Tests cover:
- No new handlers to check
- Handler with corresponding test file
- Handler missing test file
- Multiple handlers with mixed coverage
- Exclusion of test files from handler check
- Case-insensitive test file matching
"""

import pytest
import sys
import os
import importlib.util

# Load the test_coverage handler module
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/test_coverage.py'
)
spec = importlib.util.spec_from_file_location("test_coverage_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['test_coverage_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import function from loaded module
check_test_coverage = handler_module.check_test_coverage


class TestCheckTestCoverage:
    """Test suite for test_coverage check handler"""

    def test_check_test_coverage_no_new_handlers(self):
        """Test: No new handlers to check"""
        # Arrange: Changed files don't include any handlers
        changed_files = ["README.md", "terraform/main.tf", "docs/guide.md"]

        # Act
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        # Assert
        assert result["checkType"] == "TEST_COVERAGE"
        assert result["status"] == "PASS"
        assert result["message"] == "No new handlers to check"
        assert result["details"] == []

    def test_check_test_coverage_handler_with_test_exists(self):
        """Test: New handler has corresponding test file"""
        # Arrange: Handler and matching test file
        changed_files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py"
        ]

        # Act
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        # Assert
        assert result["checkType"] == "TEST_COVERAGE"
        assert result["status"] == "PASS"
        assert "All handlers have test coverage" in result["message"]
        assert result["details"] == []

    def test_check_test_coverage_handler_missing_test(self):
        """Test: New handler missing test file"""
        # Arrange: Handler without test file
        changed_files = ["lambda/new-handler/handler.py"]

        # Act
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        # Assert
        assert result["checkType"] == "TEST_COVERAGE"
        assert result["status"] == "WARN"
        assert "1 handler(s) may be missing tests" in result["message"]
        assert len(result["details"]) == 1
        assert "No test file found" in result["details"][0]
        assert "new-handler" in result["details"][0]

    def test_check_test_coverage_multiple_handlers_mixed_coverage(self):
        """Test: Multiple handlers, some with tests, some without"""
        # Arrange: Two handlers, only one has test (using same naming pattern)
        changed_files = [
            "lambda/handlerA/handler.py",
            "lambda/handlerB/handler.py",
            "lambda/tests/unit/test_handlerA.py"  # Only handlerA has test
        ]

        # Act
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        # Assert
        assert result["checkType"] == "TEST_COVERAGE"
        assert result["status"] == "WARN"
        assert "1 handler(s) may be missing tests" in result["message"]
        assert len(result["details"]) == 1
        assert any("handlerB" in detail for detail in result["details"])
        # handlerA should not be in suggestions since it has a test
        assert not any("handlerA" in detail for detail in result["details"])

    def test_check_test_coverage_excludes_test_files(self):
        """Test: Test files themselves are excluded from handler check"""
        # Arrange: Only test files modified (no actual handlers)
        changed_files = [
            "lambda/tests/unit/test_something.py",  # Should be excluded
            "lambda/tests/integration/test_flow.py"  # Should be excluded
        ]

        # Act
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        # Assert
        assert result["checkType"] == "TEST_COVERAGE"
        assert result["status"] == "PASS"
        assert "No new handlers to check" in result["message"]
        assert result["details"] == []

    def test_check_test_coverage_case_insensitive_matching(self):
        """Test: Case insensitive matching for test files"""
        # Arrange: Handler with uppercase name, test with lowercase
        changed_files = [
            "lambda/MyHandler/handler.py",
            "lambda/tests/unit/test_myhandler.py"  # lowercase test name
        ]

        # Act
        result = check_test_coverage("TEST_COVERAGE", 123, "owner/repo", changed_files)

        # Assert
        assert result["checkType"] == "TEST_COVERAGE"
        assert result["status"] == "PASS"
        assert "All handlers have test coverage" in result["message"]
        assert result["details"] == []
