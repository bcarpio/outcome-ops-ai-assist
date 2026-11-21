# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for license_compliance check handler.

Tests cover:
- License pattern detection (prohibited, allowed, review-required)
- Copyright header checking
- Source file identification
- New file detection
- End-to-end license compliance checking
"""

import json
import os
import sys
from unittest.mock import Mock, patch, MagicMock
import importlib.util
from types import ModuleType

import pytest

# First, load bedrock_utils module (dependency for license_compliance)
bedrock_utils_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/bedrock_utils.py'
)
bedrock_spec = importlib.util.spec_from_file_location("bedrock_utils", bedrock_utils_path)
bedrock_module = importlib.util.module_from_spec(bedrock_spec)
bedrock_spec.loader.exec_module(bedrock_module)

# Create a fake check_handlers package that contains bedrock_utils
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [os.path.dirname(bedrock_utils_path)]
check_handlers_package.bedrock_utils = bedrock_module
sys.modules['check_handlers'] = check_handlers_package
sys.modules['check_handlers.bedrock_utils'] = bedrock_module

# Load the license_compliance check handler module as part of check_handlers package
check_handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/license_compliance.py'
)
spec = importlib.util.spec_from_file_location(
    "check_handlers.license_compliance",
    check_handler_path,
    submodule_search_locations=[os.path.dirname(check_handler_path)]
)
license_module = importlib.util.module_from_spec(spec)
license_module.__package__ = "check_handlers"
sys.modules['check_handlers.license_compliance'] = license_module
sys.modules['license_compliance_handler'] = license_module
spec.loader.exec_module(license_module)

# Import functions from the loaded module
detect_license_patterns = license_module.detect_license_patterns
check_copyright_header = license_module.check_copyright_header
is_source_file = license_module.is_source_file
is_new_file = license_module.is_new_file
check_license_compliance = license_module.check_license_compliance
# Phase 2 functions
normalize_code = license_module.normalize_code
quick_pattern_check = license_module.quick_pattern_check
is_ai_generated_commit = license_module.is_ai_generated_commit
fetch_pr_commits = license_module.fetch_pr_commits


class TestLicensePatternDetection:
    """Test license pattern detection."""

    def test_detect_gpl_license(self):
        """Test GPL detection (prohibited)."""
        # Arrange
        code = """
        # This file is licensed under the GNU General Public License v3.0
        # See LICENSE for details
        """

        # Act
        findings = detect_license_patterns(code)

        # Assert
        assert "GPL" in findings["prohibited"]
        assert len(findings["allowed"]) == 0
        assert len(findings["review_required"]) == 0

    def test_detect_agpl_license(self):
        """Test AGPL detection (prohibited)."""
        # Arrange
        code = """
        /*
         * Licensed under GNU Affero General Public License
         */
        """

        # Act
        findings = detect_license_patterns(code)

        # Assert
        assert "AGPL" in findings["prohibited"]

    def test_detect_mit_license(self):
        """Test MIT detection (allowed)."""
        # Arrange
        code = """
        # MIT License
        #
        # Copyright (c) 2024 Company Name
        """

        # Act
        findings = detect_license_patterns(code)

        # Assert
        assert "MIT" in findings["allowed"]
        assert len(findings["prohibited"]) == 0

    def test_detect_apache_license(self):
        """Test Apache 2.0 detection (allowed)."""
        # Arrange
        code = """
        // Licensed under the Apache License, Version 2.0
        // http://www.apache.org/licenses/LICENSE-2.0
        """

        # Act
        findings = detect_license_patterns(code)

        # Assert
        assert "Apache-2.0" in findings["allowed"]

    def test_detect_lgpl_license(self):
        """Test LGPL detection (review required)."""
        # Arrange
        code = """
        # GNU Lesser General Public License
        # Version 3, 29 June 2007
        """

        # Act
        findings = detect_license_patterns(code)

        # Assert
        assert "LGPL" in findings["review_required"]
        assert len(findings["prohibited"]) == 0

    def test_detect_no_license(self):
        """Test code with no license text."""
        # Arrange
        code = """
        def hello_world():
            print("Hello, World!")
        """

        # Act
        findings = detect_license_patterns(code)

        # Assert
        assert len(findings["prohibited"]) == 0
        assert len(findings["allowed"]) == 0
        assert len(findings["review_required"]) == 0


class TestCopyrightHeaderChecking:
    """Test copyright header detection."""

    def test_has_copyright_header(self):
        """Test file with copyright header."""
        # Arrange
        diff = """
