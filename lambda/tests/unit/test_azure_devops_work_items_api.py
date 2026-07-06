"""Unit tests for the Azure DevOps Work Items REST client.

Tests focus on:
1. **URL construction** -- dev.azure.com host, correct path,
   api-version=7.0 for stable, 7.0-preview.3 for comments.
2. **Headers** -- Authorization, User-Agent, Content-Type on POSTs.
3. **Error mapping** -- 401/403/404/5xx map to typed exceptions.
4. **WIQL builder** -- correct SQL shape, string escaping, open-only
   filter, watermark filter, project filter.
5. **Batching** -- get_work_items validates the 200-ID cap.
6. **Constants pinned** -- STANDARD_WORK_ITEM_FIELDS + API_VERSION
   + COMMENTS_API_VERSION.
"""

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
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


api = _load_module(
    "azdo_boards_work_items_api",
    "azure_devops_work_items_api.py",
    "../../azure-devops-boards-integration",
)

# The module under test is vendored per-Lambda (each function dir bundles
# its own copy; there is no shared layer for it). Re-run this whole suite
# against every copy so none can drift or sit untested: the autouse
# fixture below rebinds the module-global (and the sys.modules entry that
# @patch targets) once per vendored copy.
_VENDORED_COPY_DIRS = [
    "../../azure-devops-boards-integration",
    "../../azure-devops-boards-sync",
]


@pytest.fixture(
    scope="module",
    autouse=True,
    params=_VENDORED_COPY_DIRS,
    ids=[d.rsplit("/", 1)[-1] for d in _VENDORED_COPY_DIRS],
)
def _vendored_copy(request):
    global api
    api = _load_module(
        "azdo_boards_work_items_api",
        "azure_devops_work_items_api.py",
        request.param,
    )


# ---------------------------------------------------------------------------
# urlopen response builder
# ---------------------------------------------------------------------------


def _make_response(body_dict, headers=None):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    resp.headers = headers or {}
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code, msg="Error", body=""):
    return urllib.error.HTTPError(
        url="https://example",
        code=code, msg=msg, hdrs={},
        fp=MagicMock(read=MagicMock(return_value=body.encode("utf-8"))),
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:

    def test_api_version_is_7_0(self):
        assert api.API_VERSION == "7.0"

    def test_comments_uses_preview_3(self):
        # Microsoft's own labeling on the comments endpoint. Locked
        # because the non-preview /7.0 version returns metadata only.
        assert api.COMMENTS_API_VERSION == "7.0-preview.3"

    def test_max_work_items_per_batch_is_200(self):
        # ADO documented cap; the sync worker relies on this constant
        # to chunk large ID lists.
        assert api.MAX_WORK_ITEMS_PER_BATCH == 200

    def test_standard_work_item_fields_pinned(self):
        # Regression guard against silent drift of the v1 field list.
        # Custom-field expansion is a v2 follow-up per open question 3.
        assert "System.Id" in api.STANDARD_WORK_ITEM_FIELDS
        assert "System.Title" in api.STANDARD_WORK_ITEM_FIELDS
        assert "System.State" in api.STANDARD_WORK_ITEM_FIELDS
        assert "System.Description" in api.STANDARD_WORK_ITEM_FIELDS
        assert "Microsoft.VSTS.TCM.ReproSteps" in api.STANDARD_WORK_ITEM_FIELDS
        assert "Microsoft.VSTS.Common.AcceptanceCriteria" in api.STANDARD_WORK_ITEM_FIELDS
        # 16 fields in v1; expansion goes to v2.
        assert len(api.STANDARD_WORK_ITEM_FIELDS) == 16


# ---------------------------------------------------------------------------
# HTTP entry point: headers + error mapping
# ---------------------------------------------------------------------------


class TestRequestHeaders:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_sets_bearer_authorization_and_user_agent(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"value": []})
        api._request("GET", "https://dev.azure.com/x", "token-123")
        req = mock_urlopen.call_args.args[0]
        assert req.headers["Authorization"] == "Bearer token-123"
        assert "OutcomeOps-AIAssist" in req.headers["User-agent"]

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_post_body_serialized_as_json_with_content_type(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"workItems": []})
        api._request(
            "POST", "https://dev.azure.com/x", "tok",
            body={"query": "SELECT [System.Id] FROM workitems"},
        )
        req = mock_urlopen.call_args.args[0]
        assert req.headers["Content-type"] == "application/json"
        # data should be JSON-encoded
        assert json.loads(req.data.decode("utf-8")) == {
            "query": "SELECT [System.Id] FROM workitems"
        }


