# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared.repo_metadata — provider-agnostic repo shape retrieval.

Covers both GitHub and Azure DevOps branches. Mocks ``urlopen`` and
asserts URL construction + response-shape normalization into RepoMetadata.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from shared import repo_metadata


def _mock_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.headers = {}
    return resp


# ---------------------------------------------------------------------------
# Dispatch behavior + argument validation.
# ---------------------------------------------------------------------------


class TestFetchRepoMetadataDispatch:

    def test_unknown_provider_raises(self):
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata("bitbucket")

    def test_github_requires_repository_and_token(self):
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata("github", repository="o/r")
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata("github", github_token="ghp")

    def test_azure_devops_requires_all_ado_kwargs(self):
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "azure_devops",
                ado_org="o", ado_project="p", ado_repo="r",
            )
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "azure_devops",
                ado_org="o", ado_project="p",
                ado_access_token="tok",
            )

    def test_gitlab_requires_project_path_and_token(self):
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "gitlab", gitlab_project_path="grp/proj",
            )
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "gitlab", gitlab_access_token="tok",
            )


# ---------------------------------------------------------------------------
# GitHub branch.
# ---------------------------------------------------------------------------


class TestFetchRepoMetadataGitHub:

    @patch.object(repo_metadata, "urlopen")
    def test_returns_default_branch_and_file_paths(self, mock_urlopen):
        repo_resp = _mock_response({
            "default_branch": "main",
            "name": "example-repo",
        })
        tree_resp = _mock_response({
            "tree": [
                {"path": "src/a.py", "type": "blob"},
                {"path": "src", "type": "tree"},  # folder — filtered
                {"path": "src/b.py", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ],
            "truncated": False,
        })
        mock_urlopen.side_effect = [repo_resp, tree_resp]

        result = repo_metadata.fetch_repo_metadata(
            "github",
            repository="example-owner/example-repo",
            github_token="ghp_xxx",
        )
        assert result.default_branch == "main"
        assert result.file_paths == ["src/a.py", "src/b.py", "README.md"]

        repo_req = mock_urlopen.call_args_list[0].args[0]
        assert repo_req.full_url == (
            "https://api.github.com/repos/example-owner/example-repo"
        )
        assert repo_req.headers["Authorization"] == "Bearer ghp_xxx"

        tree_req = mock_urlopen.call_args_list[1].args[0]
        assert tree_req.full_url == (
            "https://api.github.com/repos/example-owner/example-repo/"
            "git/trees/main?recursive=1"
        )

    @patch.object(repo_metadata, "urlopen")
    def test_truncated_tree_still_returns_partial_result(self, mock_urlopen):
        # GitHub caps recursive-tree at 100k entries. If truncated we
        # log a warning but return what we have -- a partial view is
        # better than a hard error for TEST_COVERAGE / README_FRESHNESS.
        repo_resp = _mock_response({"default_branch": "main"})
        tree_resp = _mock_response({
            "tree": [{"path": "src/a.py", "type": "blob"}],
            "truncated": True,
        })
        mock_urlopen.side_effect = [repo_resp, tree_resp]

        result = repo_metadata.fetch_repo_metadata(
            "github", repository="o/r", github_token="ghp",
        )
        assert result.file_paths == ["src/a.py"]

    @patch.object(repo_metadata, "urlopen")
    def test_unexpected_shape_wraps_as_repo_metadata_error(self, mock_urlopen):
        # Missing default_branch
        mock_urlopen.return_value = _mock_response({"name": "example-repo"})
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "github", repository="o/r", github_token="ghp",
            )

    @patch.object(repo_metadata, "urlopen")
    def test_network_error_wraps_as_repo_metadata_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("504 Gateway Timeout")
        with pytest.raises(repo_metadata.RepoMetadataError) as ctx:
            repo_metadata.fetch_repo_metadata(
                "github", repository="o/r", github_token="ghp",
            )
        assert "504 Gateway Timeout" in str(ctx.value)


# ---------------------------------------------------------------------------
# Azure DevOps branch.
# ---------------------------------------------------------------------------