diff --git a/lambda/hello/handler.py b/lambda/hello/handler.py
new file mode 100644
+# Copyright 2024-2025 Company Name
+# SPDX-License-Identifier: Apache-2.0
+
+def handler(event, context):
+    return {"statusCode": 200}
        """

        # Act
        has_copyright = check_copyright_header(diff)

        # Assert
        assert has_copyright is True

    def test_missing_copyright_header(self):
        """Test file missing copyright header."""
        # Arrange
        diff = """
diff --git a/lambda/hello/handler.py b/lambda/hello/handler.py
new file mode 100644
+def handler(event, context):
+    return {"statusCode": 200}
        """

        # Act
        has_copyright = check_copyright_header(diff)

        # Assert
        assert has_copyright is False

    def test_copyright_in_modified_lines_only(self):
        """Test copyright only checks added lines."""
        # Arrange
        diff = """
diff --git a/lambda/hello/handler.py b/lambda/hello/handler.py
--- a/lambda/hello/handler.py
+++ b/lambda/hello/handler.py
 # Copyright 2024 Company Name
-def old_function():
-    pass
+def new_function():
+    return True
        """

        # Act
        has_copyright = check_copyright_header(diff)

        # Assert
        assert has_copyright is False  # Copyright not in added lines


class TestSourceFileIdentification:
    """Test source file identification."""

    def test_python_file_is_source(self):
        """Test Python file identified as source."""
        assert is_source_file("lambda/hello/handler.py") is True

    def test_javascript_file_is_source(self):
        """Test JavaScript file identified as source."""
        assert is_source_file("src/index.js") is True

    def test_markdown_file_is_source(self):
        """Test Markdown file identified as source (for code blocks)."""
        assert is_source_file("docs/README.md") is True

    def test_image_file_not_source(self):
        """Test image file not identified as source."""
        assert is_source_file("assets/logo.png") is False

    def test_node_modules_skipped(self):
        """Test node_modules files skipped."""
        assert is_source_file("node_modules/package/index.js") is False

    def test_dist_directory_skipped(self):
        """Test dist directory files skipped."""
        assert is_source_file("dist/bundle.js") is False


class TestNewFileDetection:
    """Test new file detection."""

    def test_new_file_detected(self):
        """Test new file mode detected."""
        # Arrange
        diff = """
diff --git a/lambda/hello/handler.py b/lambda/hello/handler.py
new file mode 100644
index 0000000..abc1234
+# New file content
        """

        # Act
        is_new = is_new_file(diff)

        # Assert
        assert is_new is True

    def test_modified_file_not_new(self):
        """Test modified file not detected as new."""
        # Arrange
        diff = """
diff --git a/lambda/hello/handler.py b/lambda/hello/handler.py
index abc1234..def5678 100644
--- a/lambda/hello/handler.py
+++ b/lambda/hello/handler.py
-old line
+new line
        """

        # Act
        is_new = is_new_file(diff)

        # Assert
        assert is_new is False


class TestLicenseComplianceCheck:
    """Test end-to-end license compliance check."""

    @patch('license_compliance_handler.get_github_token')
    @patch('license_compliance_handler.query_knowledge_base')
    @patch('license_compliance_handler.fetch_pr_file_diff')
    @patch('license_compliance_handler.analyze_with_claude')
    def test_check_passes_with_allowed_license(
        self,
        mock_claude,
        mock_fetch_diff,
        mock_query_kb,
        mock_get_token
    ):
        """Test check passes when file has allowed license."""
        # Arrange
        mock_get_token.return_value = "fake-token"
        mock_query_kb.return_value = {"answer": "MIT and Apache 2.0 are allowed", "sources": []}
        mock_fetch_diff.return_value = """
+# MIT License
+# Copyright 2024 Company
+def handler():
+    pass
        """
        mock_claude.return_value = {
            "status": "PASS",
            "explanation": "MIT license is allowed per ADR-008",
            "suggestions": []
        }

        # Act
        result = check_license_compliance(
            check_type="LICENSE_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changed_files=["lambda/hello/handler.py"],
            query_kb_lambda_name="query-kb",
            github_token_param="/dev/app/github/token"
        )

        # Assert
        assert result["status"] == "PASS"
        assert result["checkType"] == "LICENSE_COMPLIANCE"

    @patch('license_compliance_handler.get_github_token')
    @patch('license_compliance_handler.query_knowledge_base')
    @patch('license_compliance_handler.fetch_pr_file_diff')
    @patch('license_compliance_handler.analyze_with_claude')
    def test_check_fails_with_gpl_license(
        self,
        mock_claude,
        mock_fetch_diff,
        mock_query_kb,
        mock_get_token
    ):
        """Test check fails when file has GPL license."""
        # Arrange
        mock_get_token.return_value = "fake-token"
        mock_query_kb.return_value = {"answer": "GPL is prohibited", "sources": []}
        mock_fetch_diff.return_value = """
