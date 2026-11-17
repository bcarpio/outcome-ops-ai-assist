# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared.pr_commits — provider-agnostic PR commit-list retrieval."""

import json
from unittest.mock import MagicMock, patch

import pytest

from shared import pr_commits


def _mock_response(body) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.headers = {}
    return resp


class TestFetchPRCommitsDispatch:

    def test_unknown_provider_raises(self):
        with pytest.raises(pr_commits.PRCommitsError):
            pr_commits.fetch_pr_commits("bitbucket", 1)

    def test_github_requires_repository_and_token(self):
        with pytest.raises(pr_commits.PRCommitsError):
            pr_commits.fetch_pr_commits("github", 1, repository="o/r")
        with pytest.raises(pr_commits.PRCommitsError):
            pr_commits.fetch_pr_commits("github", 1, github_token="ghp")

    def test_azure_devops_requires_all_ado_kwargs(self):
        with pytest.raises(pr_commits.PRCommitsError):
            pr_commits.fetch_pr_commits(
                "azure_devops", 1,
                ado_org="o", ado_project="p", ado_repo="r",
            )


class TestFetchPRCommitsGitHub:

    @patch.object(pr_commits, "urlopen")
    def test_returns_full_message_from_github(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response([
            {
                "sha": "abc123",
                "commit": {
                    "message": "feat: add thing\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                    "author": {"name": "Human Dev"},
                },
            },
        ])
        result = pr_commits.fetch_pr_commits(
            "github", 42, repository="o/r", github_token="ghp",
        )
        assert len(result) == 1
        assert result[0].sha == "abc123"
        assert "Co-Authored-By: Claude" in result[0].message
        assert result[0].author == "Human Dev"

    @patch.object(pr_commits, "urlopen")
    def test_unexpected_shape_wraps_as_pr_commits_error(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response([
            {"sha": "abc", "commit": {}},  # missing message + author
        ])
        with pytest.raises(pr_commits.PRCommitsError):
            pr_commits.fetch_pr_commits(
                "github", 1, repository="o/r", github_token="ghp",
            )


class TestFetchPRCommitsAzureDevOps:

    @patch.object(pr_commits, "urlopen")
    def test_untruncated_ado_response_returns_directly(self, mock_urlopen):
        list_resp = _mock_response({
            "value": [
                {
                    "commitId": "abc123",
                    "comment": "short subject only, no Claude trailer",
                    "commentTruncated": False,
                    "author": {"name": "Alice", "email": "alice@example.com"},
                },
            ],
        })
        mock_urlopen.side_effect = [list_resp]

        result = pr_commits.fetch_pr_commits(
            "azure_devops", 42,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert len(result) == 1
        assert result[0].sha == "abc123"
        assert result[0].author == "Alice"
        assert result[0].message == "short subject only, no Claude trailer"

    @patch.object(pr_commits, "urlopen")
    def test_truncated_ado_response_refetches_full_body(self, mock_urlopen):
        # This is the load-bearing behavior for AI-generated detection:
        # Co-Authored-By trailers live at the end of the body; the
        # truncated list response cuts them off. The individual /commits
        # endpoint returns the full comment.
        list_resp = _mock_response({
            "value": [
                {
                    "commitId": "abc123",
                    "comment": "feat: add thing",  # truncated
                    "commentTruncated": True,
                    "author": {"name": "Alice"},
                },
            ],
        })
        commit_resp = _mock_response({
            "commitId": "abc123",
            "comment": "feat: add thing\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        })
        mock_urlopen.side_effect = [list_resp, commit_resp]

        result = pr_commits.fetch_pr_commits(
            "azure_devops", 42,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert len(result) == 1
        assert "Co-Authored-By: Claude" in result[0].message
        # Confirm we hit BOTH endpoints.
        assert mock_urlopen.call_count == 2
        commit_req = mock_urlopen.call_args_list[1].args[0]
        assert "/commits/abc123" in commit_req.full_url

    @patch.object(pr_commits, "urlopen")
    def test_truncated_refetch_failure_falls_back_to_short(self, mock_urlopen):
        # If the refetch fails, fall back to the truncated version rather
        # than blowing up the whole check.
        list_resp = _mock_response({
            "value": [
                {
                    "commitId": "abc",
                    "comment": "truncated",
                    "commentTruncated": True,
                    "author": {"name": "u"},
                },
            ],
        })
        mock_urlopen.side_effect = [list_resp, Exception("network error")]

        result = pr_commits.fetch_pr_commits(
            "azure_devops", 1,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert len(result) == 1
        assert result[0].message == "truncated"  # fallback

    @patch.object(pr_commits, "urlopen")
    def test_ado_author_falls_back_to_email_when_name_missing(self, mock_urlopen):
        mock_urlopen.side_effect = [_mock_response({
            "value": [
                {
                    "commitId": "abc",
                    "comment": "x",
                    "commentTruncated": False,
                    "author": {"email": "user@example.com"},
                },
            ],
        })]
        result = pr_commits.fetch_pr_commits(
            "azure_devops", 1,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result[0].author == "user@example.com"

    @patch.object(pr_commits, "urlopen")
    def test_ado_url_carries_api_version(self, mock_urlopen):
        mock_urlopen.side_effect = [_mock_response({"value": []})]
        pr_commits.fetch_pr_commits(
            "azure_devops", 42,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        req = mock_urlopen.call_args_list[0].args[0]
        assert "api-version=7.0" in req.full_url
        assert req.headers["Authorization"] == "Bearer tok"


class TestFetchPRCommitsGitLab:
    """Chunk 05: MR commits via the shared GitLab client. ``message`` is
    the full body, so Co-Authored-By trailers survive without an
    ADO-style truncation refetch."""

    def test_gitlab_requires_project_path_and_token(self):
        with pytest.raises(pr_commits.PRCommitsError):
            pr_commits.fetch_pr_commits(
                "gitlab", 1, gitlab_project_path="g/p")

    def test_gitlab_commits_normalized(self):
        entries = [
            {"id": "a" * 40,
             "title": "feat: add tier",
             "message": "feat: add tier\n\nCo-Authored-By: Claude <n@a>",
             "author_name": "Brian Carpio"},
            {"id": "b" * 40, "message": "fix: typo", "author_name": "Dev"},
        ]
        with patch("shared.gitlab_api.get_merge_request_commits",
                   return_value=entries):
            commits = pr_commits.fetch_pr_commits(
                "gitlab", 7,
                gitlab_project_path="group/project",
                gitlab_access_token="tok",
            )
        assert len(commits) == 2
        assert commits[0].sha == "a" * 40
        assert "Co-Authored-By: Claude" in commits[0].message
        assert commits[0].author == "Brian Carpio"
