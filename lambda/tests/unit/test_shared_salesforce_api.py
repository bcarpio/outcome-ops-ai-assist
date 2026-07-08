"""Unit tests for shared/salesforce_api.py."""

import io
import json
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest


_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

import shared.salesforce_api as api  # noqa: E402
from shared.salesforce_api import SalesforceRateLimitedError  # noqa: E402


class TestSoqlLiteralEscape:
    def test_simple(self):
        assert api._escape_soql_literal("hello") == "hello"

    def test_single_quote(self):
        assert api._escape_soql_literal("O'Reilly") == "O\\'Reilly"

    def test_backslash(self):
        assert api._escape_soql_literal(r"path\file") == r"path\\file"


class TestChunk:
    def test_splits_by_size(self):
        chunks = list(api._chunk(["a", "b", "c", "d", "e"], 2))
        assert chunks == [["a", "b"], ["c", "d"], ["e"]]

    def test_empty(self):
        assert list(api._chunk([], 3)) == []


class TestRequestRetries:
    """Retry loop shape without hitting the network."""

    def _mock_response(self, code, body):
        m = MagicMock()
        m.getcode.return_value = code
        m.read.return_value = body.encode() if isinstance(body, str) else body
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def _make_http_error(self, code, body):
        return HTTPError(
            "https://x.example",
            code,
            "err",
            {},
            io.BytesIO(body.encode() if isinstance(body, str) else body),
        )

    @patch.object(api.urllib.request, "urlopen")
    def test_success_returns_status_and_body(self, mock_open):
        mock_open.return_value = self._mock_response(200, '{"ok":1}')
        status, body = api._request(
            "GET", "https://x.example", "/svc", "tok",
        )
        assert status == 200
        assert body == b'{"ok":1}'

    @patch.object(api.time, "sleep", lambda *_: None)
    @patch.object(api.urllib.request, "urlopen")
    def test_429_retries_then_raises_rate_limited(self, mock_open):
        # BytesIO is single-read; give a fresh error per attempt.
        mock_open.side_effect = [
            self._make_http_error(429, "throttled") for _ in range(4)
        ]
        with pytest.raises(SalesforceRateLimitedError):
            api._request("GET", "https://x.example", "/svc", "tok")

    @patch.object(api.urllib.request, "urlopen")
    def test_403_perms_raises_immediately(self, mock_open):
        mock_open.side_effect = self._make_http_error(403, "not enough")
        with pytest.raises(ValueError, match="Access denied"):
            api._request("GET", "https://x.example", "/svc", "tok")

    @patch.object(api.time, "sleep", lambda *_: None)
    @patch.object(api.urllib.request, "urlopen")
    def test_403_request_limit_exceeded_treated_as_rate_limit(self, mock_open):
        # BytesIO is single-read; each retry needs a fresh error so the
        # REQUEST_LIMIT_EXCEEDED marker stays visible on every attempt.
        mock_open.side_effect = [
            self._make_http_error(403, "[REQUEST_LIMIT_EXCEEDED] daily quota reached")
            for _ in range(4)
        ]
        with pytest.raises(SalesforceRateLimitedError):
            api._request("GET", "https://x.example", "/svc", "tok")

    @patch.object(api.urllib.request, "urlopen")
    def test_401_raises_access_token_expired(self, mock_open):
        mock_open.side_effect = self._make_http_error(401, "invalid")
        with pytest.raises(ValueError, match="expired or invalid"):
            api._request("GET", "https://x.example", "/svc", "tok")

    @patch.object(api.urllib.request, "urlopen")
    def test_404_returns_status_without_raising(self, mock_open):
        mock_open.side_effect = self._make_http_error(404, "not found")
        status, body = api._request("GET", "https://x.example", "/svc", "tok")
        assert status == 404


class TestSoqlIteration:
    @patch.object(api, "_soql")
    def test_single_page_yields_all(self, mock_soql):
        mock_soql.return_value = {
            "done": True,
            "records": [{"Id": "a"}, {"Id": "b"}],
        }
        results = list(api._iterate_query("u", "t", "SELECT Id FROM Account"))
        assert [r["Id"] for r in results] == ["a", "b"]

    @patch.object(api, "_soql_more")
    @patch.object(api, "_soql")
    def test_paginates_via_next_records_url(self, mock_soql, mock_soql_more):
        mock_soql.return_value = {
            "done": False,
            "nextRecordsUrl": "/services/data/v60.0/query/next",
            "records": [{"Id": "a"}],
        }
        mock_soql_more.return_value = {
            "done": True,
            "records": [{"Id": "b"}, {"Id": "c"}],
        }
        results = list(api._iterate_query("u", "t", "q"))
        assert [r["Id"] for r in results] == ["a", "b", "c"]


class TestChangedActivityParentIds:
    @patch.object(api, "_iterate_query")
    def test_returns_union_of_what_and_who_ids(self, mock_iter):
        mock_iter.return_value = iter([
            {"WhatId": "001A", "WhoId": None, "LastModifiedDate": "2026-06-01T10:00:00Z"},
            {"WhatId": None, "WhoId": "003C", "LastModifiedDate": "2026-06-01T11:00:00Z"},
        ])
        ids, cursor = api.changed_activity_parent_ids(
            "u", "t", "Task", "2026-06-01T09:00:00Z",
        )
        assert ids == {"001A", "003C"}
        assert cursor == "2026-06-01T11:00:00Z"


class TestListRecentActivities:
    @patch.object(api, "_soql")
    def test_combines_tasks_and_events_sorted_newest_first(self, mock_soql):
        mock_soql.side_effect = [
            {"records": [{"Subject": "T1", "LastModifiedDate": "2026-06-01T10:00:00Z"}]},
            {"records": [{"Subject": "E1", "LastModifiedDate": "2026-06-02T10:00:00Z"}]},
        ]
        acts = api.list_recent_activities("u", "t", "Account", "001XXX")
        # Newest first: Event (2026-06-02) before Task (2026-06-01)
        assert acts[0]["Subject"] == "E1"
        assert acts[0]["_activity_kind"] == "Event"
        assert acts[1]["_activity_kind"] == "Task"

    @patch.object(api, "_soql")
    def test_swallows_query_error_on_one_kind(self, mock_soql):
        mock_soql.side_effect = [
            ValueError("perms error on Task"),
            {"records": [{"Subject": "E1", "LastModifiedDate": "2026-06-02T10:00:00Z"}]},
        ]
        acts = api.list_recent_activities("u", "t", "Contact", "003C")
        assert len(acts) == 1
        assert acts[0]["_activity_kind"] == "Event"