+# GNU General Public License v3.0
+def handler():
+    pass
        """
        mock_claude.return_value = {
            "status": "FAIL",
            "explanation": "GPL license detected - prohibited per ADR-008",
            "suggestions": ["Remove GPL licensed code"]
        }

        # Act
        result = check_license_compliance(
            check_type="LICENSE_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changed_files=["lambda/hello/handler.py"],
            query_kb_lambda_name="query-kb",
            github_token_param="/dev/app/github/token"
        )

        # Assert
        assert result["status"] == "FAIL"
        assert "prohibited" in result["message"].lower()

    @patch('license_compliance_handler.get_github_token')
    @patch('license_compliance_handler.query_knowledge_base')
    def test_check_passes_with_no_source_files(
        self,
        mock_query_kb,
        mock_get_token
    ):
        """Test check passes when no source files changed."""
        # Arrange
        mock_get_token.return_value = "fake-token"
        mock_query_kb.return_value = {"answer": "Policy info", "sources": []}

        # Act
        result = check_license_compliance(
            check_type="LICENSE_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changed_files=["image.png", "data.json"],  # No source files
            query_kb_lambda_name="query-kb",
            github_token_param="/dev/app/github/token"
        )

        # Assert
        assert result["status"] == "PASS"
        assert result["message"] == "No source files to check"


class TestPhase2CodeSimilarity:
    """Test Phase 2 code similarity detection."""

    def test_normalize_code_removes_comments(self):
        """Test that normalize_code removes comments and whitespace."""
        # Arrange
        code = """
        # This is a Python comment
        def hello():  # inline comment
            // This is a JS comment
            return "world"  /* multi-line
            comment */
        """

        # Act
        normalized = normalize_code(code)

        # Assert
        assert "#" not in normalized
        assert "//" not in normalized
        assert "/*" not in normalized
        assert "def hello():" in normalized
        assert 'return "world"' in normalized

    def test_quick_pattern_check_detects_gpl_quicksort(self):
        """Test quick pattern check detects GPL quicksort pattern."""
        # Arrange
        code = """
        def quicksort(arr, pivot=0, partition=True):
            if len(arr) <= 1:
                return arr
            pivot = arr[0]
            left = [x for x in arr[1:] if x < pivot]
            right = [x for x in arr[1:] if x >= pivot]
            return quicksort(left) + [pivot] + quicksort(right)
        """

        # Act
        match = quick_pattern_check(code)

        # Assert
        assert match is not None
        assert match["match_type"] == "pattern"
        assert "quicksort" in match["description"].lower()

    def test_quick_pattern_check_no_match(self):
        """Test quick pattern check returns None for clean code."""
        # Arrange
        code = """
        def add_numbers(a, b):
            return a + b

        def multiply(x, y):
            return x * y
        """

        # Act
        match = quick_pattern_check(code)

        # Assert
        assert match is None

    def test_is_ai_generated_commit_detects_claude_code_marker(self):
        """Test AI commit detection via 'Generated with Claude Code'."""
        # Arrange
        commit = {
            "sha": "abc123",
            "message": "feat: add authentication\n\nGenerated with Claude Code\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
            "author": "Developer"
        }

        # Act
        is_ai = is_ai_generated_commit(commit)

        # Assert
        assert is_ai is True

    def test_is_ai_generated_commit_detects_co_author(self):
        """Test AI commit detection via Co-Authored-By: Claude."""
        # Arrange
        commit = {
            "sha": "abc123",
            "message": "fix: update logic\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
            "author": "Developer"
        }

        # Act
        is_ai = is_ai_generated_commit(commit)

        # Assert
        assert is_ai is True

    def test_is_ai_generated_commit_rejects_human_commit(self):
        """Test AI commit detection returns False for human commits."""
        # Arrange
        commit = {
            "sha": "abc123",
            "message": "feat: add authentication\n\nManually written code",
            "author": "Developer"
        }

        # Act
        is_ai = is_ai_generated_commit(commit)

        # Assert
        assert is_ai is False

    @patch('license_compliance_handler.requests.get')
    def test_fetch_pr_commits_success(self, mock_get):
        """Test fetching PR commits from GitHub API."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "sha": "commit1",
                "commit": {
                    "message": "feat: add feature\n\nGenerated with Claude Code",
                    "author": {"name": "Developer"}
                }
            },
            {
                "sha": "commit2",
                "commit": {
                    "message": "fix: bug fix",
                    "author": {"name": "Developer"}
                }
            }
        ]
        mock_get.return_value = mock_response

        # Act
        commits = fetch_pr_commits("owner/repo", 123, "fake-token")

        # Assert
        assert len(commits) == 2
        assert commits[0]["sha"] == "commit1"
        assert "Claude Code" in commits[0]["message"]
        assert commits[1]["sha"] == "commit2"

    def test_normalize_code_consistent_hashing(self):
        """Test that normalize_code produces consistent hashes for similar code."""
        # Arrange
        code1 = """
        def hello():
            # Comment here
            return "world"
        """

        code2 = """
        def hello():


            return "world"  # Different comment
        """

        # Act
        normalized1 = normalize_code(code1)
        normalized2 = normalize_code(code2)

        # Assert
        # Normalized versions should be similar (same logic, different formatting)
        assert "def hello():" in normalized1
        assert "def hello():" in normalized2
        assert 'return "world"' in normalized1
        assert 'return "world"' in normalized2