class TestErrorMapping:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_401_maps_to_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401, "Unauthorized", "token expired")
        with pytest.raises(api.AzureDevOpsBoardsAuthError) as exc:
            api._request("GET", "https://dev.azure.com/x", "tok")
        assert exc.value.status_code == 401
        assert "token expired" in (exc.value.response or "")

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_403_maps_to_scope_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(403, "Forbidden", "missing vso.work")
        with pytest.raises(api.AzureDevOpsBoardsScopeError):
            api._request("GET", "https://dev.azure.com/x", "tok")

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_404_maps_to_not_found_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(404, "Not Found", "no project")
        with pytest.raises(api.AzureDevOpsBoardsNotFoundError):
            api._request("GET", "https://dev.azure.com/x", "tok")

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_500_maps_to_server_error(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(503, "Service Unavailable", "")
        with pytest.raises(api.AzureDevOpsBoardsServerError) as exc:
            api._request("GET", "https://dev.azure.com/x", "tok")
        assert exc.value.status_code == 503

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_network_error_maps_to_generic_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(api.AzureDevOpsBoardsAPIError):
            api._request("GET", "https://dev.azure.com/x", "tok")

    def test_all_specific_errors_subclass_generic_api_error(self):
        # A caller catching AzureDevOpsBoardsAPIError catches every
        # ADO-typed failure mode.
        assert issubclass(api.AzureDevOpsBoardsAuthError, api.AzureDevOpsBoardsAPIError)
        assert issubclass(api.AzureDevOpsBoardsScopeError, api.AzureDevOpsBoardsAPIError)
        assert issubclass(api.AzureDevOpsBoardsNotFoundError, api.AzureDevOpsBoardsAPIError)
        assert issubclass(api.AzureDevOpsBoardsServerError, api.AzureDevOpsBoardsAPIError)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestListProjects:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_hits_projects_endpoint_with_api_version(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({
            "value": [{"id": "p1", "name": "Project 1"}],
        })
        result = api.list_projects("tok", "example-org")
        assert result == [{"id": "p1", "name": "Project 1"}]
        url = mock_urlopen.call_args.args[0].full_url
        assert url.startswith("https://dev.azure.com/example-org/_apis/projects")
        assert "api-version=7.0" in url


class TestListTeams:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_hits_project_teams_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({
            "value": [{"id": "t1", "name": "Alpha Team"}],
        })
        result = api.list_teams("tok", "example-org", "some-project")
        assert result == [{"id": "t1", "name": "Alpha Team"}]
        url = mock_urlopen.call_args.args[0].full_url
        assert (
            url.startswith(
                "https://dev.azure.com/example-org/_apis/projects/some-project/teams"
            )
        )
        assert "api-version=7.0" in url


# ---------------------------------------------------------------------------
# WIQL builder
# ---------------------------------------------------------------------------


class TestBuildIncrementalWIQL:

    def test_open_only_default_excludes_closed_states(self):
        query = api.build_incremental_wiql(project="example-project")
        assert "[System.TeamProject] = 'example-project'" in query
        assert "[System.State] NOT IN (" in query
        assert "'Closed'" in query
        assert "'Done'" in query
        assert "'Removed'" in query
        assert "'Resolved'" in query
        # No watermark clause on first sync.
        assert "System.ChangedDate" in query  # in the ORDER BY
        assert "[System.ChangedDate] >" not in query
        assert "ORDER BY [System.ChangedDate] DESC, [System.Id] DESC" in query

    def test_watermark_stripped_to_date_only(self):
        # WIQL [System.ChangedDate] is date-precision; passing a time
        # component triggers a TF-style parser error. Regression guard:
        # only the YYYY-MM-DD substring lands in the WHERE clause, and
        # the operator is >= so items later in the watermark day still
        # come along.
        query = api.build_incremental_wiql(
            project="p1", changed_since="2026-07-04T12:00:00Z",
        )
        assert "[System.ChangedDate] >= '2026-07-04'" in query
        # No time component or Z in the emitted WHERE clause.
        assert "T12:00:00" not in query
        assert "> '2026-07-04'" not in query

    def test_open_only_false_drops_state_filter(self):
        query = api.build_incremental_wiql(
            project="p1", open_only=False,
        )
        assert "[System.State] NOT IN" not in query

    def test_project_name_with_single_quote_is_escaped(self):
        # WIQL uses single-quote-doubling for literal quotes (SQL-like).
        # Regression guard for injection or query-shape breakage.
        query = api.build_incremental_wiql(project="John's Project")
        assert "'John''s Project'" in query
        # Must NOT contain the raw unescaped quote inside the literal.
        assert "'John's Project'" not in query


class TestURLEncoding:
    """Regression: ADO org / project names can contain spaces. urllib
    refuses to open a URL with control characters, so every path
    segment gets quoted before it lands in the URL."""

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_run_wiql_encodes_project_with_spaces(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"workItems": []})
        api.run_wiql("tok", "example-org", "Agile Project for OutcomeOps", "SELECT ...")
        url = mock_urlopen.call_args.args[0].full_url
        # Spaces become %20 (or +); no raw spaces in the URL.
        assert " " not in url
        assert "Agile%20Project%20for%20OutcomeOps" in url

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_get_work_item_comments_encodes_project(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"comments": []})
        api.get_work_item_comments(
            "tok", "example-org", "Agile Project for OutcomeOps", 42,
        )
        url = mock_urlopen.call_args.args[0].full_url
        assert " " not in url
        assert "Agile%20Project%20for%20OutcomeOps" in url


