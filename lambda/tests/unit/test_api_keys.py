# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for API key management in workspace-management Lambda."""

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    """Load or reload the workspace-management handler module."""
    if 'workspace_management_handler' in sys.modules:
        del sys.modules['workspace_management_handler']
    handler_path = os.path.join(os.path.dirname(__file__), '../../workspace-management/handler.py')
    spec = importlib.util.spec_from_file_location("workspace_management_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['workspace_management_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

create_api_key = handler_module.create_api_key
list_api_keys = handler_module.list_api_keys
revoke_api_key = handler_module.revoke_api_key
validate_api_key = handler_module.validate_api_key
lambda_handler = handler_module.lambda_handler


@pytest.fixture
def mock_table():
    """Create a mock DynamoDB table."""
    # Reset cached clients to ensure mocks take effect
    handler_module._dynamodb = None
    handler_module._workspaces_table = None

    table = MagicMock()
    with patch.object(handler_module, '_get_dynamodb') as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        with patch.object(handler_module, '_get_workspaces_table', return_value='test-table'):
            yield table


class TestCreateApiKey:
    """Test API key creation."""

    def test_create_api_key_success(self, mock_table):
        """Test creating an API key with just a name."""
        mock_table.put_item.return_value = {}

        result = create_api_key("user@example.com", {"name": "My Test Key"})

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["key"].startswith("ooa_")
        assert len(body["key"]) == 44  # ooa_ + 40 hex chars
        assert body["key_prefix"] == body["key"][:12]
        assert body["name"] == "My Test Key"
        assert "created_at" in body
        assert "expires_at" not in body

        # Verify DynamoDB put_item was called
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "USER#user@example.com"
        assert item["SK"].startswith("API_KEY#")
        assert item["GSI1PK"].startswith("API_KEY#")
        assert item["status"] == "active"
        # Verify the hash is correct
        expected_hash = hashlib.sha256(body["key"].encode()).hexdigest()
        assert item["key_hash"] == expected_hash

    def test_create_api_key_with_expiration(self, mock_table):
        """Test creating an API key with expiration."""
        mock_table.put_item.return_value = {}

        result = create_api_key("user@example.com", {"name": "Expiring Key", "expires_in_days": 90})

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "expires_at" in body

        # Verify expires_at is roughly 90 days from now
        expires = datetime.fromisoformat(body["expires_at"])
        now = datetime.now(timezone.utc)
        diff = (expires - now).days
        assert 89 <= diff <= 91

    def test_create_api_key_missing_name(self, mock_table):
        """Test creating an API key without a name returns 400."""
        result = create_api_key("user@example.com", {})

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "error" in body


class TestListApiKeys:
    """Test API key listing."""

    def test_list_api_keys_success(self, mock_table):
        """Test listing API keys returns key metadata (not full key or hash)."""
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "USER#user@example.com",
                    "SK": "API_KEY#key-1",
                    "key_id": "key-1",
                    "key_prefix": "ooa_abc12345",
                    "key_hash": "somehash",
                    "name": "Key 1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "status": "active",
                },
                {
                    "PK": "USER#user@example.com",
                    "SK": "API_KEY#key-2",
                    "key_id": "key-2",
                    "key_prefix": "ooa_def67890",
                    "key_hash": "anotherhash",
                    "name": "Key 2",
                    "created_at": "2026-02-01T00:00:00Z",
                    "expires_at": "2026-05-01T00:00:00Z",
                    "last_used_at": "2026-03-01T00:00:00Z",
                    "status": "active",
                },
            ]
        }

        result = list_api_keys("user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        keys = body["api_keys"]
        assert len(keys) == 2
        assert keys[0]["key_id"] == "key-1"
        assert keys[0]["key_prefix"] == "ooa_abc12345"
        assert keys[0]["name"] == "Key 1"
        # Full key and hash must never be returned
        assert "key" not in keys[0]
        assert "key_hash" not in keys[0]

    def test_list_api_keys_empty(self, mock_table):
        """Test listing when no keys exist."""
        mock_table.query.return_value = {"Items": []}

        result = list_api_keys("user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["api_keys"] == []


class TestRevokeApiKey:
    """Test API key revocation."""

    def test_revoke_api_key_success(self, mock_table):
        """Test revoking an active API key."""
        mock_table.update_item.return_value = {}

        result = revoke_api_key("user@example.com", "key-1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["message"] == "API key revoked"

        # Verify update_item called with correct key
        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args[1]
        assert kwargs["Key"] == {"PK": "USER#user@example.com", "SK": "API_KEY#key-1"}

    def test_revoke_uses_condition_expression(self, mock_table):
        """Test that revoke uses ConditionExpression to ensure key exists and belongs to user."""
        mock_table.update_item.return_value = {}

        revoke_api_key("user@example.com", "key-1")

        kwargs = mock_table.update_item.call_args[1]
        # Key is scoped to the user's PK
        assert kwargs["Key"]["PK"] == "USER#user@example.com"
        assert kwargs["Key"]["SK"] == "API_KEY#key-1"
        # ConditionExpression ensures the item exists
        assert "ConditionExpression" in kwargs


class TestValidateApiKey:
    """Test API key validation (internal helper)."""

    def test_validate_api_key_success(self, mock_table):
        """Test validating an active, non-expired key."""
        test_key = "ooa_abc123def456abc123def456abc123def456abcd"
        key_hash = hashlib.sha256(test_key.encode()).hexdigest()

        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "USER#user@example.com",
                    "SK": "API_KEY#key-1",
                    "GSI1PK": f"API_KEY#{key_hash}",
                    "key_id": "key-1",
                    "status": "active",
                }
            ]
        }
        mock_table.update_item.return_value = {}

        result = validate_api_key(test_key)

        assert result is not None
        assert result["email"] == "user@example.com"
        assert result["key_id"] == "key-1"

    def test_validate_api_key_expired(self, mock_table):
        """Test validating an expired key returns None."""
        test_key = "ooa_expired1234567890abcdef1234567890abcdef"
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "USER#user@example.com",
                    "SK": "API_KEY#key-1",
                    "key_id": "key-1",
                    "status": "active",
                    "expires_at": past,
                }
            ]
        }

        result = validate_api_key(test_key)
        assert result is None

    def test_validate_api_key_revoked(self, mock_table):
        """Test validating a revoked key returns None."""
        test_key = "ooa_revoked1234567890abcdef1234567890abcdef"

        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "USER#user@example.com",
                    "SK": "API_KEY#key-1",
                    "key_id": "key-1",
                    "status": "revoked",
                }
            ]
        }

        result = validate_api_key(test_key)
        assert result is None

    def test_validate_api_key_invalid(self, mock_table):
        """Test validating a non-existent key returns None."""
        mock_table.query.return_value = {"Items": []}

        result = validate_api_key("ooa_doesnotexist1234567890abcdef123456")
        assert result is None


class TestApiKeyRoutes:
    """Test API key routes through lambda_handler."""

    def _make_event(self, method, path, body=None, email="user@example.com"):
        """Build a Lambda event for testing."""
        event = {
            "requestContext": {"http": {"method": method, "path": path}},
            "headers": {"x-user-email": email},
        }
        if body:
            event["body"] = json.dumps(body)
        return event

    def test_route_create_api_key(self, mock_table):
        """Test POST /api-keys route."""
        mock_table.put_item.return_value = {}

        event = self._make_event("POST", "/api-keys", {"name": "Route Test Key"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 201

    def test_route_list_api_keys(self, mock_table):
        """Test GET /api-keys route."""
        mock_table.query.return_value = {"Items": []}

        event = self._make_event("GET", "/api-keys")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200

    def test_route_revoke_api_key(self, mock_table):
        """Test DELETE /api-keys/{key_id} route."""
        mock_table.update_item.return_value = {}

        event = self._make_event("DELETE", "/api-keys/test-key-id")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
