"""Unit tests for the Azure DevOps REST client (chunk 03).

The module under test is a thin set of urllib wrappers; the tests
target three things:

1. **URL construction** -- correct host (vssps for accounts,
   dev.azure.com for everything else), correct path, correct query
   params, ``api-version=7.0`` always present.
2. **Headers** -- ``Authorization: Bearer ...``, ``User-Agent``
   (operator-debuggable), ``Content-Type`` on POSTs.
3. **Error mapping** -- 401/403/404/5xx each become the typed
   exception the caller is allowed to catch.

Pagination has its own section -- both ADO styles
(continuation-token via response header, single-page body) are
exercised separately so a future contributor who breaks one branch
doesn't silently rely on the other.
"""

import importlib.util
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


def _load_module(mod_name, filename, directory):
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# Chunk 16: consolidated to lambda/shared/azure_devops_api.py. Import via
# the ordinary package path so all consumers share the same module object.
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)
import shared.azure_devops_api as api  # noqa: E402
# Legacy alias so pre-consolidation ``patch("azdo_int_api.X")`` targets
# still resolve to the canonical shared module (same object identity, so
# `except X` clauses in handlers still catch what tests raise).
sys.modules["azdo_int_api"] = api

_VENDORED_COPY_DIRS = [
    "../../shared",
]


@pytest.fixture(
    scope="module",
    autouse=True,
    params=_VENDORED_COPY_DIRS,
    ids=[d.rsplit("/", 1)[-1] for d in _VENDORED_COPY_DIRS],
)
def _vendored_copy(request):
    # Chunk 16: only one copy now (the shared module). Nothing to rebind --
    # the module-level `import shared.azure_devops_api as api` above already
    # points at the single canonical module. Kept as a no-op fixture so the
    # per-copy parametrize test IDs stay stable in the coverage report.
    yield


# ---------------------------------------------------------------------------
# urlopen response builder
# ---------------------------------------------------------------------------


def _resp(body=None, headers=None, raw_bytes=None):
    """Build a context-manager-shaped mock matching urlopen's contract."""
    response = MagicMock()
    if raw_bytes is not None:
        response.read.return_value = raw_bytes
    elif body is None:
        response.read.return_value = b""
    else:
        response.read.return_value = json.dumps(body).encode("utf-8")
    response.headers = headers or {}
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def _http_error(code, body=b""):
    err = urllib.error.HTTPError(
        url="https://dev.azure.com/x", code=code, msg="err", hdrs={}, fp=None,
    )
    # Simulate a readable body
    err.fp = MagicMock()
    err.read = MagicMock(return_value=body)
    return err


# ---------------------------------------------------------------------------
# Header / User-Agent contract -- every call must carry these
# ---------------------------------------------------------------------------


class TestHeaders:
    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_carries_bearer_and_user_agent(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        api.list_repos("tok", "example-org", "example-project")
        req = mock_urlopen.call_args.args[0]
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["User-agent"].startswith("OutcomeOps-AIAssist/")
        assert req.headers["Accept"] == "application/json"
        assert req.get_method() == "GET"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_post_carries_content_type(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 1})
        api.post_pull_request_thread(
            "tok", "example-org", "example-project", "example-repo", 42, "hello",
        )
        req = mock_urlopen.call_args.args[0]
        assert req.headers["Content-type"] == "application/json"
        assert req.get_method() == "POST"

    def test_user_agent_constant_includes_env_and_app(self):
        # Operators inspect ADO request logs to figure out which env
        # is calling. Drift here makes that ambiguous.
        assert "OutcomeOps-AIAssist/" in api.USER_AGENT
        # We don't pin ENV/APP_NAME exact values -- they're driven by
        # env vars at import time -- but we pin the prefix shape.


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


