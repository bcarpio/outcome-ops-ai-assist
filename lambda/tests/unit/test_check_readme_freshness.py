"""
Unit tests for readme_freshness check handler.

Tests cover:
- GitHub token retrieval from SSM
- PR diff fetching from GitHub API
- File diff extraction from full PR diff
- Handler name extraction from file paths
- Infrastructure name extraction from Terraform files
- Claude analysis of README adequacy
- Main handler orchestration
"""

import json
import pytest
import sys
import os
import importlib.util
from unittest.mock import Mock, patch, MagicMock
from moto import mock_aws
import boto3
import requests
from botocore.exceptions import ClientError

# Load the readme_freshness handler module
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/readme_freshness.py'
)
spec = importlib.util.spec_from_file_location("readme_freshness_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['readme_freshness_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from loaded module
get_github_token = handler_module.get_github_token
fetch_pr_file_diff = handler_module.fetch_pr_file_diff
extract_file_diff_from_full_diff = handler_module.extract_file_diff_from_full_diff
extract_handler_names = handler_module.extract_handler_names
extract_infrastructure_names = handler_module.extract_infrastructure_names
analyze_readme_with_claude = handler_module.analyze_readme_with_claude
check_readme_freshness = handler_module.check_readme_freshness


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM"""

    @mock_aws()
    @patch('readme_freshness_handler.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test: Successfully retrieve GitHub token from SSM"""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_test_token_12345"}
        }

        # Act
        token = get_github_token("/dev/app/github/token")

        # Assert
        assert token == "ghp_test_token_12345"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/dev/app/github/token",
            WithDecryption=True
        )

    @mock_aws()
    @patch('readme_freshness_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test: SSM parameter not found"""
        # Arrange
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "get_parameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            get_github_token("/dev/app/nonexistent")

    @mock_aws()
    @patch('readme_freshness_handler.ssm_client')
    def test_get_github_token_empty_value(self, mock_ssm):
        """Test: SSM parameter exists but has no value"""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {}  # Missing Value key
        }

        # Act & Assert
        with pytest.raises(Exception, match="GitHub token not found"):
            get_github_token("/dev/app/token")


class TestFetchPrFileDiff:
    """Test GitHub PR diff fetching"""

    @patch('readme_freshness_handler.requests.get')
    def test_fetch_pr_file_diff_success(self, mock_get):
        """Test: Successfully fetch README diff from GitHub"""
        # Arrange
        mock_response = Mock()
        mock_response.text = """diff --git a/README.md b/README.md
index abc123..def456 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
+## New Lambda Functions
+- hello: Greeting handler
 # Project Title"""
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        # Act
        diff = fetch_pr_file_diff("owner/repo", 123, "README.md", "token")

        # Assert
        assert "New Lambda Functions" in diff
        assert "README.md" in diff
        mock_get.assert_called_once()


class TestExtractFileDiff:
    """Test extracting specific file diff from full PR diff"""

    def test_extract_file_diff_readme(self):
        """Test: Extract README.md diff from full PR diff"""
        # Arrange
        full_diff = """diff --git a/README.md b/README.md
+README changes here
diff --git a/other.py b/other.py
+other changes"""

        # Act
        result = extract_file_diff_from_full_diff(full_diff, "README.md")

        # Assert
        assert "README.md" in result
        assert "README changes here" in result
        assert "other changes" not in result


class TestExtractHandlerNames:
    """Test extracting handler names from file paths"""

    def test_extract_handler_names_single_handler(self):
        """Test: Extract single handler name"""
        # Arrange
        files = ["lambda/hello/handler.py"]

        # Act
        result = extract_handler_names(files)

        # Assert
        assert result == ["hello"]

    def test_extract_handler_names_multiple_handlers(self):
        """Test: Extract multiple handler names"""
        # Arrange
        files = [
            "lambda/hello/handler.py",
            "lambda/world/handler.py",
            "lambda/foo/handler.py"
        ]

        # Act
        result = extract_handler_names(files)

        # Assert
        assert len(result) == 3
        assert "hello" in result
        assert "world" in result
        assert "foo" in result

    def test_extract_handler_names_excludes_tests(self):
        """Test: Exclude test files from handler extraction"""
        # Arrange
        files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py"
        ]

        # Act
        result = extract_handler_names(files)

        # Assert
        assert result == ["hello"]

    def test_extract_handler_names_excludes_non_handlers(self):
        """Test: Exclude non-handler Python files"""
        # Arrange
        files = [
            "lambda/hello/handler.py",
            "lambda/hello/utils.py",
            "README.md"
        ]

        # Act
        result = extract_handler_names(files)

        # Assert
        assert result == ["hello"]

    def test_extract_handler_names_empty_list(self):
        """Test: Handle empty file list"""
        # Arrange
        files = []

        # Act
        result = extract_handler_names(files)

        # Assert
        assert result == []


class TestExtractInfrastructureNames:
    """Test extracting infrastructure names from Terraform files"""

    def test_extract_infrastructure_names_single_file(self):
        """Test: Extract single Terraform file name"""
        # Arrange
        files = ["terraform/main.tf"]

        # Act
        result = extract_infrastructure_names(files)

        # Assert
        assert result == ["main"]

    def test_extract_infrastructure_names_multiple_files(self):
        """Test: Extract multiple Terraform file names"""
        # Arrange
        files = [
            "terraform/main.tf",
            "terraform/variables.tf",
            "terraform/outputs.tf"
        ]

        # Act
        result = extract_infrastructure_names(files)

        # Assert
        assert len(result) == 3
        assert "main" in result
        assert "variables" in result
        assert "outputs" in result

    def test_extract_infrastructure_names_excludes_non_terraform(self):
        """Test: Exclude non-Terraform files"""
        # Arrange
        files = [
            "terraform/main.tf",
            "lambda/handler.py",
            "README.md"
        ]

        # Act
        result = extract_infrastructure_names(files)

        # Assert
        assert result == ["main"]

    def test_extract_infrastructure_names_empty_list(self):
        """Test: Handle empty file list"""
        # Arrange
        files = []

        # Act
        result = extract_infrastructure_names(files)

        # Assert
        assert result == []