class TestErrorMessageExtraction:
    """ADO errors carry a JSON body with a ``message`` field. We surface
    that so the caller sees the actual ADO complaint, not just an HTTP
    code."""

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_400_error_includes_ado_message_in_exception(self, mock_urlopen):
        body = json.dumps({
            "message": "TF400898: An Internal Error Occurred.",
            "typeName": "Microsoft.TeamFoundation...",
        })
        mock_urlopen.side_effect = _http_error(400, "Bad Request", body)
        with pytest.raises(api.AzureDevOpsBoardsAPIError) as exc:
            api._request("POST", "https://dev.azure.com/x/y/_apis/wit/wiql", "tok")
        assert "TF400898" in str(exc.value)

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_404_error_includes_ado_message(self, mock_urlopen):
        body = json.dumps({"message": "Project 'RetrieveIT' was not found."})
        mock_urlopen.side_effect = _http_error(404, "Not Found", body)
        with pytest.raises(api.AzureDevOpsBoardsNotFoundError) as exc:
            api._request("GET", "https://dev.azure.com/x/y/_apis/wit/wiql", "tok")
        assert "not found" in str(exc.value).lower()

    def test_extract_returns_empty_on_non_json_body(self):
        assert api._extract_ado_error_message("") == ""
        assert api._extract_ado_error_message("<html>oops</html>") == "<html>oops</html>"

    def test_extract_pulls_inner_exception_message(self):
        body = json.dumps({
            "message": "Outer",
            "innerException": {"message": "Inner detail"},
        })
        assert "Inner detail" in api._extract_ado_error_message(body)


