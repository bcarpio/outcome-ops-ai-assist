# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0

# === Legacy dict -> unified Response adapter (ADR-035 fallout) ===
from types import SimpleNamespace
from shared.model_client_types import StopReason
class _LegacyDictResponse:
    def __init__(self, d):
        content = d.get("output", {}).get("message", {}).get("content", [])
        self.text = content[0].get("text", "") if content else ""
        self.content_blocks = [{"text": self.text}] if self.text else []
        usage = d.get("usage", {})
        self.usage = SimpleNamespace(input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0))
        self.stop_reason = StopReason.END_TURN
        self.tool_calls = None
        self.raw = d
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
from types import ModuleType

# First, load bedrock_utils module (dependency for readme_freshness)
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

# Load the readme_freshness handler module as part of check_handlers package
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/readme_freshness.py'
)
spec = importlib.util.spec_from_file_location(
    "check_handlers.readme_freshness",
    handler_path,
    submodule_search_locations=[os.path.dirname(handler_path)]
)
handler_module = importlib.util.module_from_spec(spec)
handler_module.__package__ = "check_handlers"
sys.modules['check_handlers.readme_freshness'] = handler_module
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

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_readme_adequate(self, mock_invoke):
        """Test: Claude determines README is adequate"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '{"adequate": true, "explanation": "README documents the new hello Lambda handler"}'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_readme_with_claude(
            "+## New Lambda\n+- hello: Greeting handler",
            ["hello"],
            [],
            usage_tracker
        )

        # Assert
        assert result["adequate"] is True
        assert "hello" in result["explanation"]
        assert usage_tracker.total_tokens > 0

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_readme_inadequate(self, mock_invoke):
        """Test: Claude finds README inadequate"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '{"adequate": false, "explanation": "README does not mention the new handler"}'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_readme_with_claude(
            "+Minor typo fix",
            ["hello"],
            [],
            usage_tracker
        )

        # Assert
        assert result["adequate"] is False
        assert "does not mention" in result["explanation"]

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_readme_invalid_json_response(self, mock_invoke):
        """Test: an unparseable response raises instead of guessing a verdict"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": "not valid json at all"
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act / Assert
        with pytest.raises(ValueError, match="Could not parse"):
            analyze_readme_with_claude(
                "+changes",
                ["hello"],
                [],
                usage_tracker
            )

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_readme_truncated_json_never_passes(self, mock_invoke):
        """A response cut off at max_tokens must not be read as adequate.

        The old fallback treated any text containing "adequate" and "true"
        as a PASS, so a truncated object fail-opened.
        """
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '{"adequate": false, "explanation": "true, the doc'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 2000}
        })
        usage_tracker = bedrock_module.UsageTracker()

        with pytest.raises(ValueError):
            analyze_readme_with_claude("+changes", ["hello"], [], usage_tracker)

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_readme_bedrock_error(self, mock_invoke):
        """Test: Bedrock API error"""
        # Arrange
        mock_invoke.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "converse"
        )
        usage_tracker = bedrock_module.UsageTracker()

        # Act & Assert
        with pytest.raises(ClientError):
            analyze_readme_with_claude("+changes", ["hello"], [], usage_tracker)


class _FakeSource:
    """Stands in for _PRSource: files at base/head and per-file diffs."""

    def __init__(self, files_at_head=None, diffs=None, diff_error=None):
        self.head_sha = "head"
        self.base_sha = "base"
        self._files = files_at_head or {}
        self._diffs = diffs or {}
        self._diff_error = diff_error
        self.diff_requests = []

    def file_at(self, path, sha):
        return self._files.get(path) if sha == self.head_sha else None

    def file_diffs(self, paths):
        self.diff_requests.append(list(paths))
        if self._diff_error:
            raise self._diff_error
        return {p: self._diffs[p] for p in paths if p in self._diffs}


@pytest.fixture
def source(monkeypatch):
    """Default: no .outcomeops.yaml, no diffs. Tests replace attributes."""
    fake = _FakeSource()
    monkeypatch.setattr(handler_module, "_PRSource", lambda **kw: fake)
    return fake


def _run(changed_files, **kwargs):
    return check_readme_freshness(
        "README_FRESHNESS", 123, "owner/repo", changed_files,
        "/dev/github/token", **kwargs,
    )


class TestDocumentationLocations:
    """Documentation / infrastructure path classification."""

    @pytest.mark.parametrize("path", [
        "README.md", "lambda/foo/README.md", "README", "docs/README.rst",
    ])
    def test_readme_anywhere_is_documentation(self, path):
        assert handler_module.is_documentation_file(path, []) is True

    @pytest.mark.parametrize("path,dirs,expected", [
        ("docs/lambda-foo.md", ["docs"], True),
        ("docs/adr/ADR-001.md", ["docs/adr"], True),
        ("documents/guide.adoc", ["documents/"], True),
        ("./documents/x.md", ["./documents"], False),
        ("docs/diagram.png", ["docs"], False),
        ("docsite/page.md", ["docs"], False),
        ("lambda/foo/notes.md", ["docs"], False),
        ("anything.md", ["."], True),
    ])
    def test_declared_doc_dirs(self, path, dirs, expected):
        assert handler_module.is_documentation_file(path, dirs) is expected

    @pytest.mark.parametrize("path,infra_dir,tool,expected", [
        ("terraform/main.tf", "terraform", "terraform", True),
        ("terraform/prd.tfvars.example", "terraform", "terraform", False),
        ("terraform/README.md", "terraform", "terraform", False),
        ("infra/stack.yaml", "infra", "cloudformation", True),
        ("infra/notes.md", "infra", "cloudformation", False),
        ("main.tf", "", "terraform", False),
        ("lambda/x/handler.py", "terraform", "terraform", False),
    ])
    def test_infrastructure_files(self, path, infra_dir, tool, expected):
        assert handler_module.is_infrastructure_file(path, infra_dir, tool) is expected

    def test_extract_infrastructure_names_custom_dir(self):
        names = extract_infrastructure_names(
            ["infra/net/vpc.yaml", "docs/x.md"], "infra", "cloudformation")
        assert names == ["net/vpc"]

    def test_describe_locations_root(self):
        settings = handler_module._DocSettings(["."], "terraform", "terraform")
        assert settings.describe_locations() == "README files or (repo root)/"

    def test_describe_locations_none(self):
        settings = handler_module._DocSettings([], "terraform", "terraform")
        assert settings.describe_locations() == "README files"


class TestLoadDocSettings:
    """.outcomeops.yaml drives the doc and infra locations."""

    def test_defaults_without_source(self):
        settings = handler_module._load_doc_settings(None)
        assert settings.doc_dirs == ["docs", "docs/adr"]
        assert settings.infra_dir == "terraform"

    def test_defaults_without_config_file(self):
        settings = handler_module._load_doc_settings(_FakeSource())
        assert settings.doc_dirs == ["docs", "docs/adr"]

    def test_reads_config_at_head(self):
        yaml_text = (
            b"ingestion:\n  docs_paths: [documents]\n  standards_paths: [documents/adr]\n"
            b"infrastructure:\n  tool: cloudformation\n  directory: infra\n"
        )
        settings = handler_module._load_doc_settings(
            _FakeSource(files_at_head={".outcomeops.yaml": yaml_text}))
        assert settings.doc_dirs == ["documents", "documents/adr"]
        assert settings.infra_dir == "infra"
        assert settings.infra_tool == "cloudformation"

    def test_invalid_config_falls_back(self):
        settings = handler_module._load_doc_settings(
            _FakeSource(files_at_head={".outcomeops.yaml": b"ingestion: [unclosed"}))
        assert settings.doc_dirs == ["docs", "docs/adr"]


class TestCombineDocDiffs:
    def test_sorted_and_joined(self):
        out = handler_module._combine_doc_diffs({"b.md": "B", "a.md": "A"})
        assert out == "A\nB"

    def test_per_file_truncation(self, monkeypatch):
        monkeypatch.setattr(handler_module, "_MAX_DOC_DIFF_CHARS_PER_FILE", 5)
        out = handler_module._combine_doc_diffs({"a.md": "0123456789"})
        assert out.startswith("01234") and "diff truncated" in out

    def test_total_cap_notes_omitted_files(self, monkeypatch):
        monkeypatch.setattr(handler_module, "_MAX_DOC_DIFF_CHARS_TOTAL", 15)
        out = handler_module._combine_doc_diffs(
            {"a.md": "x" * 10, "b.md": "y" * 10, "c.md": "z" * 10})
        assert "x" * 10 in out and "y" not in out
        assert "2 more documentation file(s) not shown" in out


class TestCheckReadmeFreshness:
    """Main check_readme_freshness handler"""

    def test_no_changes(self, source):
        result = _run(["docs/guide.md", "lambda/tests/unit/test_something.py"])
        assert result["checkType"] == "README_FRESHNESS"
        assert result["status"] == "PASS"
        assert result["message"] == "No new handlers or infrastructure changes detected"

    def test_terraform_without_docs(self, source):
        result = _run(["terraform/main.tf"])
        assert result["status"] == "WARN"
        assert result["message"] == "Documentation not updated"
        assert any(
            "Infrastructure files changed (1 file(s)) but no documentation was updated "
            "(looked in README files or docs/, docs/adr/)." in d
            for d in result["details"]
        )

    def test_handler_without_docs_newness_unknown(self, source):
        result = _run(["lambda/hello/handler.py"])
        assert result["status"] == "WARN"
        assert any(
            "Lambda handler files changed (1 file(s))" in d
            and "could not determine whether any are new" in d
            for d in result["details"]
        )
        assert not any("New Lambda handlers" in d for d in result["details"])

    def test_modified_handlers_only_pass(self, source):
        result = _run(
            ["lambda/hello/handler.py", "lambda/world/handler.py"], new_files=set())
        assert result["status"] == "PASS"

    def test_one_new_one_modified_handler(self, source):
        result = _run(
            ["lambda/hello/handler.py", "lambda/brand-new/handler.py"],
            new_files={"lambda/brand-new/handler.py"},
        )
        assert result["status"] == "WARN"
        assert any("New Lambda handlers added (1 file(s))" in d for d in result["details"])
        assert not any("could not determine" in d for d in result["details"])

    def test_terraform_with_modified_handlers_only(self, source):
        result = _run(["terraform/main.tf", "lambda/hello/handler.py"], new_files=set())
        assert result["status"] == "WARN"
        assert any("Infrastructure files changed (1 file(s))" in d for d in result["details"])
        assert not any("Lambda handler" in d for d in result["details"])

    def test_new_file_that_is_not_a_handler_ignored(self, source):
        result = _run(
            ["lambda/hello/handler.py", "lambda/hello/util.py"],
            new_files={"lambda/hello/util.py"},
        )
        assert result["status"] == "PASS"

    def test_terraform_readme_does_not_count_as_infrastructure(self, source):
        result = _run(["terraform/README.md"])
        assert result["status"] == "PASS"

    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_docs_dir_update_satisfies_check(self, mock_analyze, source):
        """A docs/ page (not README.md) counts as the documentation update."""
        source._diffs = {"docs/lambda-brand-new.md": "+# brand-new\n+does things"}
        mock_analyze.return_value = {"adequate": True, "explanation": "documented"}

        result = _run(
            ["lambda/brand-new/handler.py", "docs/lambda-brand-new.md"],
            new_files={"lambda/brand-new/handler.py"},
        )

        assert result["status"] == "PASS"
        assert result["message"] == "Documentation adequately covers changes"
        assert source.diff_requests == [["docs/lambda-brand-new.md"]]
        assert "does things" in mock_analyze.call_args.args[0]
        assert mock_analyze.call_args.args[1] == ["brand-new"]

    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_configured_docs_location_from_outcomeops_yaml(self, mock_analyze, source):
        """A repo that keeps docs in documents/ declares it and is honoured."""
        source._files = {".outcomeops.yaml": b"ingestion:\n  docs_paths: [documents]\n"}
        source._diffs = {"documents/handlers.md": "+brand-new handler"}
        mock_analyze.return_value = {"adequate": True, "explanation": "documented"}

        result = _run(
            ["lambda/brand-new/handler.py", "documents/handlers.md"],
            new_files={"lambda/brand-new/handler.py"},
        )

        assert result["status"] == "PASS"
        assert source.diff_requests == [["documents/handlers.md"]]

    def test_undeclared_docs_location_still_warns(self, source):
        """Without the config, documents/ is not a doc location."""
        result = _run(
            ["lambda/brand-new/handler.py", "documents/handlers.md"],
            new_files={"lambda/brand-new/handler.py"},
        )
        assert result["status"] == "WARN"
        assert result["message"] == "Documentation not updated"

    def test_configured_infra_dir(self, source):
        source._files = {
            ".outcomeops.yaml": b"infrastructure:\n  tool: cdk\n  directory: infra\n"}
        result = _run(["infra/app.ts", "terraform/legacy.tf"])
        assert result["status"] == "WARN"
        assert any("Infrastructure files changed (1 file(s))" in d for d in result["details"])

    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_claude_path_gets_only_new_handlers(self, mock_analyze, source):
        source._diffs = {"README.md": "+## brand-new"}
        mock_analyze.return_value = {"adequate": True, "explanation": "documented"}

        result = _run(
            ["lambda/hello/handler.py", "lambda/brand-new/handler.py", "README.md"],
            new_files={"lambda/brand-new/handler.py"},
        )

        assert result["status"] == "PASS"
        assert mock_analyze.call_args.args[1] == ["brand-new"]

    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_docs_updated_modified_only_skips_claude(self, mock_analyze, source):
        result = _run(["lambda/hello/handler.py", "README.md"], new_files=set())
        assert result["status"] == "PASS"
        mock_analyze.assert_not_called()
        assert source.diff_requests == []

    @patch('readme_freshness_handler.analyze_readme_with_claude')
    def test_inadequate_update(self, mock_analyze, source):
        source._diffs = {"README.md": "+Minor typo fix"}
        mock_analyze.return_value = {
            "adequate": False, "explanation": "does not mention the new handler"}

        result = _run(["lambda/hello/handler.py", "README.md"])

        assert result["status"] == "WARN"
        assert result["message"] == "Documentation may not adequately cover changes"
        assert result["details"][0] == "does not mention the new handler"

    def test_no_diff_available(self, source):
        result = _run(["lambda/hello/handler.py", "README.md"])
        assert result["status"] == "PASS"
        assert result["message"] == "Documentation updated"
        assert "README.md" in result["details"][0]

    def test_diff_error_falls_back_to_warn(self, source):
        source._diff_error = RuntimeError("API error")
        result = _run(["lambda/hello/handler.py", "README.md"])
        assert result["status"] == "WARN"
        assert "could not verify adequacy" in result["message"]
        assert any("Error: API error" in d for d in result["details"])

    def test_source_unavailable_uses_defaults_and_warns(self, monkeypatch):
        """Token/metadata failure: default locations, and the adequacy step
        reports it could not verify instead of passing."""
        def boom(**kwargs):
            raise RuntimeError("no token")
        monkeypatch.setattr(handler_module, "_PRSource", boom)

        missing = _run(["terraform/main.tf"])
        assert missing["message"] == "Documentation not updated"

        updated = _run(["terraform/main.tf", "docs/HA.md"])
        assert updated["status"] == "WARN"
        assert "could not verify adequacy" in updated["message"]

    def test_unknown_provider_raises(self, source):
        with pytest.raises(NotImplementedError):
            _run(["README.md"], provider="bitbucket")


class TestPRSource:
    """Provider dispatch for the PR source."""

    def _meta(self):
        return SimpleNamespace(base_sha="b", head_sha="h")

    @patch('shared.pr_metadata.fetch_pr_metadata')
    @patch('readme_freshness_handler.get_github_token', return_value="tok")
    def test_github_diffs_from_one_pr_diff_request(self, _tok, mock_meta):
        mock_meta.return_value = self._meta()
        src = handler_module._PRSource(
            provider="github", repository="o/r", pr_number=1,
            github_token_param="/p", connection_id=None,
            ado_org=None, ado_project=None, ado_repo=None,
        )
        full = (
            "diff --git a/README.md b/README.md\n+top\n"
            "diff --git a/docs/README.md b/docs/README.md\n+nested\n"
        )
        with patch('readme_freshness_handler.fetch_pr_diff', return_value=full) as f:
            diffs = src.file_diffs(["README.md", "docs/README.md", "docs/gone.md"])
        f.assert_called_once()
        assert "+top" in diffs["README.md"] and "+nested" not in diffs["README.md"]
        assert "+nested" in diffs["docs/README.md"]
        assert "docs/gone.md" not in diffs
        assert src.head_sha == "h"

    @patch('shared.pr_metadata.fetch_pr_metadata')
    @patch('readme_freshness_handler.refresh_ado_token', return_value="ado")
    def test_ado_diffs_computed_locally(self, _tok, mock_meta):
        from shared.source_fetch import SourceFetchError
        mock_meta.return_value = self._meta()
        src = handler_module._PRSource(
            provider="azure_devops", repository="r", pr_number=1,
            github_token_param="/p", connection_id="c",
            ado_org="o", ado_project="p", ado_repo="r",
        )

        def fetch(provider, path, sha, **kw):
            if path == "docs/new.md" and sha == "b":
                raise SourceFetchError("absent")
            if path == "docs/same.md":
                return b"same\n"
            return b"v-" + sha.encode() + b"\n"

        with patch('shared.source_fetch.fetch_file_at_sha', side_effect=fetch):
            diffs = src.file_diffs(["docs/new.md", "docs/same.md"])
            assert src.file_at("docs/new.md", "b") is None
        assert "+v-h" in diffs["docs/new.md"]
        assert "docs/same.md" not in diffs

    @patch('shared.pr_metadata.fetch_pr_metadata')
    def test_gitlab_context(self, mock_meta):
        mock_meta.return_value = self._meta()
        with patch('check_handlers.gitlab_context.get_gitlab_token', return_value="gl"), \
             patch('check_handlers.gitlab_context.get_gitlab_base_url', return_value="https://gl"):
            src = handler_module._PRSource(
                provider="gitlab", repository="g/p/r", pr_number=1,
                github_token_param="/p", connection_id="c",
                ado_org=None, ado_project=None, ado_repo=None,
            )
        assert src.fetch_kwargs["gitlab_project_path"] == "g/p/r"
        assert src.fetch_kwargs["gitlab_access_token"] == "gl"

    def test_unknown_provider(self):
        with pytest.raises(ValueError):
            handler_module._PRSource(
                provider="bitbucket", repository="r", pr_number=1,
                github_token_param="/p", connection_id=None,
                ado_org=None, ado_project=None, ado_repo=None,
            )