class TestFileTypeDetection:
    """Test that test files are excluded from scanning."""

    def test_test_directories_excluded(self):
        """Test that files in test directories are excluded."""
        assert is_source_file("lambda/tests/unit/test_foo.py") is False
        assert is_source_file("tests/test_error.py") is False
        assert is_source_file("src/test/fixtures/data.py") is False
        assert is_source_file("__tests__/unit/foo.js") is False

    def test_test_files_excluded(self):
        """Test that files starting with test_ or test. are excluded (any language)."""
        # Python
        assert is_source_file("test_something.py") is False
        assert is_source_file("lambda/test_helper.py") is False
        # JavaScript
        assert is_source_file("test_component.js") is False
        assert is_source_file("test.js") is False
        # Go
        assert is_source_file("test_handler.go") is False
        # Ruby
        assert is_source_file("test_user.rb") is False

    def test_non_test_source_files_included(self):
        """Test that non-test source files are included."""
        assert is_source_file("lambda/handler.py") is True
        assert is_source_file("src/utils/helper.py") is True
        assert is_source_file("docs/README.md") is True
        # Files with 'test' in name but not in test dir or starting with test_
        assert is_source_file("lambda/test-gpl-code/auth_validator.py") is True
        assert is_source_file("src/latest_updates.py") is True


# --- Provider passthrough to the scope resolver (ADR-052 provider parity) ---

from contextlib import ExitStack  # noqa: E402


@pytest.mark.parametrize("provider,repository,ado_kwargs", [
    ("gitlab", "group/sub/project", {}),
    ("azure_devops", "org/project/repo",
     {"ado_org": "org", "ado_project": "project", "ado_repo": "repo"}),
])
def test_license_compliance_passes_provider_to_scope_resolvers(provider, repository, ado_kwargs):
    with ExitStack() as stack:
        stack.enter_context(patch(
            'shared.pr_metadata.fetch_pr_metadata',
            return_value=MagicMock(base_sha="b", head_sha="h"),
        ))
        if provider == "gitlab":
            stack.enter_context(patch('check_handlers.gitlab_context.get_gitlab_token', return_value="gl"))
            stack.enter_context(patch('check_handlers.gitlab_context.get_gitlab_base_url', return_value="https://gl"))
        else:
            stack.enter_context(patch.object(license_module, 'refresh_ado_token', return_value="ado"))
        scope = stack.enter_context(patch(
            'shared.pr_check_scope.resolve_pr_check_workspace_scope', return_value=["ws-a"],
        ))
        owners = stack.enter_context(patch(
            'shared.pr_check_scope.find_workspaces_for_repo', return_value=["ws-a"],
        ))
        kb = stack.enter_context(patch.object(
            license_module, 'query_knowledge_base', return_value={"answer": "policy"},
        ))
        # No source files: the check returns after the KB policy lookup.
        result = check_license_compliance(
            "LICENSE_COMPLIANCE", 1, repository, ["image.png"], "qkb", "/p",
            provider=provider, connection_id="c", **ado_kwargs,
        )

    assert result["status"] == "PASS"
    scope.assert_called_once_with(
        check_type="LICENSE_COMPLIANCE", repo_name=repository, provider=provider,
    )
    owners.assert_called_once_with(repository, provider=provider)
    assert kb.call_args.kwargs["workspace_ids"] == ["ws-a"]
