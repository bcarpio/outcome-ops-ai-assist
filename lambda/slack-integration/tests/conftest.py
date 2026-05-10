"""Pytest configuration and fixtures for Slack integration Lambda tests."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Set required env vars. SLACK_ENABLED=true so most tests exercise routes."""
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("APP_NAME", "app.retrieveit.ai")
    monkeypatch.setenv("TENANTS_TABLE", "test-tenants")
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-documents-bucket")
    monkeypatch.setenv("SLACK_SYNC_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/slack-sync")
    monkeypatch.setenv("FILE_INGESTION_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/file-ingestion")
    monkeypatch.setenv("DOCUMENT_DELETE_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/document-delete")
    monkeypatch.setenv("SLACK_KMS_KEY_ARN", "arn:aws:kms:us-west-2:123:key/test-key")
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SLACK_ENABLED", "true")


@pytest.fixture(autouse=True)
def reset_global_caches():
    import handler
    import token_manager

    handler._dynamodb = None
    handler._ssm_client = None
    handler._sqs_client = None
    handler._jwt_secret = None

    token_manager._kms_client = None
    token_manager._ssm_client = None
    token_manager._dynamodb = None
    token_manager._slack_client_secret = None

    yield


@pytest.fixture
def mock_dynamodb():
    with patch("handler._get_dynamodb") as mock:
        table = MagicMock()
        mock.return_value.Table.return_value = table
        yield table


@pytest.fixture
def mock_jwt_secret():
    with patch("handler._get_jwt_secret") as mock:
        mock.return_value = "test-jwt-secret-key-for-testing-12345"
        yield mock


@pytest.fixture
def mock_sqs_client():
    with patch("handler._get_sqs_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


@pytest.fixture
def mock_token_manager():
    with patch("handler.save_tokens") as mock_save, \
         patch("handler.delete_tokens") as mock_delete, \
         patch("handler.refresh_access_token") as mock_refresh, \
         patch("handler._get_client_id") as mock_client_id, \
         patch("handler._get_client_secret") as mock_client_secret:
        mock_refresh.return_value = "xoxb-test-bot-token"
        mock_client_id.return_value = "test-client-id"
        mock_client_secret.return_value = "test-client-secret"
        yield {
            "save_tokens": mock_save,
            "delete_tokens": mock_delete,
            "refresh_access_token": mock_refresh,
            "_get_client_id": mock_client_id,
            "_get_client_secret": mock_client_secret,
        }


@pytest.fixture
def mock_slack_api():
    """Default mocks for slack_api functions imported into handler."""
    with patch("handler.oauth_v2_access") as mock_oauth, \
         patch("handler.auth_test") as mock_auth, \
         patch("handler.iterate_users") as mock_users, \
         patch("handler.users_info") as mock_uinfo, \
         patch("handler.iterate_member_channels") as mock_channels, \
         patch("handler.iterate_channel_messages") as mock_history, \
         patch("handler.iterate_thread_replies") as mock_replies:

        mock_oauth.return_value = {
            "ok": True,
            "access_token": "xoxb-test-bot-token",
            "team": {"id": "T01TEST", "name": "Test Workspace"},
            "bot_user_id": "U01BOT",
            "app_id": "A01APP",
        }
        mock_auth.return_value = {"ok": True, "user_id": "U01BOT", "team_id": "T01TEST"}
        mock_users.return_value = iter([])
        mock_uinfo.return_value = {"ok": True, "user": {"id": "U01TEST", "name": "tester",
                                                          "profile": {"display_name": "Tester", "real_name": "Tester"}}}
        mock_channels.return_value = iter([])
        mock_history.return_value = iter([])
        mock_replies.return_value = iter([])

        yield {
            "oauth_v2_access": mock_oauth,
            "auth_test": mock_auth,
            "iterate_users": mock_users,
            "users_info": mock_uinfo,
            "iterate_member_channels": mock_channels,
            "iterate_channel_messages": mock_history,
            "iterate_thread_replies": mock_replies,
        }


@pytest.fixture
def jwt_secret():
    return "test-jwt-secret-key-for-testing-12345"


@pytest.fixture
def admin_token(jwt_secret):
    return jwt.encode(
        {
            "sub": "user_abc123",
            "email": "admin@company.com",
            "name": "Admin User",
            "org_id": "org_xyz789",
            "org_role": "org_admin",
            "exp": int(time.time()) + 86400,
        },
        jwt_secret, algorithm="HS256",
    )


@pytest.fixture
def member_token(jwt_secret):
    return jwt.encode(
        {
            "sub": "user_member",
            "email": "member@company.com",
            "org_id": "org_xyz789",
            "org_role": "member",
            "exp": int(time.time()) + 86400,
        },
        jwt_secret, algorithm="HS256",
    )


@pytest.fixture
def org_membership_admin():
    return {
        "PK": "USER#user_abc123",
        "SK": "ORG_MEMBER#org_xyz789",
        "entity_type": "org_membership",
        "user_id": "user_abc123",
        "org_id": "org_xyz789",
        "role": "org_admin",
    }


@pytest.fixture
def workspace_membership_admin():
    return {
        "PK": "USER#user_abc123",
        "SK": "WS_MEMBER#ws_test123",
        "entity_type": "workspace_membership",
        "role": "workspace_admin",
    }


@pytest.fixture
def workspace_membership_member():
    return {
        "PK": "USER#user_member",
        "SK": "WS_MEMBER#ws_test123",
        "entity_type": "workspace_membership",
        "role": "member",
    }


@pytest.fixture
def configured_connection():
    return {
        "PK": "WS#ws_test123",
        "SK": "SLACK_CONNECTION#slack_abc123",
        "entity_type": "slack_connection",
        "connection_id": "slack_abc123",
        "workspace_id": "ws_test123",
        "org_id": "org_xyz789",
        "team_id": "T01TEST",
        "team_name": "Test Workspace",
        "bot_user_id": "U01BOT",
        "config": {
            "include_channels": ["C0123456"],
            "cursors": {},
        },
        "last_sync_at": "2024-01-01T00:00:00+00:00",
        "last_sync_status": "success",
        "last_sync_error": None,
        "file_count": 5,
        "created_by": "user_abc123",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }


@pytest.fixture
def unconfigured_connection():
    return {
        "PK": "WS#ws_test123",
        "SK": "SLACK_CONNECTION#slack_abc123",
        "entity_type": "slack_connection",
        "connection_id": "slack_abc123",
        "workspace_id": "ws_test123",
        "org_id": "org_xyz789",
        "team_id": "T01TEST",
        "team_name": "Test Workspace",
        "bot_user_id": "U01BOT",
        "config": None,
        "last_sync_status": "needs_configuration",
    }


@pytest.fixture
def api_gateway_event():
    def _event(method, path, body=None, headers=None, path_params=None, query_params=None):
        return {
            "rawPath": path,
            "requestContext": {"http": {"method": method, "path": path}},
            "headers": headers or {},
            "body": json.dumps(body) if body else None,
            "pathParameters": path_params,
            "queryStringParameters": query_params,
        }
    return _event


@pytest.fixture
def sqs_event():
    def _event(messages):
        return {
            "Records": [
                {
                    "messageId": f"msg-{i}",
                    "receiptHandle": f"handle-{i}",
                    "body": json.dumps(msg),
                    "eventSource": "aws:sqs",
                    "eventSourceARN": "arn:aws:sqs:us-west-2:123:slack-sync",
                }
                for i, msg in enumerate(messages)
            ]
        }
    return _event
