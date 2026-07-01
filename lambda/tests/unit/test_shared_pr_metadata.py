"""Unit tests for shared.pr_metadata — provider-agnostic PR-metadata retrieval.

Covers both GitHub and Azure DevOps branches. Mocks ``urlopen`` at the
module level and asserts URL construction, header population, and
response-shape normalization into the PRMetadata NamedTuple.
"""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from shared import pr_metadata


# ---------------------------------------------------------------------------
# Helpers to build fake urlopen responses.
# ---------------------------------------------------------------------------


def _mock_response(body: dict, link_header: str = "") -> MagicMock:
    """Build a MagicMock that behaves like a urlopen context manager."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.headers = {"Link": link_header} if link_header else {}
    return resp


# ---------------------------------------------------------------------------
# Dispatch behavior + argument validation.
# ---------------------------------------------------------------------------


class TestFetchPRMetadataDispatch:

    def test_unknown_provider_raises(self):
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata("bitbucket", 1)

    def test_github_requires_repository_and_token(self):
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata("github", 1, repository="o/r")
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata("github", 1, github_token="ghp")

    def test_azure_devops_requires_all_ado_kwargs(self):
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata(
                "azure_devops", 1,
                ado_org="o", ado_project="p", ado_repo="r",
            )
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata(
                "azure_devops", 1,
                ado_org="o", ado_project="p",
                ado_access_token="tok",
            )


# ---------------------------------------------------------------------------
# GitHub branch.
# ---------------------------------------------------------------------------


class TestFetchPRMetadataGitHub:

    @patch.object(pr_metadata, "urlopen")
    def test_returns_head_base_and_changed_files(self, mock_urlopen):
        pr_resp = _mock_response({
            "head": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            "base": {
                "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "ref": "main",
            },
            "user": {"login": "octocat"},
            "title": "Ship the thing",
        })
        files_resp = _mock_response([
            {"filename": "src/a.py"},
            {"filename": "src/b.py"},
        ])
        mock_urlopen.side_effect = [pr_resp, files_resp]

        result = pr_metadata.fetch_pr_metadata(
            "github", 42,
            repository="example-owner/example-repo",
            github_token="ghp_xxx",
        )
        assert result.head_sha == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert result.base_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        assert result.base_branch == "main"
        assert result.author == "octocat"
        assert result.title == "Ship the thing"
        assert result.changed_files == ["src/a.py", "src/b.py"]

        pr_req = mock_urlopen.call_args_list[0].args[0]
        assert pr_req.full_url == (
            "https://api.github.com/repos/example-owner/example-repo/pulls/42"
        )
        assert pr_req.headers["Authorization"] == "Bearer ghp_xxx"
        assert pr_req.headers["Accept"] == "application/vnd.github+json"

        files_req = mock_urlopen.call_args_list[1].args[0]
        assert files_req.full_url == (
            "https://api.github.com/repos/example-owner/example-repo/"
            "pulls/42/files?per_page=100"
        )

    @patch.object(pr_metadata, "urlopen")
    def test_follows_link_header_for_pagination(self, mock_urlopen):
        pr_resp = _mock_response({
            "head": {"sha": "h"},
            "base": {"sha": "b", "ref": "main"},
            "user": {"login": "u"},
            "title": "t",
        })
        files_page_1 = _mock_response(
            [{"filename": "a"}],
            link_header=(
                '<https://api.github.com/repos/o/r/pulls/1/files?per_page=100&page=2>; '
                'rel="next", <https://.../page=5>; rel="last"'
            ),
        )
        files_page_2 = _mock_response([{"filename": "b"}])
        mock_urlopen.side_effect = [pr_resp, files_page_1, files_page_2]

        result = pr_metadata.fetch_pr_metadata(
            "github", 1, repository="o/r", github_token="ghp",
        )
        assert result.changed_files == ["a", "b"]
        # Third call was the paginated next URL.
        assert mock_urlopen.call_count == 3
        page_2_req = mock_urlopen.call_args_list[2].args[0]
        assert "page=2" in page_2_req.full_url

    @patch.object(pr_metadata, "urlopen")
    def test_missing_title_defaults_to_empty(self, mock_urlopen):
        pr_resp = _mock_response({
            "head": {"sha": "h"},
            "base": {"sha": "b", "ref": "main"},
            "user": {"login": "u"},
            # no title
        })
        files_resp = _mock_response([])
        mock_urlopen.side_effect = [pr_resp, files_resp]

        result = pr_metadata.fetch_pr_metadata(
            "github", 1, repository="o/r", github_token="ghp",
        )
        assert result.title == ""

    @patch.object(pr_metadata, "urlopen")
    def test_unexpected_shape_wraps_as_pr_metadata_error(self, mock_urlopen):
        # Missing head.sha
        mock_urlopen.return_value = _mock_response({"head": {}, "base": {"sha": "b", "ref": "m"}, "user": {"login": "u"}})
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata(
                "github", 1, repository="o/r", github_token="ghp",
            )

    @patch.object(pr_metadata, "urlopen")
    def test_network_error_wraps_as_pr_metadata_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("timeout")
        with pytest.raises(pr_metadata.PRMetadataError) as ctx:
            pr_metadata.fetch_pr_metadata(
                "github", 1, repository="o/r", github_token="ghp",
            )
        assert "timeout" in str(ctx.value)


# ---------------------------------------------------------------------------
# Azure DevOps branch.
# ---------------------------------------------------------------------------


class TestFetchPRMetadataAzureDevOps:

    @patch.object(pr_metadata, "urlopen")
    def test_returns_head_base_and_changed_files_from_ado(self, mock_urlopen):
        pr_resp = _mock_response({
            "lastMergeSourceCommit": {"commitId": "adohead1234"},
            "lastMergeTargetCommit": {"commitId": "adobase5678"},
            "targetRefName": "refs/heads/main",
            "createdBy": {"uniqueName": "user@example.com"},
            "title": "Ship the thing",
        })
        iterations_resp = _mock_response({
            "value": [
                {"id": 1},
                {"id": 2},
                {"id": 3},
            ],
        })
        # ADO's pull-request-iteration-changes endpoint returns entries
        # WITHOUT gitObjectType on item -- confirmed live 2026-07-01.
        # Only entries missing a path get skipped.
        changes_resp = _mock_response({
            "changeEntries": [
                {"item": {"path": "/src/a.py"}},
                {"item": {"path": "/src/b.py"}},
                # entry with no path -- skipped defensively.
                {"item": {"objectId": "abc"}},
            ],
        })
        mock_urlopen.side_effect = [pr_resp, iterations_resp, changes_resp]

        result = pr_metadata.fetch_pr_metadata(
            "azure_devops", 42,
            ado_org="example-org",
            ado_project="example-project",
            ado_repo="example-repo",
            ado_access_token="ado_bearer_xyz",
        )
        assert result.head_sha == "adohead1234"
        assert result.base_sha == "adobase5678"
        assert result.base_branch == "main"           # stripped refs/heads/
        assert result.author == "user@example.com"
        assert result.title == "Ship the thing"
        assert result.changed_files == ["src/a.py", "src/b.py"]  # no leading slash, no folder

    @patch.object(pr_metadata, "urlopen")
    def test_target_ref_without_prefix_used_as_is(self, mock_urlopen):
        # If ADO ever returns a bare branch name, don't slice off the wrong prefix.
        pr_resp = _mock_response({
            "lastMergeSourceCommit": {"commitId": "h"},
            "lastMergeTargetCommit": {"commitId": "b"},
            "targetRefName": "main",  # no refs/heads/
            "createdBy": {"uniqueName": "u"},
            "title": "t",
        })
        iterations_resp = _mock_response({"value": []})
        mock_urlopen.side_effect = [pr_resp, iterations_resp]

        result = pr_metadata.fetch_pr_metadata(
            "azure_devops", 1,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result.base_branch == "main"

    @patch.object(pr_metadata, "urlopen")
    def test_empty_iterations_returns_empty_changed_files(self, mock_urlopen):
        # Rare but real: a PR with no iterations yet (just opened,
        # before ADO computes the merge preview).
        pr_resp = _mock_response({
            "lastMergeSourceCommit": {"commitId": "h"},
            "lastMergeTargetCommit": {"commitId": "b"},
            "targetRefName": "refs/heads/main",
            "createdBy": {"uniqueName": "u"},
            "title": "t",
        })
        iterations_resp = _mock_response({"value": []})
        mock_urlopen.side_effect = [pr_resp, iterations_resp]

        result = pr_metadata.fetch_pr_metadata(
            "azure_devops", 1,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result.changed_files == []

    @patch.object(pr_metadata, "urlopen")
    def test_last_iteration_id_is_max_not_last(self, mock_urlopen):
        # If ADO returns iterations out of order, we should pick the
        # highest id, not the last-in-array. Regression guard against
        # a "just take iterations[-1]" naive fix.
        pr_resp = _mock_response({
            "lastMergeSourceCommit": {"commitId": "h"},
            "lastMergeTargetCommit": {"commitId": "b"},
            "targetRefName": "refs/heads/main",
            "createdBy": {"uniqueName": "u"},
            "title": "t",
        })
        iterations_resp = _mock_response({
            "value": [{"id": 3}, {"id": 1}, {"id": 2}],
        })
        changes_resp = _mock_response({"changeEntries": []})
        mock_urlopen.side_effect = [pr_resp, iterations_resp, changes_resp]

        pr_metadata.fetch_pr_metadata(
            "azure_devops", 1,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        changes_req = mock_urlopen.call_args_list[2].args[0]
        # Should have iteration 3 in the URL, not 1 or 2.
        assert "/iterations/3/changes" in changes_req.full_url

    @patch.object(pr_metadata, "urlopen")
    def test_author_falls_back_to_display_name(self, mock_urlopen):
        pr_resp = _mock_response({
            "lastMergeSourceCommit": {"commitId": "h"},
            "lastMergeTargetCommit": {"commitId": "b"},
            "targetRefName": "refs/heads/main",
            "createdBy": {"displayName": "Alice Example"},  # no uniqueName
            "title": "t",
        })
        iterations_resp = _mock_response({"value": []})
        mock_urlopen.side_effect = [pr_resp, iterations_resp]

        result = pr_metadata.fetch_pr_metadata(
            "azure_devops", 1,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result.author == "Alice Example"

    @patch.object(pr_metadata, "urlopen")
    def test_unexpected_shape_wraps_as_pr_metadata_error(self, mock_urlopen):
        # Missing lastMergeSourceCommit
        mock_urlopen.return_value = _mock_response({
            "lastMergeTargetCommit": {"commitId": "b"},
            "targetRefName": "refs/heads/main",
            "createdBy": {},
        })
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata(
                "azure_devops", 1,
                ado_org="o", ado_project="p", ado_repo="r",
                ado_access_token="tok",
            )

    @patch.object(pr_metadata, "urlopen")
    def test_pr_url_carries_api_version(self, mock_urlopen):
        pr_resp = _mock_response({
            "lastMergeSourceCommit": {"commitId": "h"},
            "lastMergeTargetCommit": {"commitId": "b"},
            "targetRefName": "refs/heads/main",
            "createdBy": {"uniqueName": "u"},
            "title": "t",
        })
        iterations_resp = _mock_response({"value": []})
        mock_urlopen.side_effect = [pr_resp, iterations_resp]

        pr_metadata.fetch_pr_metadata(
            "azure_devops", 42,
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        req = mock_urlopen.call_args_list[0].args[0]
        assert "api-version=7.0" in req.full_url
        assert req.headers["Authorization"] == "Bearer tok"


# ---------------------------------------------------------------------------
# _parse_github_link_next helper directly.
# ---------------------------------------------------------------------------


class TestParseGitHubLinkNext:

    def test_returns_none_for_empty_header(self):
        assert pr_metadata._parse_github_link_next("") is None

    def test_returns_next_url_from_single_link(self):
        header = '<https://api.github.com/x?page=2>; rel="next"'
        assert pr_metadata._parse_github_link_next(header) == (
            "https://api.github.com/x?page=2"
        )

    def test_returns_next_from_multi_link(self):
        header = (
            '<https://api.github.com/x?page=2>; rel="next", '
            '<https://api.github.com/x?page=5>; rel="last"'
        )
        assert pr_metadata._parse_github_link_next(header) == (
            "https://api.github.com/x?page=2"
        )

    def test_returns_none_when_next_absent(self):
        header = '<https://api.github.com/x?page=5>; rel="last"'
        assert pr_metadata._parse_github_link_next(header) is None
