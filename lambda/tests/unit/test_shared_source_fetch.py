# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared.source_fetch — provider-aware code-map source fetcher."""

from unittest.mock import MagicMock, patch

import pytest

from shared import source_fetch


@pytest.fixture(autouse=True)
def _reset_s3_cache():
    source_fetch._s3_client = None
    yield
    source_fetch._s3_client = None


class TestProviderDispatch:

    def test_unknown_provider_raises(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_repo_file("bitbucket", "file.py")

    def test_azure_devops_requires_workspace_slug_bucket(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_repo_file("azure_devops", "file.py")
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_repo_file(
                "azure_devops", "file.py",
                workspace_id="ws-1",  # missing slug + kb_bucket
            )

    def test_github_requires_repo_project_branch_token(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_repo_file(
                "github", "file.py",
                repo_project="o/r",  # missing default_branch + github_token
            )


class TestAzureDevOpsBranch:

    @patch.object(source_fetch, "_get_s3")
    def test_reads_from_s3_mirror_at_expected_key(self, mock_get_s3):
        # The key shape MUST match what azure-devops-sync wrote
        # (workspaces/{ws}/azure_devops/{slug}/{path} with the leading
        # slash on the path normalized away). If this drifts the
        # downstream code-map worker reads from the wrong key and
        # 404s -- but still has 1 grant on the right prefix.
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"actual content")),
        }
        mock_get_s3.return_value = mock_s3

        result = source_fetch.fetch_repo_file(
            "azure_devops",
            "/src/main.py",  # leading slash like ADO returns
            workspace_id="ws-xyz",
            slug="example-org/example-project/example-repo",
            kb_bucket="kb-bucket",
        )
        assert result == "actual content"

        mock_s3.get_object.assert_called_once_with(
            Bucket="kb-bucket",
            Key="workspaces/ws-xyz/azure_devops/example-org/example-project/example-repo/src/main.py",
        )

    @patch.object(source_fetch, "_get_s3")
    def test_s3_read_error_wraps_as_source_fetch_error(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        mock_get_s3.return_value = mock_s3
        with pytest.raises(source_fetch.SourceFetchError) as ctx:
            source_fetch.fetch_repo_file(
                "azure_devops", "/x.py",
                workspace_id="ws-1", slug="o/p/r", kb_bucket="b",
            )
        assert "NoSuchKey" in str(ctx.value)

    @patch.object(source_fetch, "_get_s3")
    def test_utf8_replace_on_bad_bytes(self, mock_get_s3):
        # A single bad byte shouldn't crash the whole batch -- we
        # decode with errors="replace" so the pipeline keeps moving.
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"good\xff text")),
        }
        mock_get_s3.return_value = mock_s3
        result = source_fetch.fetch_repo_file(
            "azure_devops", "/x.py",
            workspace_id="ws-1", slug="o/p/r", kb_bucket="b",
        )
        assert "good" in result
        assert "text" in result