class TestUrlConstruction:

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_list_accessible_orgs_hits_vssps_with_memberid(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        api.list_accessible_orgs("tok", member_id="user-uuid")
        url = mock_urlopen.call_args.args[0].full_url
        # vssps -- legacy identity host. Don't change to dev.azure.com.
        assert url.startswith("https://app.vssps.visualstudio.com/_apis/accounts")
        assert "memberId=user-uuid" in url
        assert "api-version=7.0" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_list_projects_hits_dev_azure(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        api.list_projects("tok", "example-org")
        url = mock_urlopen.call_args.args[0].full_url
        assert url.startswith("https://dev.azure.com/example-org/_apis/projects")
        assert "api-version=7.0" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_list_repos_url(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        api.list_repos("tok", "example-org", "example-project")
        url = mock_urlopen.call_args.args[0].full_url
        assert (
            "https://dev.azure.com/example-org/example-project/_apis/git/repositories"
            in url
        )
        assert "api-version=7.0" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_repo_url(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"defaultBranch": "refs/heads/main"})
        api.get_repo("tok", "example-org", "example-project", "example-repo")
        url = mock_urlopen.call_args.args[0].full_url
        assert (
            "https://dev.azure.com/example-org/example-project/_apis/git/repositories/example-repo"
            in url
        )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_latest_commit_sha_url_has_branch_and_top(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": [{"commitId": "abc123"}]})
        api.get_latest_commit_sha(
            "tok", "example-org", "example-project", "example-repo", "main",
        )
        url = mock_urlopen.call_args.args[0].full_url
        assert "/commits" in url
        assert "searchCriteria.itemVersion.version=main" in url
        # %24 is URL-encoded "$" -- ADO's $top param
        assert "%24top=1" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_tree_recursive_url(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        api.get_tree_recursive(
            "tok", "example-org", "example-project", "example-repo", "main",
        )
        url = mock_urlopen.call_args.args[0].full_url
        assert "/items" in url
        assert "scopePath=%2F" in url  # "/" URL-encoded
        assert "recursionLevel=Full" in url
        assert "versionDescriptor.version=main" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_file_content_url_has_path_and_download(self, mock_urlopen):
        mock_urlopen.return_value = _resp(raw_bytes=b"file body")
        api.get_file_content(
            "tok", "example-org", "example-project", "example-repo",
            "/src/foo.py", "main",
        )
        url = mock_urlopen.call_args.args[0].full_url
        assert "path=%2Fsrc%2Ffoo.py" in url
        assert "download=true" in url
        assert "versionDescriptor.version=main" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_file_content_sends_wildcard_accept(self, mock_urlopen):
        # Regression guard for the 2026-06-05 prd bug. The default
        # Accept: application/json on every other endpoint must NOT
        # carry over to get_file_content -- ADO honors Accept and
        # returns the JSON metadata wrapper (objectId/url/blob href
        # descriptor, ~900 bytes) instead of the actual blob bytes.
        # The download=true query param alone is NOT enough.
        mock_urlopen.return_value = _resp(raw_bytes=b"the actual source")
        api.get_file_content(
            "tok", "example-org", "example-project", "example-repo",
            "/README.md", "main",
        )
        req = mock_urlopen.call_args.args[0]
        # urllib normalizes header keys to title-case
        assert req.headers["Accept"] == "*/*"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_list_open_pulls_url(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        api.list_open_pulls(
            "tok", "example-org", "example-project", "example-repo",
        )
        url = mock_urlopen.call_args.args[0].full_url
        assert "/pullrequests" in url
        assert "searchCriteria.status=active" in url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_post_pull_request_thread_url(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 99})
        api.post_pull_request_thread(
            "tok", "example-org", "example-project", "example-repo", 42, "hi",
        )
        url = mock_urlopen.call_args.args[0].full_url
        assert "/pullRequests/42/threads" in url
        assert "api-version=7.0" in url


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_list_repos_returns_value_array(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": [{"name": "r1"}, {"name": "r2"}]})
        out = api.list_repos("tok", "example-org", "example-project")
        assert [r["name"] for r in out] == ["r1", "r2"]

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_repo_returns_body_dict(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"defaultBranch": "refs/heads/develop"})
        out = api.get_repo("tok", "example-org", "example-project", "example-repo")
        assert out["defaultBranch"] == "refs/heads/develop"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_default_branch_strips_refs_heads(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"defaultBranch": "refs/heads/main"})
        assert (
            api.get_default_branch("tok", "example-org", "example-project", "example-repo")
            == "main"
        )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_default_branch_passes_through_when_no_prefix(self, mock_urlopen):
        # Defensive: if ADO ever returns a short ref, we don't double-strip.
        mock_urlopen.return_value = _resp({"defaultBranch": "trunk"})
        assert (
            api.get_default_branch("tok", "example-org", "example-project", "example-repo")
            == "trunk"
        )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_default_branch_returns_none_when_missing(self, mock_urlopen):
        mock_urlopen.return_value = _resp({})
        assert (
            api.get_default_branch("tok", "example-org", "example-project", "example-repo")
            is None
        )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_latest_commit_sha_returns_first_commit_id(self, mock_urlopen):
        mock_urlopen.return_value = _resp(
            {"value": [{"commitId": "abc123"}, {"commitId": "def456"}]}
        )
        sha = api.get_latest_commit_sha(
            "tok", "example-org", "example-project", "example-repo", "main",
        )
        assert sha == "abc123"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_latest_commit_sha_none_on_empty(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        sha = api.get_latest_commit_sha(
            "tok", "example-org", "example-project", "example-repo", "main",
        )
        assert sha is None

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_file_content_returns_raw_bytes(self, mock_urlopen):
        # Binary file content; do NOT JSON-parse.
        mock_urlopen.return_value = _resp(raw_bytes=b"\x00\x01\x02 not json")
        out = api.get_file_content(
            "tok", "example-org", "example-project", "example-repo",
            "/bin.so", "main",
        )
        assert out == b"\x00\x01\x02 not json"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_tree_recursive_returns_value_array(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": [
            {"path": "/a.py", "gitObjectType": "blob", "objectId": "sha-a"},
            {"path": "/b", "gitObjectType": "tree", "objectId": "sha-b"},
        ]})
        items = api.get_tree_recursive(
            "tok", "example-org", "example-project", "example-repo", "main",
        )
        assert [i["path"] for i in items] == ["/a.py", "/b"]


# ---------------------------------------------------------------------------
# POST body shape (PR thread)
# ---------------------------------------------------------------------------


class TestPullRequestThreadBody:

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_body_has_comments_with_type_1_status_1(self, mock_urlopen):
        # ADO's PR thread API rejects payloads that don't match this
        # exact shape. Pinning here so a refactor can't silently break
        # the chunk-07 PR-comment posting path.
        mock_urlopen.return_value = _resp({"id": 99})
        api.post_pull_request_thread(
            "tok", "example-org", "example-project", "example-repo",
            42, "this is the comment",
        )
        req = mock_urlopen.call_args.args[0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload == {
            "comments": [
                {"content": "this is the comment", "commentType": 1},
            ],
            "status": 1,
        }


class TestUpdatePullRequestThreadComment:
    """Test the PATCH-side sibling to post_pull_request_thread.

    Used by the update-in-place PR-check UX so subsequent runs edit
    the existing comment rather than posting a new one.
    """

    @pytest.fixture(autouse=True)
    def _requires_patch_helper(self):
        if not hasattr(api, "update_pull_request_thread_comment"):
            pytest.skip(
                "update_pull_request_thread_comment not in this vendored copy"
            )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_patch_uses_correct_url_and_method(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 1, "content": "new body"})
        api.update_pull_request_thread_comment(
            "tok", "example-org", "example-project", "example-repo",
            pull_request_id=42, thread_id=99, comment_id=1,
            comment_markdown="new body",
        )
        req = mock_urlopen.call_args.args[0]
        # URL shape: .../pullRequests/{n}/threads/{tid}/comments/{cid}?api-version=7.0
        assert (
            "dev.azure.com/example-org/example-project/_apis/git/"
            "repositories/example-repo/pullRequests/42/threads/99/comments/1"
            in req.full_url
        )
        assert "api-version=7.0" in req.full_url
        assert req.get_method() == "PATCH"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_patch_body_matches_ado_shape(self, mock_urlopen):
        # ADO's PATCH shape requires content + commentType + parentCommentId.
        # parentCommentId=0 marks a top-level thread comment (not a reply).
        mock_urlopen.return_value = _resp({"id": 1, "content": "x"})
        api.update_pull_request_thread_comment(
            "tok", "org", "proj", "repo",
            pull_request_id=1, thread_id=5, comment_id=1,
            comment_markdown="edited",
        )
        req = mock_urlopen.call_args.args[0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload == {
            "content": "edited",
            "commentType": 1,
            "parentCommentId": 0,
        }

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_patch_returns_body(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 1, "content": "updated"})
        result = api.update_pull_request_thread_comment(
            "tok", "org", "proj", "repo",
            pull_request_id=1, thread_id=5, comment_id=1,
            comment_markdown="updated",
        )
        assert result["content"] == "updated"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_patch_404_maps_to_not_found_error(self, mock_urlopen):
        # If the thread/comment was deleted between POST and PATCH,
        # the underlying _request maps 404 to AzureDevOpsNotFoundError.
        # Caller catches this and falls back to POST (chunk 03 logic).
        mock_urlopen.side_effect = _http_error(404)
        with pytest.raises(api.AzureDevOpsNotFoundError):
            api.update_pull_request_thread_comment(
                "tok", "org", "proj", "repo",
                pull_request_id=1, thread_id=999, comment_id=1,
                comment_markdown="x",
            )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_patch_401_maps_to_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401)
        with pytest.raises(api.AzureDevOpsAuthError):
            api.update_pull_request_thread_comment(
                "tok", "org", "proj", "repo",
                pull_request_id=1, thread_id=5, comment_id=1,
                comment_markdown="x",
            )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_patch_403_maps_to_scope_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(403)
        with pytest.raises(api.AzureDevOpsScopeError):
            api.update_pull_request_thread_comment(
                "tok", "org", "proj", "repo",
                pull_request_id=1, thread_id=5, comment_id=1,
                comment_markdown="x",
            )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_401_becomes_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401, b'{"message":"expired"}')
        with pytest.raises(api.AzureDevOpsAuthError) as ctx:
            api.list_repos("tok", "example-org", "example-project")
        assert ctx.value.status_code == 401

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_403_becomes_scope_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(403, b'{"message":"need vso.code_write"}')
        with pytest.raises(api.AzureDevOpsScopeError) as ctx:
            api.post_pull_request_thread(
                "tok", "example-org", "example-project", "example-repo", 1, "hi",
            )
        assert ctx.value.status_code == 403
        assert "vso.code_write" in (ctx.value.response or "")

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_404_becomes_not_found(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(404, b'{"message":"no such repo"}')
        with pytest.raises(api.AzureDevOpsNotFoundError):
            api.get_repo("tok", "example-org", "example-project", "missing-repo")

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_500_becomes_server_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(500, b"internal")
        with pytest.raises(api.AzureDevOpsServerError) as ctx:
            api.list_repos("tok", "example-org", "example-project")
        assert ctx.value.status_code == 500

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_503_also_server_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(503, b"unavailable")
        with pytest.raises(api.AzureDevOpsServerError):
            api.list_repos("tok", "example-org", "example-project")

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_unmapped_4xx_falls_through_to_base(self, mock_urlopen):
        # 418 -- not 401/403/404, not 5xx. Should be the base class so
        # callers still catch it generically, but isn't elevated to a
        # typed exception with semantics it doesn't have.
        mock_urlopen.side_effect = _http_error(418, b"teapot")
        with pytest.raises(api.AzureDevOpsAPIError) as ctx:
            api.list_repos("tok", "example-org", "example-project")
        # NOT one of the more specific subclasses
        assert not isinstance(ctx.value, api.AzureDevOpsAuthError)
        assert not isinstance(ctx.value, api.AzureDevOpsScopeError)
        assert not isinstance(ctx.value, api.AzureDevOpsNotFoundError)
        assert not isinstance(ctx.value, api.AzureDevOpsServerError)
        assert ctx.value.status_code == 418

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_url_error_becomes_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(api.AzureDevOpsAPIError):
            api.list_repos("tok", "example-org", "example-project")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagedGet:
    """``_paged_get`` is the single helper for both ADO pagination styles.
    Both branches must be exercised explicitly -- a future contributor who
    breaks the continuation-token path can't silently rely on the
    single-page path covering them."""

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_single_page_no_continuation_token(self, mock_urlopen):
        mock_urlopen.return_value = _resp(
            {"value": [{"id": "a"}, {"id": "b"}]},
            headers={},  # No X-MS-ContinuationToken
        )
        results = list(api._paged_get("https://dev.azure.com/x/_apis/projects", "tok"))
        assert [r["id"] for r in results] == ["a", "b"]
        # Exactly one HTTP call
        assert mock_urlopen.call_count == 1

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_continuation_token_drives_second_call(self, mock_urlopen):
        # ADO returns the cursor as a response header, then expects
        # ``continuationToken`` as a query param on the next call.
        mock_urlopen.side_effect = [
            _resp(
                {"value": [{"id": "p1"}, {"id": "p2"}]},
                headers={"X-MS-ContinuationToken": "cursor-xyz"},
            ),
            _resp(
                {"value": [{"id": "p3"}]},
                headers={},  # No more pages
            ),
        ]
        results = list(api._paged_get("https://dev.azure.com/x/_apis/projects", "tok"))
        assert [r["id"] for r in results] == ["p1", "p2", "p3"]
        assert mock_urlopen.call_count == 2
        # Second URL must carry the continuationToken from the first
        # response.
        second_url = mock_urlopen.call_args_list[1].args[0].full_url
        assert "continuationToken=cursor-xyz" in second_url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_continuation_token_case_insensitive(self, mock_urlopen):
        # ADO occasionally returns the header with lowercase casing
        # (proxy normalization). _paged_get must accept both.
        mock_urlopen.side_effect = [
            _resp(
                {"value": [{"id": "a"}]},
                headers={"x-ms-continuationtoken": "lower-cursor"},
            ),
            _resp({"value": [{"id": "b"}]}, headers={}),
        ]
        results = list(api._paged_get("https://dev.azure.com/x/_apis/projects", "tok"))
        assert [r["id"] for r in results] == ["a", "b"]
        assert "continuationToken=lower-cursor" in (
            mock_urlopen.call_args_list[1].args[0].full_url
        )

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_empty_value_array_yields_nothing(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        assert list(api._paged_get("https://dev.azure.com/x/_apis/projects", "tok")) == []

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_missing_value_key_yields_nothing(self, mock_urlopen):
        # Defensive: a malformed-but-2xx response shouldn't crash.
        mock_urlopen.return_value = _resp({})
        assert list(api._paged_get("https://dev.azure.com/x/_apis/projects", "tok")) == []

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_initial_call_carries_top_param(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"value": []})
        list(api._paged_get("https://dev.azure.com/x/_apis/projects", "tok", page_size=50))
        url = mock_urlopen.call_args.args[0].full_url
        assert "%24top=50" in url  # $top URL-encoded


# ---------------------------------------------------------------------------
# API version pinning
# ---------------------------------------------------------------------------


class TestApiVersionPinning:
    """ADO bumps REST versions every few months. We pin to 7.0; if any
    URL stops carrying that, we want a test failure -- not a silent
    behavior change in prod."""

    def test_api_version_constant(self):
        assert api.API_VERSION == "7.0"

    @patch("azdo_int_api.urllib.request.urlopen")
    @pytest.mark.parametrize("call", [
        lambda: api.list_accessible_orgs("tok", "mid"),
        lambda: api.list_projects("tok", "example-org"),
        lambda: api.list_repos("tok", "example-org", "example-project"),
        lambda: api.get_repo("tok", "example-org", "example-project", "example-repo"),
        lambda: api.get_latest_commit_sha("tok", "example-org", "example-project", "example-repo", "main"),
        lambda: api.get_tree_recursive("tok", "example-org", "example-project", "example-repo", "main"),
        lambda: api.list_open_pulls("tok", "example-org", "example-project", "example-repo"),
    ])
    def test_every_get_carries_api_version(self, mock_urlopen, call):
        mock_urlopen.return_value = _resp({"value": []})
        call()
        url = mock_urlopen.call_args.args[0].full_url
        assert "api-version=7.0" in url, f"API version missing from {url}"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_get_file_content_carries_api_version(self, mock_urlopen):
        mock_urlopen.return_value = _resp(raw_bytes=b"x")
        api.get_file_content(
            "tok", "example-org", "example-project", "example-repo", "/a", "main",
        )
        assert "api-version=7.0" in mock_urlopen.call_args.args[0].full_url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_post_pull_request_thread_carries_api_version(self, mock_urlopen):
        mock_urlopen.return_value = _resp({})
        api.post_pull_request_thread(
            "tok", "example-org", "example-project", "example-repo", 1, "x",
        )
        assert "api-version=7.0" in mock_urlopen.call_args.args[0].full_url


# ---------------------------------------------------------------------------
# Inline (line-anchored) PR thread -- the inline-review machinery
# ---------------------------------------------------------------------------


class TestPostPullRequestInlineThread:

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_sets_right_file_start_end_at_line(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 99, "comments": [{"id": 1}]})
        api.post_pull_request_inline_thread(
            "tok", "example-org", "example-project", "example-repo",
            42, "the finding", file_path="/src/foo.py", line=17,
        )
        req = mock_urlopen.call_args.args[0]
        payload = json.loads(req.data.decode("utf-8"))
        tc = payload["threadContext"]
        assert tc["rightFileStart"] == {"line": 17, "offset": 1}
        assert tc["rightFileEnd"] == {"line": 17, "offset": 1}
        assert payload["comments"] == [{"content": "the finding", "commentType": 1}]
        assert payload["status"] == 1

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_normalizes_file_path_to_leading_slash(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 99})
        api.post_pull_request_inline_thread(
            "tok", "org", "proj", "repo",
            42, "x", file_path="src/no_leading_slash.py", line=3,
        )
        req = mock_urlopen.call_args.args[0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["threadContext"]["filePath"] == "/src/no_leading_slash.py"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_preserves_already_absolute_file_path(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 99})
        api.post_pull_request_inline_thread(
            "tok", "org", "proj", "repo",
            42, "x", file_path="/already/abs.py", line=3,
        )
        req = mock_urlopen.call_args.args[0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["threadContext"]["filePath"] == "/already/abs.py"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_posts_to_threads_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 99})
        api.post_pull_request_inline_thread(
            "tok", "example-org", "example-project", "example-repo",
            42, "x", file_path="a.py", line=1,
        )
        req = mock_urlopen.call_args.args[0]
        assert "/pullRequests/42/threads" in req.full_url
        assert "api-version=7.0" in req.full_url
        assert req.get_method() == "POST"

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_returns_body(self, mock_urlopen):
        mock_urlopen.return_value = _resp({"id": 99, "comments": [{"id": 7}]})
        out = api.post_pull_request_inline_thread(
            "tok", "org", "proj", "repo", 42, "x", file_path="a.py", line=1,
        )
        assert out["id"] == 99
        assert out["comments"][0]["id"] == 7


class TestDeletePullRequestThreadComment:

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_issues_delete_to_comment_endpoint(self, mock_urlopen):
        # 204 No Content -- _request tolerates the empty body.
        mock_urlopen.return_value = _resp()  # empty body
        out = api.delete_pull_request_thread_comment(
            "tok", "example-org", "example-project", "example-repo",
            pull_request_id=42, thread_id=5, comment_id=9,
        )
        assert out is None
        req = mock_urlopen.call_args.args[0]
        assert req.get_method() == "DELETE"
        assert (
            "repositories/example-repo/pullRequests/42/threads/5/comments/9"
            in req.full_url
        )
        assert "api-version=7.0" in req.full_url

    @patch("azdo_int_api.urllib.request.urlopen")
    def test_404_maps_to_not_found(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(404)
        with pytest.raises(api.AzureDevOpsNotFoundError):
            api.delete_pull_request_thread_comment(
                "tok", "org", "proj", "repo",
                pull_request_id=1, thread_id=1, comment_id=1,
            )