class TestFetchRepoMetadataAzureDevOps:

    @patch.object(repo_metadata, "urlopen")
    def test_returns_default_branch_stripped_and_blob_paths(self, mock_urlopen):
        repo_resp = _mock_response({
            "defaultBranch": "refs/heads/main",
            "name": "example-repo",
        })
        items_resp = _mock_response({
            "value": [
                {"path": "/src/a.py", "gitObjectType": "blob"},
                {"path": "/src",       "gitObjectType": "tree"},   # filtered
                {"path": "/src/b.py", "gitObjectType": "blob"},
                {"path": "/README.md", "gitObjectType": "blob"},
            ],
        })
        mock_urlopen.side_effect = [repo_resp, items_resp]

        result = repo_metadata.fetch_repo_metadata(
            "azure_devops",
            ado_org="example-org",
            ado_project="example-project",
            ado_repo="example-repo",
            ado_access_token="ado_bearer",
        )
        assert result.default_branch == "main"  # refs/heads/ stripped
        # Leading-slash paths normalized:
        assert result.file_paths == ["src/a.py", "src/b.py", "README.md"]

        repo_req = mock_urlopen.call_args_list[0].args[0]
        assert repo_req.full_url.startswith(
            "https://dev.azure.com/example-org/example-project/_apis/git/"
            "repositories/example-repo?"
        )
        assert "api-version=7.0" in repo_req.full_url
        assert repo_req.headers["Authorization"] == "Bearer ado_bearer"

        items_req = mock_urlopen.call_args_list[1].args[0]
        url = items_req.full_url
        assert url.startswith(
            "https://dev.azure.com/example-org/example-project/_apis/git/"
            "repositories/example-repo/items?"
        )
        assert "scopePath=%2F" in url
        assert "recursionLevel=Full" in url
        assert "versionDescriptor.version=main" in url
        assert "versionDescriptor.versionType=branch" in url

    @patch.object(repo_metadata, "urlopen")
    def test_default_branch_without_refs_heads_prefix_used_as_is(self, mock_urlopen):
        repo_resp = _mock_response({"defaultBranch": "main"})
        items_resp = _mock_response({"value": []})
        mock_urlopen.side_effect = [repo_resp, items_resp]

        result = repo_metadata.fetch_repo_metadata(
            "azure_devops",
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result.default_branch == "main"

    @patch.object(repo_metadata, "urlopen")
    def test_missing_default_branch_wraps_as_repo_metadata_error(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"name": "example-repo"})
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "azure_devops",
                ado_org="o", ado_project="p", ado_repo="r",
                ado_access_token="tok",
            )

    @patch.object(repo_metadata, "urlopen")
    def test_empty_tree_returns_empty_file_paths(self, mock_urlopen):
        repo_resp = _mock_response({"defaultBranch": "refs/heads/main"})
        items_resp = _mock_response({"value": []})
        mock_urlopen.side_effect = [repo_resp, items_resp]

        result = repo_metadata.fetch_repo_metadata(
            "azure_devops",
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result.file_paths == []

    @patch.object(repo_metadata, "urlopen")
    def test_ado_network_error_wraps_as_repo_metadata_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection reset")
        with pytest.raises(repo_metadata.RepoMetadataError) as ctx:
            repo_metadata.fetch_repo_metadata(
                "azure_devops",
                ado_org="o", ado_project="p", ado_repo="r",
                ado_access_token="tok",
            )
        assert "connection reset" in str(ctx.value)


# ---------------------------------------------------------------------------
# GitLab branch (ADR-050). Delegates to the shared GitLab API v4 client;
# ``get_tree`` already filters to blobs + returns bare paths.
# ---------------------------------------------------------------------------


class TestFetchRepoMetadataGitLab:

    @patch("shared.gitlab_api.get_tree")
    @patch("shared.gitlab_api.get_default_branch")
    def test_returns_default_branch_and_blob_paths(self, mock_branch, mock_tree):
        mock_branch.return_value = "main"
        mock_tree.return_value = ["src/a.py", "src/b.py", "README.md"]

        # Nested-subgroup project path exercises the variable-slash slug.
        result = repo_metadata.fetch_repo_metadata(
            "gitlab",
            gitlab_project_path="acme/platform/backend/api",
            gitlab_access_token="glpat-xyz",
        )
        assert result.default_branch == "main"
        assert result.file_paths == ["src/a.py", "src/b.py", "README.md"]

        assert mock_branch.call_args.args == (
            "glpat-xyz", "acme/platform/backend/api",
        )
        assert mock_tree.call_args.args == (
            "glpat-xyz", "acme/platform/backend/api", "main",
        )

    @patch("shared.gitlab_api.get_default_branch")
    def test_missing_default_branch_wraps_as_repo_metadata_error(self, mock_branch):
        mock_branch.return_value = None
        with pytest.raises(repo_metadata.RepoMetadataError):
            repo_metadata.fetch_repo_metadata(
                "gitlab",
                gitlab_project_path="grp/proj",
                gitlab_access_token="tok",
            )

    @patch("shared.gitlab_api.get_tree")
    @patch("shared.gitlab_api.get_default_branch")
    def test_empty_tree_returns_empty_file_paths(self, mock_branch, mock_tree):
        mock_branch.return_value = "develop"
        mock_tree.return_value = []
        result = repo_metadata.fetch_repo_metadata(
            "gitlab",
            gitlab_project_path="grp/proj",
            gitlab_access_token="tok",
        )
        assert result.default_branch == "develop"
        assert result.file_paths == []

    @patch("shared.gitlab_api.get_default_branch")
    def test_network_error_wraps_as_repo_metadata_error(self, mock_branch):
        mock_branch.side_effect = Exception("connection reset")
        with pytest.raises(repo_metadata.RepoMetadataError) as ctx:
            repo_metadata.fetch_repo_metadata(
                "gitlab",
                gitlab_project_path="grp/proj",
                gitlab_access_token="tok",
            )
        assert "connection reset" in str(ctx.value)
