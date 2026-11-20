# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared/oauth_state.py against a moto DynamoDB table."""

from __future__ import annotations

import os
import sys

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared import oauth_state  # noqa: E402

pytestmark = pytest.mark.real_oauth_state

TABLE = "test-workspaces"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("WORKSPACES_TABLE", TABLE)
    with mock_aws():
        oauth_state._reset_clients_for_tests()
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t
        oauth_state._reset_clients_for_tests()


class TestIssueAndConsume:
    def test_round_trip_returns_bound_workspace_user_and_extra(self, table):
        token = oauth_state.issue_state("gitlab", "ws_1", "admin@x.com", {"instance_id": "gl2"})
        assert oauth_state.consume_state("gitlab", token) == {
            "workspace_id": "ws_1", "user_email": "admin@x.com", "instance_id": "gl2",
        }

    def test_token_is_opaque_and_unique(self, table):
        a = oauth_state.issue_state("jira", "ws_1", "admin@x.com")
        b = oauth_state.issue_state("jira", "ws_1", "admin@x.com")
        assert a != b
        assert "ws_1" not in a and "admin" not in a
        assert len(a) >= 40

    def test_token_is_single_use(self, table):
        token = oauth_state.issue_state("jira", "ws_1", "admin@x.com")
        assert oauth_state.consume_state("jira", token)
        assert oauth_state.consume_state("jira", token) is None

    def test_forged_json_state_is_rejected(self, table):
        forged = '{"workspace_id": "ws_victim", "user_email": "someone@x.com"}'
        assert oauth_state.consume_state("confluence", forged) is None

    @pytest.mark.parametrize("token", [None, "", 123])
    def test_missing_or_non_string_token_is_rejected(self, table, token):
        assert oauth_state.consume_state("github", token) is None

    def test_token_for_another_provider_is_rejected_and_burned(self, table):
        token = oauth_state.issue_state("github", "ws_1", "admin@x.com")
        assert oauth_state.consume_state("gitlab", token) is None
        assert oauth_state.consume_state("github", token) is None

    def test_expired_token_is_rejected(self, table, monkeypatch, caplog):
        token = oauth_state.issue_state("teams", "ws_1", "admin@x.com")
        later = oauth_state.time.time() + oauth_state.STATE_TTL_SECONDS + 1
        monkeypatch.setattr(oauth_state.time, "time", lambda: later)
        with caplog.at_level("WARNING"):
            assert oauth_state.consume_state("teams", token) is None
        assert "expired teams state for workspace ws_1" in caplog.text

    def test_row_is_deleted_after_consume(self, table):
        token = oauth_state.issue_state("outlook", "ws_1", "admin@x.com")
        key = {"PK": f"OAUTH_STATE#{token}", "SK": f"OAUTH_STATE#{token}"}
        assert "Item" in table.get_item(Key=key)
        oauth_state.consume_state("outlook", token)
        assert "Item" not in table.get_item(Key=key)


class TestErrors:
    @pytest.mark.parametrize("args", [
        ("", "ws_1", "a@x.com"), ("github", "", "a@x.com"), ("github", "ws_1", ""),
    ])
    def test_issue_requires_provider_workspace_and_user(self, table, args):
        with pytest.raises(ValueError):
            oauth_state.issue_state(*args)

    def test_missing_table_env_raises(self, table, monkeypatch):
        monkeypatch.delenv("WORKSPACES_TABLE")
        with pytest.raises(RuntimeError, match="WORKSPACES_TABLE"):
            oauth_state.issue_state("github", "ws_1", "a@x.com")

    def test_unexpected_dynamodb_error_propagates(self, table, monkeypatch):
        class Boom:
            def delete_item(self, **_kw):
                raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "DeleteItem")
        monkeypatch.setattr(oauth_state, "_table", lambda _name: Boom())
        with pytest.raises(ClientError):
            oauth_state.consume_state("github", "tok")