class TestRunWIQL:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_posts_query_body_and_returns_id_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({
            "workItems": [{"id": 42}, {"id": 43}, {"id": 99}],
        })
        result = api.run_wiql(
            "tok", "example-org", "p1",
            "SELECT [System.Id] FROM workitems",
        )
        assert result == [42, 43, 99]

        req = mock_urlopen.call_args.args[0]
        url = req.full_url
        assert url.startswith(
            "https://dev.azure.com/example-org/p1/_apis/wit/wiql"
        )
        assert "api-version=7.0" in url
        assert req.get_method() == "POST"
        assert json.loads(req.data.decode("utf-8")) == {
            "query": "SELECT [System.Id] FROM workitems"
        }

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_empty_result_returns_empty_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"workItems": []})
        assert api.run_wiql("tok", "org", "p", "SELECT ...") == []

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_missing_workitems_key_returns_empty_list(self, mock_urlopen):
        # ADO occasionally returns queryResult=None-style bodies.
        # Defensive: don't crash, return empty.
        mock_urlopen.return_value = _make_response({"foo": "bar"})
        assert api.run_wiql("tok", "org", "p", "SELECT ...") == []

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_malformed_entries_are_skipped(self, mock_urlopen):
        # A stray non-dict entry or missing "id" shouldn't kill the whole
        # result set.
        mock_urlopen.return_value = _make_response({
            "workItems": [{"id": 1}, "malformed", {"noId": True}, {"id": 2}],
        })
        assert api.run_wiql("tok", "org", "p", "SELECT ...") == [1, 2]


# ---------------------------------------------------------------------------
# get_work_items
# ---------------------------------------------------------------------------


class TestGetWorkItems:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_batch_fetch_uses_standard_fields_by_default(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({
            "value": [{"id": 1}, {"id": 2}],
        })
        result = api.get_work_items("tok", "example-org", [1, 2])
        assert result == [{"id": 1}, {"id": 2}]

        url = mock_urlopen.call_args.args[0].full_url
        assert url.startswith(
            "https://dev.azure.com/example-org/_apis/wit/workitems"
        )
        assert "ids=1%2C2" in url or "ids=1,2" in url
        # Standard fields present in the query string
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        assert "System.Id" in params["fields"]
        assert "System.Title" in params["fields"]
        assert "System.Description" in params["fields"]

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_custom_field_list_overrides_default(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"value": []})
        api.get_work_items(
            "tok", "example-org", [1],
            fields=["System.Id", "System.State"],
        )
        url = mock_urlopen.call_args.args[0].full_url
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        assert params["fields"] == "System.Id,System.State"

    def test_empty_id_list_returns_empty_without_http_call(self):
        # No HTTP mock -- if this makes an HTTP call, it blows up.
        assert api.get_work_items("tok", "org", []) == []

    def test_over_200_ids_raises_value_error(self):
        # Regression guard: ADO rejects >200 IDs per batch. The caller
        # (sync worker) is required to chunk; the API client validates.
        with pytest.raises(ValueError, match="200"):
            api.get_work_items("tok", "org", list(range(201)))


# ---------------------------------------------------------------------------
# get_work_item_comments
# ---------------------------------------------------------------------------


class TestGetWorkItemComments:

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_uses_preview_3_api_version(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({
            "comments": [
                {"id": 1, "text": "First comment"},
                {"id": 2, "text": "Second"},
            ],
        })
        result = api.get_work_item_comments("tok", "example-org", "p1", 42)
        assert len(result) == 2
        url = mock_urlopen.call_args.args[0].full_url
        assert (
            url.startswith(
                "https://dev.azure.com/example-org/p1/_apis/wit/workitems/42/comments"
            )
        )
        # Locked at 7.0-preview.3 -- Microsoft's own labeling.
        assert "api-version=7.0-preview.3" in url

    @patch("azdo_boards_work_items_api.urllib.request.urlopen")
    def test_empty_comments_returns_empty_list(self, mock_urlopen):
        mock_urlopen.return_value = _make_response({"comments": []})
        assert api.get_work_item_comments("tok", "org", "p", 1) == []
