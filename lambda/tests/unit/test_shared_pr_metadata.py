# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
        # pr + 2 file pages + merge-base compare (falls back when exhausted)
        assert mock_urlopen.call_count == 4
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


class TestAdoChangeTypeToGitHubStatus:
    """Map ADO's ``changeType`` -> GitHub's ``status`` vocabulary so
    downstream check-selection code (written to GitHub semantics)
    treats both providers uniformly. Prior to the mapping, ADO PRs
    fired BREAKING_CHANGES on newly-added handler files because the
    analyze-pr wrapper force-labelled everything ``"modified"``.
    """

    def test_add_maps_to_added(self):
        assert pr_metadata._ado_change_type_to_github_status("add") == "added"

    def test_edit_maps_to_modified(self):
        assert pr_metadata._ado_change_type_to_github_status("edit") == "modified"

    def test_delete_maps_to_removed(self):
        assert pr_metadata._ado_change_type_to_github_status("delete") == "removed"

    def test_rename_maps_to_renamed(self):
        assert pr_metadata._ado_change_type_to_github_status("rename") == "renamed"

    def test_undelete_maps_to_added(self):
        assert pr_metadata._ado_change_type_to_github_status("undelete") == "added"

    def test_delete_wins_over_edit_in_combo(self):
        # ADO can emit comma-joined combos like "edit, delete" for
        # unusual cases; delete dominates so downstream sees "removed".
        assert pr_metadata._ado_change_type_to_github_status(
            "edit, delete"
        ) == "removed"

    def test_add_wins_over_rename_in_combo(self):
        # Copy-into-new-path shows up as "add, rename"; we want
        # "added" so BREAKING_CHANGES stays skipped for the new file.
        assert pr_metadata._ado_change_type_to_github_status(
            "add, rename"
        ) == "added"

    def test_empty_defaults_to_modified(self):
        assert pr_metadata._ado_change_type_to_github_status("") == "modified"
        assert pr_metadata._ado_change_type_to_github_status(None) == "modified"

    def test_unknown_defaults_to_modified(self):
        assert pr_metadata._ado_change_type_to_github_status(
            "some-future-value"
        ) == "modified"

    def test_ado_pr_metadata_populates_file_change_types(self):
        # End-to-end: fetch_pr_metadata for ADO surfaces per-path
        # GitHub-style status in file_change_types.
        with patch.object(pr_metadata, "urlopen") as mock_urlopen:
            pr_resp = _mock_response({
                "lastMergeSourceCommit": {"commitId": "h"},
                "lastMergeTargetCommit": {"commitId": "b"},
                "targetRefName": "refs/heads/main",
                "createdBy": {"uniqueName": "u"},
                "title": "t",
            })
            iterations_resp = _mock_response({"value": [{"id": 1}]})
            changes_resp = _mock_response({
                "changeEntries": [
                    {"item": {"path": "/new.py"}, "changeType": "add"},
                    {"item": {"path": "/old.py"}, "changeType": "edit"},
                    {"item": {"path": "/gone.py"}, "changeType": "delete"},
                ],
            })
            mock_urlopen.side_effect = [pr_resp, iterations_resp, changes_resp]

            result = pr_metadata.fetch_pr_metadata(
                "azure_devops", 1,
                ado_org="o", ado_project="p", ado_repo="r",
                ado_access_token="tok",
            )

        assert result.file_change_types == {
            "new.py": "added",
            "old.py": "modified",
            "gone.py": "removed",
        }

    def test_github_pr_metadata_populates_file_change_types(self):
        with patch.object(pr_metadata, "urlopen") as mock_urlopen:
            pr_resp = _mock_response({
                "head": {"sha": "h"},
                "base": {"sha": "b", "ref": "main"},
                "user": {"login": "u"},
                "title": "t",
            })
            files_resp = _mock_response([
                {"filename": "new.py", "status": "added"},
                {"filename": "old.py", "status": "modified"},
                {"filename": "gone.py", "status": "removed"},
            ])
            mock_urlopen.side_effect = [pr_resp, files_resp]

            result = pr_metadata.fetch_pr_metadata(
                "github", 1,
                repository="owner/repo",
                github_token="ghp",
            )

        assert result.file_change_types == {
            "new.py": "added",
            "old.py": "modified",
            "gone.py": "removed",
        }


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


# ---------------------------------------------------------------------------
# GitLab branch.
# ---------------------------------------------------------------------------


