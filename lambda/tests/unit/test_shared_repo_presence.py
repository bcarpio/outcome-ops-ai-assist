# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Tests for shared/repo_presence.py against a moto DynamoDB table."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared import repo_presence  # noqa: E402
from shared.repo_presence import RepoPresenceError  # noqa: E402

TABLE = "test-workspaces"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("WORKSPACES_TABLE", TABLE)
    with mock_aws():
        repo_presence._reset_clients_for_tests()
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
        repo_presence._reset_clients_for_tests()


@pytest.fixture
def stub_table(monkeypatch):
    """Replace the DynamoDB resource with a stub whose Table is a MagicMock."""
    monkeypatch.setenv("WORKSPACES_TABLE", TABLE)
    fake_table = MagicMock()
    fake_resource = MagicMock()
    fake_resource.Table.return_value = fake_table
    with patch.object(repo_presence, "_dynamodb_resource", fake_resource):
        yield fake_resource, fake_table


class TestRepoRowKey:
    def test_github_key(self):
        assert repo_presence.repo_row_key("ws_1", "github", "acme/api") == {
            "PK": "WS#ws_1", "SK": "GITHUB_REPO#acme/api",
        }

    def test_gitlab_nested_slug(self):
        key = repo_presence.repo_row_key("ws_1", "gitlab", "grp/sub/deeper/repo")
        assert key == {"PK": "WS#ws_1", "SK": "GITLAB_REPO#grp/sub/deeper/repo"}

    def test_azure_devops_three_part_slug(self):
        key = repo_presence.repo_row_key("ws_1", "azure_devops", "org/Project/Repo")
        assert key == {"PK": "WS#ws_1", "SK": "AZDO_REPO#org/Project/Repo"}

    def test_strips_one_leading_and_trailing_slash(self):
        key = repo_presence.repo_row_key("ws_1", "github", "/acme/api/")
        assert key["SK"] == "GITHUB_REPO#acme/api"

    def test_preserves_case(self):
        key = repo_presence.repo_row_key("ws_1", "github", "Acme/API")
        assert key["SK"] == "GITHUB_REPO#Acme/API"

    def test_github_prefix_is_not_bare_repo(self):
        assert repo_presence.SOURCE_SK_PREFIX["github"] == "GITHUB_REPO#"

    @pytest.mark.parametrize("source", ["", "bitbucket", "GitHub", None])
    def test_unknown_source_raises(self, source):
        with pytest.raises(ValueError):
            repo_presence.repo_row_key("ws_1", source, "acme/api")

    @pytest.mark.parametrize("workspace_id", ["", None])
    def test_empty_workspace_raises(self, workspace_id):
        with pytest.raises(ValueError):
            repo_presence.repo_row_key(workspace_id, "github", "acme/api")

    @pytest.mark.parametrize("slug", ["", None, "/", "//"])
    def test_empty_slug_raises(self, slug):
        with pytest.raises(ValueError):
            repo_presence.repo_row_key("ws_1", "github", slug)


class TestIsRepoConnected:
    @pytest.mark.parametrize("source,slug", [
        ("github", "acme/api"),
        ("gitlab", "grp/sub/repo"),
        ("azure_devops", "org/proj/repo"),
    ])
    def test_connected_row_returns_true(self, table, source, slug):
        table.put_item(Item=repo_presence.repo_row_key("ws_1", source, slug))
        assert repo_presence.is_repo_connected("ws_1", source, slug) is True

    @pytest.mark.parametrize("source,slug", [
        ("github", "acme/api"),
        ("gitlab", "grp/sub/repo"),
        ("azure_devops", "org/proj/repo"),
    ])
    def test_missing_row_returns_false(self, table, source, slug):
        assert repo_presence.is_repo_connected("ws_1", source, slug) is False

    def test_row_in_other_workspace_is_not_connected(self, table):
        table.put_item(Item=repo_presence.repo_row_key("ws_other", "github", "acme/api"))
        assert repo_presence.is_repo_connected("ws_1", "github", "acme/api") is False

    def test_legacy_repo_prefix_row_is_not_connected(self, table):
        table.put_item(Item={"PK": "WS#ws_1", "SK": "REPO#acme/api"})
        assert repo_presence.is_repo_connected("ws_1", "github", "acme/api") is False

    def test_explicit_table_name_overrides_env(self, table, monkeypatch):
        monkeypatch.setenv("WORKSPACES_TABLE", "does-not-exist")
        table.put_item(Item=repo_presence.repo_row_key("ws_1", "github", "acme/api"))
        assert repo_presence.is_repo_connected(
            "ws_1", "github", "acme/api", table_name=TABLE,
        ) is True

    def test_missing_table_env_raises_presence_error(self, monkeypatch):
        monkeypatch.delenv("WORKSPACES_TABLE", raising=False)
        with pytest.raises(RepoPresenceError):
            repo_presence.is_repo_connected("ws_1", "github", "acme/api")

    def test_nonexistent_table_raises_presence_error(self, table):
        with pytest.raises(RepoPresenceError):
            repo_presence.is_repo_connected(
                "ws_1", "github", "acme/api", table_name="no-such-table",
            )

    def test_bad_args_raise_value_error_not_presence_error(self, table):
        with pytest.raises(ValueError):
            repo_presence.is_repo_connected("ws_1", "bitbucket", "acme/api")

    def test_passes_consistent_read_and_pk_projection(self, stub_table):
        fake_resource, fake_table = stub_table
        fake_table.get_item.return_value = {"Item": {"PK": "WS#ws_1"}}
        assert repo_presence.is_repo_connected("ws_1", "gitlab", "g/s/r") is True
        fake_resource.Table.assert_called_once_with(TABLE)
        fake_table.get_item.assert_called_once_with(
            Key={"PK": "WS#ws_1", "SK": "GITLAB_REPO#g/s/r"},
            ConsistentRead=True,
            ProjectionExpression="PK",
        )

    def test_client_error_raises_presence_error(self, stub_table):
        _, fake_table = stub_table
        fake_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
            "GetItem",
        )
        with pytest.raises(RepoPresenceError) as exc_info:
            repo_presence.is_repo_connected("ws_1", "github", "acme/api")
        assert isinstance(exc_info.value.__cause__, ClientError)

    def test_botocore_error_raises_presence_error(self, stub_table):
        _, fake_table = stub_table
        fake_table.get_item.side_effect = EndpointConnectionError(endpoint_url="https://ddb")
        with pytest.raises(RepoPresenceError):
            repo_presence.is_repo_connected("ws_1", "github", "acme/api")

    def test_resource_is_created_lazily_and_cached(self, table):
        repo_presence._reset_clients_for_tests()
        assert repo_presence._dynamodb_resource is None
        repo_presence.is_repo_connected("ws_1", "github", "acme/api")
        first = repo_presence._dynamodb_resource
        assert first is not None
        repo_presence.is_repo_connected("ws_1", "github", "acme/api")
        assert repo_presence._dynamodb_resource is first
