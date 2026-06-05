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


class TestGitHubBranch:

    @patch.object(source_fetch, "urlopen")
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