class TestFetchPRMetadataGitLab:

    def test_gitlab_requires_project_path_and_token(self):
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata(
                "gitlab", 1, gitlab_project_path="g/p")
        with pytest.raises(pr_metadata.PRMetadataError):
            pr_metadata.fetch_pr_metadata(
                "gitlab", 1, gitlab_access_token="tok")

    def test_gitlab_mr_shape_normalized(self):
        mr = {
            "diff_refs": {"head_sha": "h" * 40, "base_sha": "b" * 40},
            "target_branch": "main",
            "author": {"username": "bcarpio"},
            "title": "Add licensing tier",
        }
        diffs = [
            {"new_path": "src/added.py", "old_path": "src/added.py",
             "new_file": True, "deleted_file": False, "renamed_file": False},
            {"new_path": "src/edited.py", "old_path": "src/edited.py",
             "new_file": False, "deleted_file": False, "renamed_file": False},
            {"new_path": "src/renamed_to.py", "old_path": "src/renamed_from.py",
             "new_file": False, "deleted_file": False, "renamed_file": True},
            {"new_path": "src/gone.py", "old_path": "src/gone.py",
             "new_file": False, "deleted_file": True, "renamed_file": False},
        ]
        with patch("shared.gitlab_api.get_merge_request", return_value=mr), \
             patch("shared.gitlab_api.get_merge_request_diffs",
                   return_value=diffs):
            meta = pr_metadata.fetch_pr_metadata(
                "gitlab", 7,
                gitlab_project_path="group/subgroup/project",
                gitlab_access_token="tok",
            )
        assert meta.head_sha == "h" * 40
        assert meta.base_sha == "b" * 40
        assert meta.base_branch == "main"
        assert meta.author == "bcarpio"
        assert meta.title == "Add licensing tier"
        # Explicit booleans -> GitHub status vocab; delete keys on
        # old_path, rename keys on new_path.
        assert meta.file_change_types["src/added.py"] == "added"
        assert meta.file_change_types["src/edited.py"] == "modified"
        assert meta.file_change_types["src/renamed_to.py"] == "renamed"
        assert meta.file_change_types["src/gone.py"] == "removed"
        assert "src/renamed_from.py" not in meta.file_change_types
        assert set(meta.changed_files) == {
            "src/added.py", "src/edited.py", "src/renamed_to.py",
            "src/gone.py",
        }

    def test_gitlab_missing_diff_refs_raises(self):
        with patch("shared.gitlab_api.get_merge_request",
                   return_value={"target_branch": "main"}):
            with pytest.raises(pr_metadata.PRMetadataError):
                pr_metadata.fetch_pr_metadata(
                    "gitlab", 7,
                    gitlab_project_path="g/p",
                    gitlab_access_token="tok",
                )


class TestGitlabDiffToGithubStatus:

    def test_precedence_delete_over_all(self):
        assert pr_metadata._gitlab_diff_to_github_status(
            {"deleted_file": True, "new_file": True, "renamed_file": True},
        ) == "removed"

    def test_add_over_rename(self):
        assert pr_metadata._gitlab_diff_to_github_status(
            {"new_file": True, "renamed_file": True},
        ) == "added"

    def test_plain_edit_is_modified(self):
        assert pr_metadata._gitlab_diff_to_github_status({}) == "modified"


# ---------------------------------------------------------------------------
# Merge base: diff from where the PR branched, not the target branch tip.
# ---------------------------------------------------------------------------


class TestMergeBase:

    def _github_pr(self):
        return _mock_response({
            "head": {"sha": "h" * 40},
            "base": {"sha": "t" * 40, "ref": "main"},
            "user": {"login": "octocat"},
            "title": "x",
        })

    @patch.object(pr_metadata, "urlopen")
    def test_github_uses_compare_merge_base(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._github_pr(),
            _mock_response([{"filename": "a.py"}]),
            _mock_response({"merge_base_commit": {"sha": "m" * 40}}),
        ]
        meta = pr_metadata.fetch_pr_metadata(
            "github", 7, repository="o/r", github_token="tok")
        assert meta.base_sha == "m" * 40
        compare_req = mock_urlopen.call_args_list[2].args[0]
        assert f"/compare/{'t' * 40}...{'h' * 40}" in compare_req.full_url

    @patch.object(pr_metadata, "urlopen")
    def test_github_compare_failure_keeps_base_sha(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._github_pr(),
            _mock_response([{"filename": "a.py"}]),
            RuntimeError("compare down"),
        ]
        meta = pr_metadata.fetch_pr_metadata(
            "github", 7, repository="o/r", github_token="tok")
        assert meta.base_sha == "t" * 40

    @patch.object(pr_metadata, "urlopen")
    def test_github_compare_without_merge_base_keeps_base_sha(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._github_pr(),
            _mock_response([{"filename": "a.py"}]),
            _mock_response({}),
        ]
        meta = pr_metadata.fetch_pr_metadata(
            "github", 7, repository="o/r", github_token="tok")
        assert meta.base_sha == "t" * 40

    def _ado_responses(self, merge_bases):
        return [
            _mock_response({
                "lastMergeSourceCommit": {"commitId": "src"},
                "lastMergeTargetCommit": {"commitId": "tgt"},
                "targetRefName": "refs/heads/main",
                "createdBy": {"uniqueName": "u"},
                "title": "x",
            }),
            _mock_response({"value": [{"id": 1}]}),
            _mock_response({"changeEntries": [{"item": {"path": "/a.py"}}]}),
            merge_bases,
        ]

    @patch.object(pr_metadata, "urlopen")
    def test_ado_uses_merge_bases_api(self, mock_urlopen):
        mock_urlopen.side_effect = self._ado_responses(
            _mock_response({"value": [{"commitId": "mb"}], "count": 1}))
        meta = pr_metadata.fetch_pr_metadata(
            "azure_devops", 3, ado_org="org", ado_project="proj",
            ado_repo="repo", ado_access_token="tok")
        assert meta.base_sha == "mb"
        url = mock_urlopen.call_args_list[3].args[0].full_url
        assert "/commits/src/mergebases" in url and "otherCommitId=tgt" in url

    @patch.object(pr_metadata, "urlopen")
    def test_ado_merge_base_failure_keeps_target(self, mock_urlopen):
        mock_urlopen.side_effect = self._ado_responses(RuntimeError("404"))
        meta = pr_metadata.fetch_pr_metadata(
            "azure_devops", 3, ado_org="org", ado_project="proj",
            ado_repo="repo", ado_access_token="tok")
        assert meta.base_sha == "tgt"

    @patch.object(pr_metadata, "urlopen")
    def test_ado_empty_merge_bases_keeps_target(self, mock_urlopen):
        mock_urlopen.side_effect = self._ado_responses(
            _mock_response({"value": [], "count": 0}))
        meta = pr_metadata.fetch_pr_metadata(
            "azure_devops", 3, ado_org="org", ado_project="proj",
            ado_repo="repo", ado_access_token="tok")
        assert meta.base_sha == "tgt"