class TestGitLabBranch:
    """ADR-050: gitlab-sync mirrors source to S3 first (the ADO pattern),
    so fetch_repo_file reads the latest snapshot from the S3 mirror at
    ``workspaces/{ws}/gitlab/{slug}/{path}``."""

    def test_gitlab_requires_workspace_slug_bucket(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_repo_file("gitlab", "file.py")
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_repo_file(
                "gitlab", "file.py",
                workspace_id="ws-1",  # missing slug + kb_bucket
            )

    @patch.object(source_fetch, "_get_s3")
    def test_reads_from_s3_mirror_with_nested_subgroup_slug(self, mock_get_s3):
        # Nested-subgroup slug has a VARIABLE number of slashes -- the S3
        # key must preserve every segment of the slug verbatim, or the
        # code-map worker reads the wrong key.
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"gitlab content")),
        }
        mock_get_s3.return_value = mock_s3

        result = source_fetch.fetch_repo_file(
            "gitlab",
            "/src/main.py",  # leading slash normalized
            workspace_id="ws-xyz",
            slug="acme/platform/backend/api",  # group/subgroup/subgroup/project
            kb_bucket="kb-bucket",
        )
        assert result == "gitlab content"
        mock_s3.get_object.assert_called_once_with(
            Bucket="kb-bucket",
            Key="workspaces/ws-xyz/gitlab/acme/platform/backend/api/src/main.py",
        )

    @patch.object(source_fetch, "_get_s3")
    def test_s3_read_error_wraps_as_source_fetch_error(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        mock_get_s3.return_value = mock_s3
        with pytest.raises(source_fetch.SourceFetchError) as ctx:
            source_fetch.fetch_repo_file(
                "gitlab", "/x.py",
                workspace_id="ws-1", slug="grp/proj", kb_bucket="b",
            )
        assert "NoSuchKey" in str(ctx.value)


class TestGitHubBranch:

    @patch("shared.github_backoff.github_urllib_request")
    def test_constructs_raw_url_and_carries_bearer(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"some github content"
        mock_urlopen.return_value = mock_resp

        result = source_fetch.fetch_repo_file(
            "github",
            "src/main.py",
            repo_project="example-owner/example-repo",
            default_branch="main",
            github_token="ghp_xxx",
        )
        assert result == "some github content"

        req = mock_urlopen.call_args.args[0]
        assert req.full_url == (
            "https://raw.githubusercontent.com/example-owner/example-repo/main/src/main.py"
        )
        assert req.headers["Authorization"] == "Bearer ghp_xxx"


# ---------------------------------------------------------------------------
# fetch_file_at_sha — of the PR-analyzer ADO parity plan.
# Same shape as fetch_repo_file but reads at a specific commit SHA and
# returns raw bytes rather than decoded text.
# ---------------------------------------------------------------------------


class TestFetchFileAtShaDispatch:

    def test_unknown_provider_raises(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha("bitbucket", "file.py", "deadbee")

    def test_github_requires_repository_and_token(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "github", "file.py", "abc123",
                repository="o/r",  # missing github_token
            )
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "github", "file.py", "abc123",
                github_token="ghp",  # missing repository
            )

    def test_gitlab_requires_project_path_and_token(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "gitlab", "file.py", "abc123",
                gitlab_project_path="grp/proj",  # missing token
            )
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "gitlab", "file.py", "abc123",
                gitlab_access_token="tok",  # missing project path
            )

    def test_azure_devops_requires_all_ado_kwargs(self):
        # ado_access_token missing
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "azure_devops", "file.py", "abc123",
                ado_org="org", ado_project="proj", ado_repo="repo",
            )
        # ado_repo missing
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "azure_devops", "file.py", "abc123",
                ado_org="org", ado_project="proj",
                ado_access_token="tok",
            )