class TestAnalyzeReadmeWithClaude:
    """Test Claude analysis of README adequacy"""

    @patch('readme_freshness_handler.bedrock_client')
    def test_analyze_readme_adequate(self, mock_bedrock):
        """Test: Claude determines README is adequate"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": '{"adequate": true, "explanation": "README documents the new hello Lambda handler"}'
                    }]
                }
            }
        }

        # Act
        result = analyze_readme_with_claude(
            "+## New Lambda\n+- hello: Greeting handler",
            ["hello"],
            []
        )

        # Assert
        assert result["adequate"] is True
        assert "hello" in result["explanation"]

    @patch('readme_freshness_handler.bedrock_client')
    def test_analyze_readme_inadequate(self, mock_bedrock):
        """Test: Claude finds README inadequate"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": '{"adequate": false, "explanation": "README does not mention the new handler"}'
                    }]
                }
            }
        }

        # Act
        result = analyze_readme_with_claude(
            "+Minor typo fix",
            ["hello"],
            []
        )

        # Assert
        assert result["adequate"] is False
        assert "does not mention" in result["explanation"]

    @patch('readme_freshness_handler.bedrock_client')
    def test_analyze_readme_invalid_json_response(self, mock_bedrock):
        """Test: Handle invalid JSON response from Claude (fallback parsing)"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": "not valid json at all"
                    }]
                }
            }
        }

        # Act
        result = analyze_readme_with_claude(
            "+changes",
            ["hello"],
            []
        )

        # Assert
        assert "adequate" in result
        assert "explanation" in result
        assert result["explanation"] == "Could not parse analysis response"

    @patch('readme_freshness_handler.bedrock_client')
    def test_analyze_readme_bedrock_error(self, mock_bedrock):
        """Test: Bedrock API error"""
        # Arrange
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "converse"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            analyze_readme_with_claude("+changes", ["hello"], [])


class TestCheckReadmeFreshness:
    """Test main check_readme_freshness handler"""

    def test_check_readme_freshness_no_changes(self):
        """Test: No infrastructure or handler changes"""
        # Arrange
        changed_files = ["docs/guide.md", "lambda/tests/unit/test_something.py"]

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            changed_files,
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "PASS"
        assert "No infrastructure or handler changes" in result["message"]

    def test_check_readme_freshness_terraform_without_readme(self):
        """Test: Terraform changed but README not updated"""
        # Arrange
        changed_files = ["terraform/main.tf"]

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            changed_files,
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "WARN"
        assert "README.md not updated" in result["message"]
        assert any("Infrastructure files changed" in detail for detail in result["details"])

    def test_check_readme_freshness_handler_without_readme(self):
        """Test: Handler changed but README not updated"""
        # Arrange
        changed_files = ["lambda/hello/handler.py"]

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            changed_files,
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "WARN"
        assert "README.md not updated" in result["message"]
        assert any("Lambda handlers added" in detail for detail in result["details"])

    @patch('readme_freshness_handler.get_github_token')
    @patch('readme_freshness_handler.fetch_pr_file_diff')
    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_check_readme_freshness_adequate_update(
        self, mock_analyze, mock_fetch, mock_token
    ):
        """Test: README adequately documents changes"""
        # Arrange
        mock_token.return_value = "token"
        mock_fetch.return_value = "+## New Lambda\n+- hello: Greeting handler"
        mock_analyze.return_value = {
            "adequate": True,
            "explanation": "README documents the new hello Lambda handler"
        }

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            ["lambda/hello/handler.py", "README.md"],
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "PASS"
        assert "adequately documents" in result["message"]

    @patch('readme_freshness_handler.get_github_token')
    @patch('readme_freshness_handler.fetch_pr_file_diff')
    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_check_readme_freshness_inadequate_update(
        self, mock_analyze, mock_fetch, mock_token
    ):
        """Test: README update inadequate"""
        # Arrange
        mock_token.return_value = "token"
        mock_fetch.return_value = "+Minor typo fix"
        mock_analyze.return_value = {
            "adequate": False,
            "explanation": "README does not mention the new handler"
        }

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            ["lambda/hello/handler.py", "README.md"],
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "WARN"
        assert "may not adequately document" in result["message"]

    @patch('readme_freshness_handler.get_github_token')
    @patch('readme_freshness_handler.fetch_pr_file_diff')
    def test_check_readme_freshness_no_diff_available(
        self, mock_fetch, mock_token
    ):
        """Test: README in changed files but no diff available"""
        # Arrange
        mock_token.return_value = "token"
        mock_fetch.return_value = "diff --git a/README.md b/README.md\n(No diff available)"

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            ["lambda/hello/handler.py", "README.md"],
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "PASS"
        assert "README.md updated" in result["message"]

    @patch('readme_freshness_handler.get_github_token')
    def test_check_readme_freshness_analysis_error(self, mock_token):
        """Test: Error during Claude analysis (fallback to WARN)"""
        # Arrange
        mock_token.side_effect = Exception("API error")

        # Act
        result = check_readme_freshness(
            "README_FRESHNESS",
            123,
            "owner/repo",
            ["lambda/hello/handler.py", "README.md"],
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "WARN"
        assert "could not verify adequacy" in result["message"]
        assert any("Error:" in detail for detail in result["details"])