class TestFetchFileAtShaGitHub:

    @patch("shared.github_backoff.github_urllib_request")
    def test_uses_sha_in_place_of_branch_ref(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"file bytes at sha"
        mock_urlopen.return_value = mock_resp

        result = source_fetch.fetch_file_at_sha(
            "github",
            "src/main.py",
            "abc1234deadbeefcafebabe0000000000000000",
            repository="example-owner/example-repo",
            github_token="ghp_xxx",
        )
        assert result == b"file bytes at sha"

        req = mock_urlopen.call_args.args[0]
        assert req.full_url == (
            "https://raw.githubusercontent.com/example-owner/example-repo/"
            "abc1234deadbeefcafebabe0000000000000000/src/main.py"
        )
        assert req.headers["Authorization"] == "Bearer ghp_xxx"

    @patch("shared.github_backoff.github_urllib_request")
    def test_strips_leading_slash_on_file_path(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_urlopen.return_value = mock_resp

        source_fetch.fetch_file_at_sha(
            "github",
            "/src/main.py",   # leading slash
            "abc",
            repository="o/r",
            github_token="ghp",
        )
        req = mock_urlopen.call_args.args[0]
        assert "raw.githubusercontent.com/o/r/abc/src/main.py" in req.full_url

    @patch("shared.github_backoff.github_urllib_request")
    def test_returns_raw_bytes_not_decoded_text(self, mock_urlopen):
        # LICENSE_COMPLIANCE / TEST_COVERAGE may inspect binary lock
        # files; we return bytes so the caller decides how to decode.
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"\x00\x01\x02\xff"
        mock_urlopen.return_value = mock_resp

        result = source_fetch.fetch_file_at_sha(
            "github", "x.bin", "abc",
            repository="o/r", github_token="ghp",
        )
        assert result == b"\x00\x01\x02\xff"
        assert isinstance(result, bytes)

    @patch("shared.github_backoff.github_urllib_request")
    def test_network_error_wraps_as_source_fetch_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection reset")
        with pytest.raises(source_fetch.SourceFetchError) as ctx:
            source_fetch.fetch_file_at_sha(
                "github", "x.py", "abc",
                repository="o/r", github_token="ghp",
            )
        assert "connection reset" in str(ctx.value)


class TestFetchFileAtShaAzureDevOps:

    @patch.object(source_fetch, "urlopen")
    def test_constructs_ado_items_url_with_commit_version_type(self, mock_urlopen):
        # The load-bearing bits are (a) versionType=commit rather than
        # branch, (b) download=true so ADO returns raw bytes not a JSON
        # object wrapper, and (c) Accept: */* on the client side so ADO
        # honors download=true. Live-burned 2026-06-05.
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"ado file bytes"
        mock_urlopen.return_value = mock_resp

        result = source_fetch.fetch_file_at_sha(
            "azure_devops",
            "src/foo.py",
            "abc1234deadbeef",
            ado_org="example-org",
            ado_project="example-project",
            ado_repo="example-repo",
            ado_access_token="ado_bearer_xyz",
        )
        assert result == b"ado file bytes"

        req = mock_urlopen.call_args.args[0]
        url = req.full_url
        assert url.startswith(
            "https://dev.azure.com/example-org/example-project/_apis/git/"
            "repositories/example-repo/items?"
        )
        # Query params must all appear -- ADO is unforgiving about
        # missing versionType or download flags.
        assert "path=%2Fsrc%2Ffoo.py" in url
        assert "download=true" in url
        assert "versionDescriptor.version=abc1234deadbeef" in url
        assert "versionDescriptor.versionType=commit" in url
        assert "api-version=7.0" in url

        assert req.headers["Authorization"] == "Bearer ado_bearer_xyz"
        assert req.headers["Accept"] == "*/*"

    @patch.object(source_fetch, "urlopen")
    def test_prepends_slash_on_file_path_for_ado(self, mock_urlopen):
        # ADO's items API expects the path leading-slashed; the caller
        # may pass either form so we normalize.
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_urlopen.return_value = mock_resp

        source_fetch.fetch_file_at_sha(
            "azure_devops", "src/foo.py", "abc",
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        req = mock_urlopen.call_args.args[0]
        # URL-encoded leading slash → %2F
        assert "path=%2Fsrc%2Ffoo.py" in req.full_url

    @patch.object(source_fetch, "urlopen")
    def test_returns_raw_bytes_from_ado(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"\xde\xad\xbe\xef"
        mock_urlopen.return_value = mock_resp

        result = source_fetch.fetch_file_at_sha(
            "azure_devops", "x.bin", "abc",
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        assert result == b"\xde\xad\xbe\xef"

    @patch.object(source_fetch, "urlopen")
    def test_ado_network_error_wraps_as_source_fetch_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("401 Unauthorized")
        with pytest.raises(source_fetch.SourceFetchError) as ctx:
            source_fetch.fetch_file_at_sha(
                "azure_devops", "x.py", "abc",
                ado_org="o", ado_project="p", ado_repo="r",
                ado_access_token="tok",
            )
        assert "401 Unauthorized" in str(ctx.value)

    @patch.object(source_fetch, "urlopen")
    def test_ado_version_type_branch_used_in_url(self, mock_urlopen):
        # ado_version_type="branch" lets callers read files
        # at default-branch tip (used by TEST_COVERAGE for
        # .outcomeops.yaml).
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"branch content"
        mock_urlopen.return_value = mock_resp

        source_fetch.fetch_file_at_sha(
            "azure_devops", ".outcomeops.yaml", "main",
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
            ado_version_type="branch",
        )
        req = mock_urlopen.call_args.args[0]
        assert "versionDescriptor.versionType=branch" in req.full_url
        assert "versionDescriptor.version=main" in req.full_url

    def test_ado_version_type_rejects_unknown_value(self):
        with pytest.raises(source_fetch.SourceFetchError):
            source_fetch.fetch_file_at_sha(
                "azure_devops", "x", "abc",
                ado_org="o", ado_project="p", ado_repo="r",
                ado_access_token="tok",
                ado_version_type="tag",  # not one of commit / branch
            )

    @patch.object(source_fetch, "urlopen")
    def test_ado_version_type_defaults_to_commit(self, mock_urlopen):
        # behavior preserved: without ado_version_type,
        # default is "commit".
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_urlopen.return_value = mock_resp

        source_fetch.fetch_file_at_sha(
            "azure_devops", "x.py", "abc1234",
            ado_org="o", ado_project="p", ado_repo="r",
            ado_access_token="tok",
        )
        req = mock_urlopen.call_args.args[0]
        assert "versionDescriptor.versionType=commit" in req.full_url


class TestFetchFileAtShaGitLab:
    """ADR-050: fetch_file_at_sha delegates to the shared GitLab API v4
    raw-file client. ``sha`` is passed through as the ``ref``; GitLab
    accepts a SHA, branch, or tag uniformly in that slot."""

    @patch("shared.gitlab_api.get_file_content")
    def test_passes_sha_as_ref_and_strips_leading_slash(self, mock_get):
        mock_get.return_value = b"gitlab bytes at sha"
        result = source_fetch.fetch_file_at_sha(
            "gitlab",
            "/src/main.py",  # leading slash normalized to no-leading-slash
            "abc1234deadbeef",
            gitlab_project_path="acme/platform/backend/api",
            gitlab_access_token="glpat-xyz",
        )
        assert result == b"gitlab bytes at sha"
        # Positional call: (access_token, project_path, file_path, ref)
        args = mock_get.call_args.args
        assert args[0] == "glpat-xyz"
        assert args[1] == "acme/platform/backend/api"
        assert args[2] == "src/main.py"  # leading slash stripped
        assert args[3] == "abc1234deadbeef"  # sha in the ref slot

    @patch("shared.gitlab_api.get_file_content")
    def test_returns_raw_bytes(self, mock_get):
        mock_get.return_value = b"\xde\xad\xbe\xef"
        result = source_fetch.fetch_file_at_sha(
            "gitlab", "x.bin", "abc",
            gitlab_project_path="grp/proj",
            gitlab_access_token="tok",
        )
        assert result == b"\xde\xad\xbe\xef"
        assert isinstance(result, bytes)

    @patch("shared.gitlab_api.get_file_content")
    def test_network_error_wraps_as_source_fetch_error(self, mock_get):
        mock_get.side_effect = Exception("401 Unauthorized")
        with pytest.raises(source_fetch.SourceFetchError) as ctx:
            source_fetch.fetch_file_at_sha(
                "gitlab", "x.py", "abc",
                gitlab_project_path="grp/proj",
                gitlab_access_token="tok",
            )
        assert "401 Unauthorized" in str(ctx.value)
