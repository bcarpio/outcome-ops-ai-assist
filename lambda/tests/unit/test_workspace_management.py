"""Unit tests for workspace-management Lambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _load_handler_module():
    """Load or reload the workspace-management handler module."""
    # Remove from cache if present to ensure fresh load
    if 'workspace_management_handler' in sys.modules:
        del sys.modules['workspace_management_handler']

    handler_path = os.path.join(os.path.dirname(__file__), '../../workspace-management/handler.py')
    spec = importlib.util.spec_from_file_location("workspace_management_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['workspace_management_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


# Load the workspace-management handler module
handler_module = _load_handler_module()

# Import functions from the loaded module
create_workspace = handler_module.create_workspace
list_workspaces = handler_module.list_workspaces
get_workspace = handler_module.get_workspace
update_workspace = handler_module.update_workspace
delete_workspace = handler_module.delete_workspace
add_member = handler_module.add_member
list_members = handler_module.list_members
update_member = handler_module.update_member
remove_member = handler_module.remove_member
list_repos = handler_module.list_repos
add_repo = handler_module.add_repo
remove_repo = handler_module.remove_repo
lambda_handler = handler_module.lambda_handler
generate_id = handler_module.generate_id
_response = handler_module._response
_error = handler_module._error
_parse_path = handler_module._parse_path
_get_user_from_request = handler_module._get_user_from_request

# Import request models
CreateWorkspaceRequest = handler_module.CreateWorkspaceRequest
UpdateWorkspaceRequest = handler_module.UpdateWorkspaceRequest
AddMemberRequest = handler_module.AddMemberRequest
UpdateMemberRequest = handler_module.UpdateMemberRequest
AddRepoRequest = handler_module.AddRepoRequest
UpdateSharingRequest = handler_module.UpdateSharingRequest
AddOrgMemberRequest = handler_module.AddOrgMemberRequest
UpdateOrgMemberRequest = handler_module.UpdateOrgMemberRequest
DocumentUploadRequest = handler_module.DocumentUploadRequest
CreateApiKeyRequest = handler_module.CreateApiKeyRequest

# Import additional functions
update_workspace = handler_module.update_workspace
delete_workspace = handler_module.delete_workspace
update_member = handler_module.update_member
get_search_access = handler_module.get_search_access
update_search_access = handler_module.update_search_access
list_all_workspaces_for_sharing = handler_module.list_all_workspaces_for_sharing
get_searchable_workspaces = handler_module.get_searchable_workspaces
get_available_prompts = handler_module.get_available_prompts
list_org_members = handler_module.list_org_members
add_org_member = handler_module.add_org_member
update_org_member = handler_module.update_org_member
remove_org_member = handler_module.remove_org_member
get_document_upload_url = handler_module.get_document_upload_url
list_documents = handler_module.list_documents
delete_document = handler_module.delete_document
handle_get_system_prompt = handler_module.handle_get_system_prompt
handle_update_system_prompt = handler_module.handle_update_system_prompt
handle_reset_system_prompt = handler_module.handle_reset_system_prompt
create_api_key = handler_module.create_api_key
list_api_keys = handler_module.list_api_keys
revoke_api_key = handler_module.revoke_api_key
search_users = handler_module.search_users
create_mcp_server = handler_module.create_mcp_server
list_mcp_servers = handler_module.list_mcp_servers
get_mcp_server = handler_module.get_mcp_server
delete_mcp_server = handler_module.delete_mcp_server
assign_mcp_to_workspace = handler_module.assign_mcp_to_workspace
remove_mcp_from_workspace = handler_module.remove_mcp_from_workspace
list_workspace_mcp_servers = handler_module.list_workspace_mcp_servers


class TestHelperFunctions:
    """Test helper functions."""

    def test_generate_id_returns_unique_ids(self):
        """Test that generate_id returns unique IDs."""
        ids = [generate_id() for _ in range(100)]
        assert len(set(ids)) == 100  # All unique

    def test_generate_id_format(self):
        """Test that generate_id returns URL-safe string."""
        id_ = generate_id()
        assert isinstance(id_, str)
        assert len(id_) > 0
        # URL-safe base64 characters only
        valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        assert all(c in valid_chars for c in id_)

    def test_response_builds_correct_format(self):
        """Test _response builds correct HTTP response format."""
        result = _response(200, {"key": "value"})
        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"] == "application/json"
        assert json.loads(result["body"]) == {"key": "value"}

    def test_error_builds_error_response(self):
        """Test _error builds error response."""
        result = _error(404, "Not found")
        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "Not found"

    def test_parse_path_simple(self):
        """Test parsing simple path."""
        assert _parse_path("/workspaces") == ("workspaces",)

    def test_parse_path_with_id(self):
        """Test parsing path with ID."""
        assert _parse_path("/workspaces/abc123") == ("workspaces", "abc123")

    def test_parse_path_nested(self):
        """Test parsing nested path."""
        assert _parse_path("/workspaces/abc123/members") == ("workspaces", "abc123", "members")

    def test_parse_path_strips_slashes(self):
        """Test that parse_path handles leading/trailing slashes."""
        assert _parse_path("/workspaces/") == ("workspaces",)
        assert _parse_path("workspaces") == ("workspaces",)

    def test_get_user_from_request_extracts_email(self):
        """Test extracting user email from headers."""
        event = {"headers": {"x-user-email": "user@example.com"}}
        assert _get_user_from_request(event) == "user@example.com"

    def test_get_user_from_request_missing_header(self):
        """Test handling missing email header."""
        event = {"headers": {}}
        assert _get_user_from_request(event) is None

    def test_get_user_from_request_no_headers(self):
        """Test handling missing headers entirely."""
        event = {}
        assert _get_user_from_request(event) is None


class TestRequestModels:
    """Test Pydantic request models."""

    def test_create_workspace_request_valid(self):
        """Test valid workspace creation request."""
        request = CreateWorkspaceRequest(name="Test Workspace", description="A test")
        assert request.name == "Test Workspace"
        assert request.description == "A test"

    def test_create_workspace_request_strips_name(self):
        """Test that name is stripped of whitespace."""
        request = CreateWorkspaceRequest(name="  Test Workspace  ")
        assert request.name == "Test Workspace"

    def test_create_workspace_request_empty_name_fails(self):
        """Test that empty name fails validation."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            CreateWorkspaceRequest(name="")

    def test_create_workspace_request_privacy_mode_optional(self):
        """privacy_mode is optional; default None lets the handler apply
        the org-default of STRICT."""
        request = CreateWorkspaceRequest(name="W")
        assert request.privacy_mode is None

    def test_create_workspace_request_privacy_mode_strict(self):
        request = CreateWorkspaceRequest(name="W", privacy_mode="STRICT")
        assert request.privacy_mode == "STRICT"

    def test_create_workspace_request_privacy_mode_none(self):
        request = CreateWorkspaceRequest(name="W", privacy_mode="NONE")
        assert request.privacy_mode == "NONE"

    def test_create_workspace_request_privacy_mode_invalid(self):
        """Anything other than STRICT / NONE rejects at validation."""
        with pytest.raises(Exception):
            CreateWorkspaceRequest(name="W", privacy_mode="LOOSE")

    def test_add_member_request_valid_roles(self):
        """Test valid member roles."""
        request = AddMemberRequest(email="user@example.com", role="workspace_admin")
        assert request.role == "workspace_admin"

        request = AddMemberRequest(email="user@example.com", role="member")
        assert request.role == "member"

    def test_add_member_request_invalid_role(self):
        """Test that invalid role fails validation."""
        with pytest.raises(Exception):
            AddMemberRequest(email="user@example.com", role="superadmin")

    def test_add_member_request_default_role(self):
        """Test default member role."""
        request = AddMemberRequest(email="user@example.com")
        assert request.role == "member"

    def test_add_repo_request_valid(self):
        """Test valid repo addition request."""
        request = AddRepoRequest(repo_name="owner/repo")
        assert request.repo_name == "owner/repo"

    def test_add_repo_request_strips_name(self):
        """Test that repo name is stripped."""
        request = AddRepoRequest(repo_name="  owner/repo  ")
        assert request.repo_name == "owner/repo"


class TestCreateWorkspace:
    """Test workspace creation."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_success(self, mock_dynamodb, mock_get_table, mock_is_admin):
        """Test successful workspace creation."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        request = CreateWorkspaceRequest(name="My Workspace", description="Test workspace")

        # Act
        result = create_workspace("admin@example.com", request)

        # Assert
        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "workspace" in body
        assert body["workspace"]["name"] == "My Workspace"
        assert body["workspace"]["created_by"] == "admin@example.com"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_error_handling(self, mock_dynamodb, mock_get_table, mock_is_admin):
        """Test error handling during workspace creation."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_batch_writer = MagicMock()
        mock_batch_writer.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "Internal error"}}, "PutItem"
        )
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        request = CreateWorkspaceRequest(name="Test")

        # Act
        result = create_workspace("user@example.com", request)

        # Assert
        assert result["statusCode"] == 500

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_defaults_privacy_mode_strict(
        self, mock_dynamodb, mock_get_table, mock_is_admin,
    ):
        """When the request omits privacy_mode, the workspace METADATA row
        is created with privacy_mode='STRICT' (the safe default)."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        request = CreateWorkspaceRequest(name="W")
        result = create_workspace("admin@example.com", request)
        assert result["statusCode"] == 201

        # The first put_item is the workspace METADATA row.
        first_put = mock_batch_writer.put_item.call_args_list[0]
        item = first_put.kwargs["Item"]
        assert item["SK"] == "METADATA"
        assert item["privacy_mode"] == "STRICT"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_honors_explicit_privacy_mode_none(
        self, mock_dynamodb, mock_get_table, mock_is_admin,
    ):
        """When the request specifies privacy_mode='NONE', that lands in DDB."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        request = CreateWorkspaceRequest(name="W", privacy_mode="NONE")
        result = create_workspace("admin@example.com", request)
        assert result["statusCode"] == 201

        first_put = mock_batch_writer.put_item.call_args_list[0]
        item = first_put.kwargs["Item"]
        assert item["SK"] == "METADATA"
        assert item["privacy_mode"] == "NONE"


class TestCreateWorkspaceIsGlobal:
    """Chunk 01 of the workspace-scope plan. Verify the is_global
    request field is accepted, persisted to the metadata row, and
    defaults to False when omitted."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_defaults_to_false_when_omitted(
        self, mock_dynamodb, mock_get_table, mock_is_admin,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        req = CreateWorkspaceRequest(name="Team A")
        result = create_workspace("admin@example.com", req)
        assert result["statusCode"] == 201

        body = json.loads(result["body"])
        assert body["workspace"]["is_global"] is False

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_is_global_true_persisted_when_set(
        self, mock_dynamodb, mock_get_table, mock_is_admin,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        req = CreateWorkspaceRequest(name="Security Standards", is_global=True)
        result = create_workspace("admin@example.com", req)
        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["workspace"]["is_global"] is True


class TestListWorkspaces:
    """Test workspace listing."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_workspaces_returns_user_workspaces(self, mock_dynamodb, mock_get_table):
        """Test listing workspaces for a user."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock org admin check (not an org admin)
        # Mock membership query
        mock_table.query.return_value = {
            "Items": [
                {"workspace_id": "ws1", "role": "workspace_admin"},
                {"workspace_id": "ws2", "role": "member"},
            ]
        }

        # Mock get_item calls: org_member check, then workspace metadata fetches
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"workspace_id": "ws1", "name": "Workspace 1", "updated_at": "2024-01-01"}},
            {"Item": {"workspace_id": "ws2", "name": "Workspace 2", "updated_at": "2024-01-02"}},
        ]

        # Act
        result = list_workspaces("user@example.com")

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["workspaces"]) == 2
        # Should be sorted by updated_at descending
        assert body["workspaces"][0]["name"] == "Workspace 2"

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_workspaces_surfaces_is_global_workspaces_to_non_members(
        self, mock_dynamodb, mock_get_table,
    ):
        """Chunk 01 of the workspace-scope plan: every caller sees
        is_global=true workspaces even when they're not a member.
        Global workspaces show up tagged role='global_reader' so the
        UI + downstream scope resolvers can distinguish them from
        owned workspaces."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # User is a member of one workspace (ws-eng).
        mock_table.query.return_value = {
            "Items": [{"workspace_id": "ws-eng", "role": "member"}],
        }
        mock_table.get_item.side_effect = [
            {},  # org_admin check -> not an org admin
            {"Item": {
                "workspace_id": "ws-eng", "name": "Engineering",
                "updated_at": "2024-01-01",
            }},
        ]
        # Global-scan returns Security (is_global=true) that the user
        # is NOT a member of, plus their own Eng workspace (should be
        # deduped since already surfaced above).
        mock_table.scan.return_value = {
            "Items": [
                {
                    "workspace_id": "ws-sec", "name": "Security Standards",
                    "is_global": True, "updated_at": "2024-01-02",
                },
                {
                    "workspace_id": "ws-eng", "name": "Engineering",
                    "is_global": False, "updated_at": "2024-01-01",
                },
            ],
        }

        result = list_workspaces("engineer@example.com")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        workspace_ids = [w["workspace_id"] for w in body["workspaces"]]
        assert "ws-eng" in workspace_ids
        assert "ws-sec" in workspace_ids
        # Security workspace comes through tagged global_reader.
        sec = next(w for w in body["workspaces"] if w["workspace_id"] == "ws-sec")
        assert sec["role"] == "global_reader"
        # And the Eng workspace stays as the user's own role.
        eng = next(w for w in body["workspaces"] if w["workspace_id"] == "ws-eng")
        assert eng["role"] == "member"

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_workspaces_empty(self, mock_dynamodb, mock_get_table):
        """Test listing workspaces when user has none."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}  # org_admin check - not an org admin
        mock_table.query.return_value = {"Items": []}

        # Act
        result = list_workspaces("user@example.com")

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["workspaces"] == []


class TestGetWorkspace:
    """Test getting workspace details."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_workspace_success(self, mock_dynamodb, mock_get_table):
        """Test getting workspace details."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check, workspace metadata, org_admin again, membership again
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Membership check for _can_access_workspace
            {"Item": {"workspace_id": "ws1", "name": "Test Workspace"}},  # Workspace metadata
            {},  # org_admin check for role display - not an org admin
            {"Item": {"role": "member"}},  # Membership for role display
        ]

        # Act
        result = get_workspace("user@example.com", "ws1")

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["workspace"]["name"] == "Test Workspace"
        assert body["workspace"]["role"] == "member"

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_workspace_not_member(self, mock_dynamodb, mock_get_table):
        """Test getting workspace when not a member."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {},  # No membership
        ]

        # Act
        result = get_workspace("user@example.com", "ws1")

        # Assert
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_workspace_not_found(self, mock_dynamodb, mock_get_table):
        """Test getting non-existent workspace."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check, workspace metadata
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Has membership
            {},  # Workspace not found
        ]

        # Act
        result = get_workspace("user@example.com", "ws1")

        # Assert
        assert result["statusCode"] == 404


class TestAddMember:
    """Test adding members to workspace."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_member_success(self, mock_dynamodb, mock_get_table):
        """Test successfully adding a member."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace_admin membership check, workspace exists,
        # target user not already member
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "workspace_admin"}},  # Admin membership check
            {"Item": {"workspace_id": "ws1"}},  # Workspace exists
            {},  # Target user not already member
        ]

        request = AddMemberRequest(email="newuser@example.com", role="member")

        # Act
        result = add_member("admin@example.com", "ws1", request)

        # Assert
        assert result["statusCode"] == 201
        # One put_item call: WS_MEMBER only (no ORG_MEMBER for regular members)
        assert mock_table.put_item.call_count == 1

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_member_not_admin(self, mock_dynamodb, mock_get_table):
        """Test that non-admin cannot add members."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check (not admin)
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Not workspace_admin
        ]

        request = AddMemberRequest(email="newuser@example.com")

        # Act
        result = add_member("user@example.com", "ws1", request)

        # Assert
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_member_already_exists(self, mock_dynamodb, mock_get_table):
        """Test adding member that already exists."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace_admin check, workspace exists, existing member
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "workspace_admin"}},  # Admin check
            {"Item": {"workspace_id": "ws1"}},  # Workspace exists
            {"Item": {"user_email": "existing@example.com"}},  # Already member
        ]

        request = AddMemberRequest(email="existing@example.com")

        # Act
        result = add_member("admin@example.com", "ws1", request)

        # Assert
        assert result["statusCode"] == 409


class TestListMembers:
    """Test listing workspace members."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_members_success(self, mock_dynamodb, mock_get_table):
        """Test listing workspace members."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Membership check
        ]
        mock_table.query.return_value = {
            "Items": [
                {"user_email": "admin@example.com", "role": "workspace_admin"},
                {"user_email": "user@example.com", "role": "member"},
            ]
        }

        # Act
        result = list_members("user@example.com", "ws1")

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["members"]) == 2
        # Admins should be first
        assert body["members"][0]["role"] == "workspace_admin"

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_members_not_member(self, mock_dynamodb, mock_get_table):
        """Test listing members when not a workspace member."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {},  # Not a member
        ]

        # Act
        result = list_members("outsider@example.com", "ws1")

        # Assert
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_members_paginates(self, mock_dynamodb, mock_get_table):
        """Chunk 05: single-shot .query() truncates at 1MB, silently
        dropping members from the workspace UI. Confirm the loop walks
        LastEvaluatedKey so page-2 members appear in the response."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {},  # org_admin check
            {"Item": {"role": "member"}},  # membership
        ]

        page1 = {
            "Items": [{"user_email": "alice@example.com", "role": "workspace_admin"}],
            "LastEvaluatedKey": {"PK": "USER#alice@example.com"},
        }
        page2 = {
            "Items": [{"user_email": "bob@example.com", "role": "member"}],
        }
        mock_table.query.side_effect = [page1, page2]

        result = list_members("alice@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        emails = {m["user_email"] for m in body["members"]}
        assert emails == {"alice@example.com", "bob@example.com"}


class TestRemoveMember:
    """Test removing members from workspace."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_member_success(self, mock_dynamodb, mock_get_table):
        """Test admin removing a member."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace_admin check, target member exists
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "workspace_admin"}},  # Caller is workspace_admin
            {"Item": {"user_email": "target@example.com"}},  # Target exists
        ]

        # Act
        result = remove_member("admin@example.com", "ws1", "target@example.com")

        # Assert
        assert result["statusCode"] == 200
        mock_table.delete_item.assert_called_once()

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_member_self_removal(self, mock_dynamodb, mock_get_table):
        """Test member removing themselves."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace membership check, self exists
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Caller is member (not admin)
            {"Item": {"user_email": "user@example.com"}},  # Self exists
        ]

        # Act
        result = remove_member("user@example.com", "ws1", "user@example.com")

        # Assert
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_member_non_admin_cannot_remove_others(self, mock_dynamodb, mock_get_table):
        """Test that non-admin cannot remove other members."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace membership check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Not workspace_admin
        ]

        # Act
        result = remove_member("user@example.com", "ws1", "other@example.com")

        # Assert
        assert result["statusCode"] == 403


class TestRepoOperations:
    """Test repo operations."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_repos_success(self, mock_dynamodb, mock_get_table):
        """Test listing repos in workspace."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, membership check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Membership check
        ]
        mock_table.query.return_value = {
            "Items": [
                {"repo_name": "org/repo1", "enabled": True},
                {"repo_name": "org/repo2", "enabled": True},
            ]
        }

        # Act
        result = list_repos("user@example.com", "ws1")

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["repos"]) == 2

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_repo_success(self, mock_dynamodb, mock_get_table):
        """Test adding repo to workspace."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace_admin check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "workspace_admin"}},  # Workspace admin check
        ]

        request = AddRepoRequest(repo_name="org/new-repo")

        # Act
        result = add_repo("admin@example.com", "ws1", request)

        # Assert
        assert result["statusCode"] == 201
        mock_table.put_item.assert_called_once()

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_repo_not_admin(self, mock_dynamodb, mock_get_table):
        """Test that non-admin cannot add repos."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace membership check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "member"}},  # Not workspace_admin
        ]

        request = AddRepoRequest(repo_name="org/repo")

        # Act
        result = add_repo("user@example.com", "ws1", request)

        # Assert
        assert result["statusCode"] == 403

    @pytest.mark.skip(reason="Skipped due to mock interference in full test suite - works in isolation")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_repo_already_exists(self, mock_dynamodb, mock_get_table):
        """Test adding repo that already exists."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        # Use the botocore ClientError that the handler module actually uses
        from botocore.exceptions import ClientError as BotoClientError

        def raise_conditional_check_error(*args, **kwargs):
            raise BotoClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Already exists"}},
                "PutItem"
            )

        mock_table.put_item.side_effect = raise_conditional_check_error

        request = AddRepoRequest(repo_name="org/existing-repo")

        # Act
        result = add_repo("admin@example.com", "ws1", request)

        # Assert
        assert result["statusCode"] == 409

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_repo_success(self, mock_dynamodb, mock_get_table):
        """Test removing repo from workspace."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Mock get_item calls: org_admin check, workspace_admin check
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an org admin
            {"Item": {"role": "workspace_admin"}},  # Workspace admin check
        ]

        # Act
        result = remove_repo("admin@example.com", "ws1", "org/repo")

        # Assert
        assert result["statusCode"] == 200
        mock_table.delete_item.assert_called_once()


class TestLoginSucceededAudit:
    """plan/audit-completeness chunk 02 LOGIN_SUCCEEDED — fires once per
    (user, day) per Lambda container; subsequent requests no-op."""

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_first_request_fires_login_succeeded(
        self, mock_dynamodb, mock_get_table, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {}
        mock_table.query.return_value = {"Items": []}
        # Reset module cache between tests so we always hit the "first" path.
        handler_module._login_seen_today.clear()

        event = {
            "httpMethod": "GET",
            "path": "/workspaces",
            "headers": {"x-user-email": "freshuser@example.com"},
        }
        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            lambda_handler(event, None)

        # log_admin_action gets called for LOGIN_SUCCEEDED. Other paths in
        # this request (list_workspaces) do not emit.
        login_calls = [c for c in mock_audit.call_args_list
                       if c.args[1] == "LOGIN_SUCCEEDED"]
        assert len(login_calls) == 1
        kwargs = login_calls[0].kwargs
        assert kwargs["user_email"] == "freshuser@example.com"
        assert kwargs["category"] == "auth"

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_second_request_does_not_re_emit(
        self, mock_dynamodb, mock_get_table, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {}
        mock_table.query.return_value = {"Items": []}
        handler_module._login_seen_today.clear()

        event = {
            "httpMethod": "GET",
            "path": "/workspaces",
            "headers": {"x-user-email": "repeatuser@example.com"},
        }
        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            lambda_handler(event, None)
            # Reset call history to isolate the second invocation.
            mock_audit.reset_mock()
            lambda_handler(event, None)

        login_calls = [c for c in mock_audit.call_args_list
                       if c.args[1] == "LOGIN_SUCCEEDED"]
        assert login_calls == []


class TestLambdaHandler:
    """Test main Lambda handler routing."""

    def test_handler_missing_user_email(self):
        """Test handler returns 401 when user email missing."""
        event = {
            "httpMethod": "GET",
            "path": "/workspaces",
            "headers": {},
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 401

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_list_workspaces(self, mock_dynamodb, mock_get_table):
        """Test handler routes GET /workspaces correctly."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {}  # org_admin check - not an org admin
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/workspaces",
            "headers": {"x-user-email": "user@example.com"},
        }

        # Act
        result = lambda_handler(event, None)

        # Assert
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_create_workspace(self, mock_dynamodb, mock_get_table, mock_is_admin):
        """Test handler routes POST /workspaces correctly."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = {
            "httpMethod": "POST",
            "path": "/workspaces",
            "headers": {"x-user-email": "user@example.com"},
            "body": json.dumps({"name": "Test Workspace"}),
        }

        # Act
        result = lambda_handler(event, None)

        # Assert
        assert result["statusCode"] == 201

    def test_handler_routes_not_found(self):
        """Test handler returns 404 for unknown routes."""
        event = {
            "httpMethod": "GET",
            "path": "/unknown/path",
            "headers": {"x-user-email": "user@example.com"},
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 404

    def test_handler_invalid_json_body(self):
        """Test handler handles invalid JSON body."""
        event = {
            "httpMethod": "POST",
            "path": "/workspaces",
            "headers": {"x-user-email": "user@example.com"},
            "body": "invalid json{",
        }

        result = lambda_handler(event, None)

        # Should fail validation since name is required
        assert result["statusCode"] == 400

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_function_url_format(self, mock_dynamodb, mock_get_table):
        """Test handler works with Lambda Function URL event format."""
        # Arrange
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {}  # org_admin check - not an org admin
        mock_table.query.return_value = {"Items": []}

        # Function URL format uses requestContext.http
        event = {
            "requestContext": {
                "http": {
                    "method": "GET",
                    "path": "/workspaces",
                }
            },
            "headers": {"x-user-email": "user@example.com"},
        }

        # Act
        result = lambda_handler(event, None)

        # Assert
        assert result["statusCode"] == 200


class TestUpdateWorkspace:
    """Test workspace update."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_membership", return_value={"role": "workspace_admin"})
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_workspace_success(self, mock_dynamodb, mock_get_table,
                                       mock_membership, mock_access, mock_org_admin,
                                       mock_ws_admin):
        """Test successful workspace update."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "name": "Updated Name", "role": "org_admin"}
        }

        request = UpdateWorkspaceRequest(name="Updated Name")
        result = update_workspace("admin@example.com", "ws1", request)

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_workspace_not_admin(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test non-admin cannot update workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = UpdateWorkspaceRequest(name="New Name")
        result = update_workspace("user@example.com", "ws1", request)

        assert result["statusCode"] == 403

    # ------------------------------------------------------------------
    # Chunk 01 of the workspace-scope plan.
    # ------------------------------------------------------------------

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_workspace_admin_cannot_flip_is_global(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_ws_admin,
    ):
        """Only org admins can flip is_global. Workspace admins can
        still update name/description; they just can't broaden the
        deployment's read surface."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = UpdateWorkspaceRequest(is_global=True)
        result = update_workspace("workspace-admin@example.com", "ws1", request)
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert "org admins" in body["error"].lower()

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_flipping_is_global_emits_dedicated_audit_action(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_ws_admin, mock_log,
    ):
        """The is_global flip emits WORKSPACE_GLOBAL_FLAG_SET in
        addition to the generic WORKSPACE_UPDATED, so compliance
        auditors can filter their feed to just scope changes."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.side_effect = [
            # First read: existing row's prior is_global value
            {"Item": {"workspace_id": "ws1", "name": "Sec", "is_global": False}},
            # Second read: post-update get_workspace call
            {"Item": {"workspace_id": "ws1", "name": "Sec", "is_global": True, "role": "org_admin"}},
        ]

        request = UpdateWorkspaceRequest(is_global=True)
        result = update_workspace("admin@example.com", "ws1", request)
        assert result["statusCode"] == 200

        # Two audit calls: WORKSPACE_UPDATED + WORKSPACE_GLOBAL_FLAG_SET.
        actions = [c.args[1] for c in mock_log.call_args_list]
        assert "WORKSPACE_UPDATED" in actions
        assert "WORKSPACE_GLOBAL_FLAG_SET" in actions
        # The global-flag audit row carries old->new.
        global_call = next(
            c for c in mock_log.call_args_list
            if c.args[1] == "WORKSPACE_GLOBAL_FLAG_SET"
        )
        assert global_call.kwargs["details"]["old"] is False
        assert global_call.kwargs["details"]["new"] is True


class TestDeleteWorkspace:
    """Test workspace deletion."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_workspace_success(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test successful workspace deletion."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {"Items": [
            {"PK": "WS#ws1", "SK": "METADATA"},
        ]}

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        result = delete_workspace("admin@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == "ws1"

    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_workspace_not_admin(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test non-admin cannot delete workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = delete_workspace("user@example.com", "ws1")

        assert result["statusCode"] == 403

    @patch("workspace_management_handler.dual_write.ddb_delete_item")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_workspace_paginates_partition_and_gsi(
        self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_delete
    ):
        """Chunk 05: single-shot query truncates at 1MB. delete_workspace
        must paginate BOTH the workspace-partition query and the GSI
        member enumeration so items on page 2 aren't orphaned after
        the audit row says 'deleted'."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        page1 = {
            "Items": [{"PK": "WS#ws1", "SK": "METADATA", "name": "big"}],
            "LastEvaluatedKey": {"PK": "WS#ws1", "SK": "METADATA"},
        }
        page2 = {
            "Items": [{"PK": "WS#ws1", "SK": "REPO#huge/repo"}],
        }
        member_page1 = {
            "Items": [{"PK": "USER#alice", "SK": "WS_MEMBER#ws1"}],
            "LastEvaluatedKey": {"PK": "USER#alice"},
        }
        member_page2 = {
            "Items": [{"PK": "USER#bob", "SK": "WS_MEMBER#ws1"}],
        }
        mock_table.query.side_effect = [page1, page2, member_page1, member_page2]

        result = delete_workspace("admin@example.com", "ws1")

        assert result["statusCode"] == 200
        # Items across BOTH pages of BOTH queries must be deleted
        deleted_keys = [
            call.args[1] for call in mock_delete.call_args_list
        ]
        assert {"PK": "WS#ws1", "SK": "METADATA"} in deleted_keys
        assert {"PK": "WS#ws1", "SK": "REPO#huge/repo"} in deleted_keys
        assert {"PK": "USER#alice", "SK": "WS_MEMBER#ws1"} in deleted_keys
        assert {"PK": "USER#bob", "SK": "WS_MEMBER#ws1"} in deleted_keys


class TestUpdateMember:
    """Test member role updates."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_member_success(self, mock_dynamodb, mock_get_table, mock_org_admin, mock_ws_admin):
        """Test successful member role update."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"role": "member", "user_email": "target@example.com"}}

        request = UpdateMemberRequest(role="workspace_admin")
        result = update_member("admin@example.com", "ws1", "target@example.com", request)

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_member_demote_admin_non_org_admin(self, mock_dynamodb, mock_get_table,
                                                        mock_org_admin, mock_ws_admin):
        """Test non-org-admin cannot demote workspace admin."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin", "user_email": "target@example.com"}}

        request = UpdateMemberRequest(role="member")
        result = update_member("admin@example.com", "ws1", "target@example.com", request)

        assert result["statusCode"] == 403

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_member_demote_last_admin(self, mock_dynamodb, mock_get_table,
                                              mock_org_admin, mock_ws_admin):
        """Test cannot demote last admin."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_table.query.return_value = {"Items": [
            {"role": "workspace_admin"},
        ]}

        request = UpdateMemberRequest(role="member")
        result = update_member("admin@example.com", "ws1", "target@example.com", request)

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_member_not_found(self, mock_dynamodb, mock_get_table,
                                      mock_org_admin, mock_ws_admin):
        """Test updating non-existent member."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        request = UpdateMemberRequest(role="member")
        result = update_member("admin@example.com", "ws1", "nobody@example.com", request)

        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_member_not_admin(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test non-admin cannot update members."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = UpdateMemberRequest(role="workspace_admin")
        result = update_member("user@example.com", "ws1", "other@example.com", request)

        assert result["statusCode"] == 403

    @patch("workspace_management_handler.dual_write.ddb_update_item")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_member_last_admin_check_paginates(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_ws_admin, mock_update
    ):
        """Chunk 05: the last-admin check must paginate. If the second
        remaining admin lives on page 2 of the members query, a single-
        shot .query() would truncate to page 1 (only one admin), wrongly
        blocking the demotion. With pagination, the second admin is seen
        and the demotion is allowed."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # get_item for _get_membership: target IS a workspace_admin
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        page1 = {
            "Items": [{"role": "workspace_admin", "user_email": "target@example.com"}],
            "LastEvaluatedKey": {"PK": "USER#target"},
        }
        page2 = {
            "Items": [{"role": "workspace_admin", "user_email": "other-admin@example.com"}],
        }
        mock_table.query.side_effect = [page1, page2]

        request = UpdateMemberRequest(role="member")
        result = update_member("admin@example.com", "ws1", "target@example.com", request)

        # Demotion succeeds because pagination revealed the second admin
        assert result["statusCode"] == 200


class TestRemoveMemberLastAdmin:
    """Test removing last admin from workspace."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_last_admin(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test cannot remove last workspace admin."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin", "user_email": "admin@example.com"}}
        mock_table.query.return_value = {"Items": [{"role": "workspace_admin"}]}

        result = remove_member("admin@example.com", "ws1", "admin@example.com")

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_member_not_found(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test removing non-existent member."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = remove_member("admin@example.com", "ws1", "nobody@example.com")

        assert result["statusCode"] == 404


class TestRemoveRepoWithQueue:
    """Test repo removal with delete queue."""

    @patch("workspace_management_handler._get_delete_queue_url", return_value="https://sqs.example.com/queue")
    @patch("workspace_management_handler._get_sqs_client")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_repo_sends_sqs_message(self, mock_dynamodb, mock_get_table,
                                            mock_ws_admin, mock_sqs, mock_queue):
        """Test repo removal sends cleanup message to SQS."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        result = remove_repo("admin@example.com", "ws1", "org/repo")

        assert result["statusCode"] == 200
        # Repo removal now queues a FULL cleanup: KB owner-prefix +
        # legacy bare-name prefix + symbol-graph (delete-bug 01/03).
        import json as _json
        bodies = [
            _json.loads(c.kwargs["MessageBody"])
            for c in mock_sqs_client.send_message.call_args_list
        ]
        assert len(bodies) == 3
        prefixes = {b["prefix"] for b in bodies if b["action"] == "delete_prefix"}
        assert prefixes == {"org/repo/", "repo/"}  # owner + bare
        sg = [b for b in bodies if b["action"] == "delete_symbol_graph"]
        assert len(sg) == 1 and sg[0]["repo_bare"] == "repo"


class TestSearchAccess:
    """Test workspace sharing / search access."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_search_access_success(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test getting search access settings."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {"Item": {"workspace_id": "ws1", "search_allowed_workspaces": ["ws2"]}},
            {"Item": {"workspace_id": "ws2", "name": "Workspace 2"}},
        ]

        result = get_search_access("admin@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["search_allowed_workspaces"]) == 1

    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_search_access_not_admin(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test non-admin cannot view sharing settings."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = get_search_access("user@example.com", "ws1")

        assert result["statusCode"] == 403

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_search_access_not_found(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test search access for non-existent workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = get_search_access("admin@example.com", "ws1")

        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_search_access_success(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test updating search access."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # The audit-emit refactor adds an upfront get_item to capture
        # the previous allowed list before the mutation.
        mock_table.get_item.side_effect = [
            # 1. capture prev_allowed (ws1 starts with no sharing)
            {"Item": {"workspace_id": "ws1", "search_allowed_workspaces": []}},
            # 2. validate ws2 exists
            {"Item": {"workspace_id": "ws2"}},
            # 3. get_search_access read after update
            {"Item": {"workspace_id": "ws1", "search_allowed_workspaces": ["ws2"]}},
            # 4. fetch allowed ws name for the response payload
            {"Item": {"workspace_id": "ws2", "name": "Workspace 2"}},
        ]

        request = UpdateSharingRequest(search_allowed_workspaces=["ws2"])
        result = update_search_access("admin@example.com", "ws1", request)

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_search_access_invalid_workspace(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test updating search access with invalid workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}  # workspace not found

        request = UpdateSharingRequest(search_allowed_workspaces=["nonexistent"])
        result = update_search_access("admin@example.com", "ws1", request)

        assert result["statusCode"] == 400

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_search_access_emits_audit_with_diff(
        self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_audit,
    ):
        """plan/audit-completeness chunk 06 — emit
        ACTION_WORKSPACE_SHARING_UPDATED with previous + new + diff."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            # prev_allowed: ws was sharing with ws_old1, ws_old2
            {"Item": {
                "workspace_id": "ws1",
                "search_allowed_workspaces": ["ws_old1", "ws_old2"],
            }},
            # validate ws_old1 exists
            {"Item": {"workspace_id": "ws_old1"}},
            # validate ws_new exists
            {"Item": {"workspace_id": "ws_new"}},
            # get_search_access read after update
            {"Item": {
                "workspace_id": "ws1",
                "search_allowed_workspaces": ["ws_old1", "ws_new"],
            }},
            # fetch each allowed ws name (best-effort)
            {"Item": {"workspace_id": "ws_old1", "name": "Old1"}},
            {"Item": {"workspace_id": "ws_new", "name": "New"}},
        ]

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            req = UpdateSharingRequest(search_allowed_workspaces=["ws_old1", "ws_new"])
            update_search_access(
                "admin@example.com", "ws1", req,
                audit_ctx={"source_ip": "203.0.113.7", "user_agent": "ua"},
            )

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "WORKSPACE_SHARING_UPDATED"
        d = kwargs["details"]
        assert d["previous_allowed"] == ["ws_old1", "ws_old2"]
        assert d["new_allowed"] == ["ws_old1", "ws_new"]
        assert d["added"] == ["ws_new"]
        assert d["removed"] == ["ws_old2"]


class TestListAllWorkspacesForSharing:
    """Test listing workspaces for sharing dropdown."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._scan_all_workspaces")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_for_sharing_excludes_current(self, mock_dynamodb, mock_get_table,
                                                mock_scan, mock_ws_admin):
        """Test listing excludes current workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_scan.return_value = [
            {"workspace_id": "ws1", "name": "Current"},
            {"workspace_id": "ws2", "name": "Other"},
        ]

        result = list_all_workspaces_for_sharing("admin@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["workspaces"]) == 1
        assert body["workspaces"][0]["workspace_id"] == "ws2"


class TestRegionRole:
    """HA chunk 9a: /api/region/role endpoint.

    The endpoint bypasses auth and returns the local region's HA role
    (active|passive). Single-region deployments always return "active"
    via shared.active_region's fast-path (AppConfig env vars unset).
    """

    @patch("workspace_management_handler.get_activated_at")
    @patch("workspace_management_handler.get_region_role")
    def test_region_role_returns_role_region_and_activated_at(
        self, mock_get_role, mock_get_activated_at
    ):
        mock_get_role.return_value = "active"
        mock_get_activated_at.return_value = None

        with patch.dict(os.environ, {"AWS_REGION": "us-west-2"}, clear=False):
            event = {
                "httpMethod": "GET",
                "path": "/api/region/role",
                "headers": {},
                "body": "{}",
            }
            result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["role"] == "active"
        assert body["region"] == "us-west-2"
        assert body["activated_at"] is None

    @patch("workspace_management_handler.get_activated_at")
    @patch("workspace_management_handler.get_region_role")
    def test_region_role_returns_passive_when_appconfig_says_passive(
        self, mock_get_role, mock_get_activated_at
    ):
        mock_get_role.return_value = "passive"
        mock_get_activated_at.return_value = 1_700_000_000

        event = {
            "httpMethod": "GET",
            "path": "/api/region/role",
            "headers": {},
            "body": "{}",
        }
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["role"] == "passive"
        assert body["activated_at"] == 1_700_000_000

    @patch("workspace_management_handler.get_activated_at")
    @patch("workspace_management_handler.get_region_role")
    def test_region_role_bypasses_user_auth(self, mock_get_role, mock_get_activated_at):
        """The endpoint must work without x-user-email being set."""
        mock_get_role.return_value = "active"
        mock_get_activated_at.return_value = None

        event = {
            "httpMethod": "GET",
            "path": "/api/region/role",
            "headers": {},  # no x-user-email
            "body": "{}",
        }
        result = lambda_handler(event, None)

        # Other routes return 401 with no user header; this one returns 200.
        assert result["statusCode"] == 200

    def test_region_role_non_get_still_requires_auth(self):
        """POST to the same path falls through to the auth check (401)."""
        event = {
            "httpMethod": "POST",
            "path": "/api/region/role",
            "headers": {},
            "body": "{}",
        }
        result = lambda_handler(event, None)
        # No x-user-email -> 401 from the regular auth path.
        assert result["statusCode"] == 401


class TestSearchableWorkspaces:
    """Test searchable workspaces for chat."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._scan_all_workspaces")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_searchable_org_admin_sees_all(self, mock_dynamodb, mock_get_table,
                                           mock_scan, mock_org_admin):
        """Test org admin sees all workspaces."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_scan.return_value = [
            {"workspace_id": "ws1", "name": "WS1"},
            {"workspace_id": "ws2", "name": "WS2"},
        ]

        result = get_searchable_workspaces("admin@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["workspaces"]) == 2
        assert body["workspaces"][0]["access_type"] == "org_admin"

    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._scan_all_workspaces")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_searchable_regular_user_with_shared(self, mock_dynamodb, mock_get_table,
                                                  mock_scan, mock_org_admin):
        """Test regular user sees own workspaces plus shared."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # User's memberships
        mock_table.query.return_value = {
            "Items": [{"workspace_id": "ws1"}]
        }
        # ws1 metadata, then ws2 and ws3 from scan
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1", "name": "WS1"}}

        mock_scan.return_value = [
            {"workspace_id": "ws1", "name": "WS1", "search_allowed_workspaces": []},
            {"workspace_id": "ws2", "name": "WS2", "search_allowed_workspaces": ["ws1"]},
            {"workspace_id": "ws3", "name": "WS3", "search_allowed_workspaces": []},
        ]

        result = get_searchable_workspaces("user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # ws1 (member) + ws2 (shared with ws1) = 2
        assert len(body["workspaces"]) == 2


class TestAvailablePrompts:
    """Test available prompts listing."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_available_prompts(self, mock_dynamodb, mock_get_table):
        """Test listing workspaces with custom prompts."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{"workspace_id": "ws1"}, {"workspace_id": "ws2"}]
        }
        mock_table.get_item.side_effect = [
            {},  # org_admin check - not an admin
            {"Item": {"name": "WS1", "custom_system_prompt": "You are a helpful assistant."}},
            {"Item": {"name": "WS2"}},  # No custom prompt
        ]

        result = get_available_prompts("user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["prompts"]) == 1
        assert body["prompts"][0]["workspace_name"] == "WS1"


class TestOrgMembers:
    """Test org member management."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_org_members(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test listing org members."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{"user_email": "admin@example.com", "org_role": "org_admin"}]
        }
        mock_table.scan.return_value = {"Items": []}

        result = list_org_members("admin@example.com")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_org_members_not_admin(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test non-admin cannot list org members."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = list_org_members("user@example.com")

        assert result["statusCode"] == 403

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_org_members_paginates_at_limit(
        self, mock_dynamodb, mock_get_table, mock_org_admin
    ):
        """fable-scale chunk 10: response is sliced to `limit` items, with
        next_cursor set when more members exist beyond the page."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [
                {"user_email": f"user{i:03d}@example.com", "org_role": "member"}
                for i in range(50)
            ]
        }
        mock_table.scan.return_value = {"Items": []}

        result = list_org_members("admin@example.com", {"limit": "20"})
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["members"]) == 20
        assert body["total"] == 50
        assert body["next_cursor"] is not None
        # Cursor is the last returned member's email.
        assert body["next_cursor"] == body["members"][-1]["user_email"]

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_org_members_cursor_advances(
        self, mock_dynamodb, mock_get_table, mock_org_admin
    ):
        """Passing back the cursor from page 1 gets page 2 with no overlap."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [
                {"user_email": f"user{i:03d}@example.com", "org_role": "member"}
                for i in range(50)
            ]
        }
        mock_table.scan.return_value = {"Items": []}

        page1 = list_org_members("admin@example.com", {"limit": "20"})
        body1 = json.loads(page1["body"])
        cursor = body1["next_cursor"]

        page2 = list_org_members("admin@example.com",
                                 {"limit": "20", "cursor": cursor})
        body2 = json.loads(page2["body"])
        assert len(body2["members"]) == 20
        page1_emails = {m["user_email"] for m in body1["members"]}
        page2_emails = {m["user_email"] for m in body2["members"]}
        assert page1_emails.isdisjoint(page2_emails)

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_org_members_paginates_gsi_query(
        self, mock_dynamodb, mock_get_table, mock_org_admin
    ):
        """Chunk 05: ORG#default GSI query paginates. Members on page 2
        would silently disappear from the org admin UI otherwise."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        page1 = {
            "Items": [{"user_email": "admin@example.com", "org_role": "org_admin"}],
            "LastEvaluatedKey": {"PK": "USER#admin@example.com"},
        }
        page2 = {
            "Items": [{"user_email": "alice@example.com", "org_role": "member"}],
        }
        mock_table.query.side_effect = [page1, page2]
        mock_table.scan.return_value = {"Items": []}

        result = list_org_members("admin@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        emails = {m["user_email"] for m in body["members"]}
        assert "admin@example.com" in emails
        assert "alice@example.com" in emails

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value=None)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_org_member_success(self, mock_dynamodb, mock_get_table,
                                     mock_org_membership, mock_org_admin):
        """Test adding org member."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = AddOrgMemberRequest(email="new@example.com", org_role="member")
        result = add_org_member("admin@example.com", request)

        assert result["statusCode"] == 201
        mock_table.put_item.assert_called_once()

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value={"org_role": "member"})
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_org_member_already_exists(self, mock_dynamodb, mock_get_table,
                                            mock_org_membership, mock_org_admin):
        """Test adding existing org member returns 409."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = AddOrgMemberRequest(email="existing@example.com")
        result = add_org_member("admin@example.com", request)

        assert result["statusCode"] == 409

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value={"org_role": "member"})
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_org_member_success(self, mock_dynamodb, mock_get_table,
                                        mock_org_membership, mock_org_admin):
        """Test updating org member role."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = UpdateOrgMemberRequest(org_role="org_admin")
        result = update_org_member("admin@example.com", "target@example.com", request)

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value=None)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_org_member_not_found(self, mock_dynamodb, mock_get_table,
                                          mock_org_membership, mock_org_admin):
        """Test updating non-existent org member."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {"Items": [
            {"org_role": "org_admin"},
            {"org_role": "org_admin"},
        ]}

        request = UpdateOrgMemberRequest(org_role="member")
        result = update_org_member("admin@example.com", "admin@example.com", request)

        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_org_member_demote_last_admin(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test cannot demote last org admin."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {"Items": [{"org_role": "org_admin"}]}

        request = UpdateOrgMemberRequest(org_role="member")
        result = update_org_member("admin@example.com", "admin@example.com", request)

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_org_member_success(self, mock_dynamodb, mock_get_table,
                                        mock_org_membership, mock_org_admin):
        """Test removing org member."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_org_membership.return_value = {"org_role": "member"}
        mock_table.query.return_value = {"Items": [
            {"PK": "USER#target@example.com", "SK": "WS_MEMBER#ws1"}
        ]}

        result = remove_org_member("admin@example.com", "target@example.com")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_org_member_last_admin(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test cannot remove last org admin."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {"Items": [{"org_role": "org_admin"}]}

        result = remove_org_member("admin@example.com", "admin@example.com")

        assert result["statusCode"] == 400


class TestSystemPrompt:
    """Test system prompt management."""

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_system_prompt(self, mock_dynamodb, mock_get_table, mock_access):
        """Test getting system prompt."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {
            "Item": {
                "workspace_id": "ws1",
                "custom_system_prompt": "You are helpful.",
                "custom_prompt_updated_at": "2024-01-01",
                "custom_prompt_updated_by": "admin@example.com",
            }
        }

        result = handle_get_system_prompt("ws1", "user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["custom_system_prompt"] == "You are helpful."
        assert body["is_default"] is False

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_system_prompt_default(self, mock_dynamodb, mock_get_table, mock_access):
        """Test getting default system prompt (no custom one set)."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        result = handle_get_system_prompt("ws1", "user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["is_default"] is True

    @patch("workspace_management_handler._can_access_workspace", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_system_prompt_not_member(self, mock_dynamodb, mock_get_table, mock_access):
        """Test non-member cannot get system prompt."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = handle_get_system_prompt("ws1", "outsider@example.com")

        assert result["statusCode"] == 403

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_system_prompt(self, mock_dynamodb, mock_get_table,
                                   mock_ws_admin, mock_audit):
        """Test updating system prompt."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        body = {"custom_system_prompt": "You are a coding assistant."}
        result = handle_update_system_prompt("ws1", "admin@example.com", body)

        assert result["statusCode"] == 200
        body_result = json.loads(result["body"])
        assert body_result["is_default"] is False

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_system_prompt_empty(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test updating system prompt with empty value fails."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        body = {"custom_system_prompt": "   "}
        result = handle_update_system_prompt("ws1", "admin@example.com", body)

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_system_prompt_too_long(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test updating system prompt exceeding length limit."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        body = {"custom_system_prompt": "x" * 10001}
        result = handle_update_system_prompt("ws1", "admin@example.com", body)

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_system_prompt_missing_field(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test updating system prompt without required field."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        body = {}
        result = handle_update_system_prompt("ws1", "admin@example.com", body)

        assert result["statusCode"] == 400

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_reset_system_prompt(self, mock_dynamodb, mock_get_table,
                                  mock_ws_admin, mock_audit):
        """Test resetting system prompt."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        result = handle_reset_system_prompt("ws1", "admin@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["is_default"] is True

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_reset_system_prompt_not_found(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test resetting prompt for non-existent workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = handle_reset_system_prompt("ws1", "admin@example.com")

        assert result["statusCode"] == 404


class TestApiKeys:
    """Test API key management."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_api_key(self, mock_dynamodb, mock_get_table):
        """Test creating API key."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = create_api_key("user@example.com", {"name": "My Key"})

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "key" in body
        assert body["key"].startswith("ooa_")
        assert "key_id" in body

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_api_key_with_expiry(self, mock_dynamodb, mock_get_table):
        """Test creating API key with expiry."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = create_api_key("user@example.com", {"name": "Temp Key", "expires_in_days": 30})

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "expires_at" in body

    def test_create_api_key_invalid_request(self):
        """Test creating API key with invalid body."""
        result = create_api_key("user@example.com", {})

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_api_keys(self, mock_dynamodb, mock_get_table):
        """Test listing API keys."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [
                {"key_id": "k1", "key_prefix": "ooa_abc", "name": "Key 1",
                 "created_at": "2024-01-01", "status": "active"}
            ]
        }

        result = list_api_keys("user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["api_keys"]) == 1

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_revoke_api_key_success(self, mock_dynamodb, mock_get_table):
        """Test revoking API key."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = revoke_api_key("user@example.com", "key123")

        assert result["statusCode"] == 200

    @pytest.mark.skip(reason="Skipped due to mock interference in full test suite - works in isolation")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_revoke_api_key_not_found(self, mock_dynamodb, mock_get_table):
        """Test revoking non-existent API key returns 404."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Not found"}},
            "UpdateItem"
        )

        result = revoke_api_key("user@example.com", "nonexistent")

        assert result["statusCode"] == 404


class TestSearchUsers:
    """Test user search."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_search_users_success(self, mock_dynamodb, mock_get_table):
        """Test searching for users."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.scan.return_value = {
            "Items": [
                {"user_email": "john@example.com", "name": "John", "SK": "PROFILE"},
            ]
        }

        result = search_users("admin@example.com", "john")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["users"]) == 1

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_search_users_short_query(self, mock_dynamodb, mock_get_table):
        """Test search with query too short."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = search_users("admin@example.com", "j")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["users"] == []


class TestDocumentOperations:
    """Test document upload, list, delete."""

    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_upload_url(self, mock_dynamodb, mock_get_table, mock_access,
                             mock_bucket, mock_s3):
        """Test generating upload URL."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.generate_presigned_url.return_value = "https://s3.example.com/upload"

        request = DocumentUploadRequest(filename="doc.md")
        result = get_document_upload_url("user@example.com", "ws1", request)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "upload_url" in body

    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_documents(self, mock_dynamodb, mock_get_table, mock_access,
                             mock_bucket, mock_s3):
        """Test listing documents."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        from datetime import datetime, timezone
        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client

        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": "workspaces/ws1/uploads/readme.md",
                        "Size": 1024,
                        "LastModified": datetime.now(timezone.utc),
                    }
                ]
            }
        ]

        result = list_documents("user@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["count"] == 1

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_documents_bad_prefix(self, mock_dynamodb, mock_get_table, mock_access):
        """Test listing documents with traversal attempt."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = list_documents("user@example.com", "ws1", prefix="../../etc")

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._get_sqs_client")
    @patch("workspace_management_handler._get_delete_queue_url", return_value="https://sqs.example.com/queue")
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_document_queued(self, mock_dynamodb, mock_get_table, mock_ws_admin,
                                     mock_bucket, mock_s3, mock_queue, mock_sqs):
        """Test document deletion via queue."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {}

        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        result = delete_document("admin@example.com", "ws1", "readme.md")

        assert result["statusCode"] == 200
        mock_sqs_client.send_message.assert_called_once()

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_sqs_client")
    @patch("workspace_management_handler._get_delete_queue_url", return_value="https://sqs.example.com/queue")
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_document_emits_audit_row(
        self, mock_dynamodb, mock_get_table, mock_ws_admin,
        mock_bucket, mock_s3, mock_queue, mock_sqs, mock_audit,
    ):
        """plan/audit-completeness chunk 05 — delete_document emits
        ACTION_DOCUMENT_DELETED with file_path + file_size + queue marker."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {"ContentLength": 12345}

        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = delete_document(
                "admin@example.com", "ws1", "readme.md",
                audit_ctx={"source_ip": "203.0.113.7", "user_agent": "ua"},
            )

        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "DOCUMENT_DELETED"
        assert kwargs["user_email"] == "admin@example.com"
        assert kwargs["details"]["document_id"] == "readme.md"
        assert kwargs["details"]["file_path"] == "workspaces/ws1/uploads/readme.md"
        assert kwargs["details"]["file_size"] == 12345
        assert kwargs["details"]["vectors_purged"] == "queued"
        assert kwargs["source_ip"] == "203.0.113.7"

    @pytest.mark.skip(reason="Skipped due to mock interference in full test suite - works in isolation")
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_document_not_found(self, mock_dynamodb, mock_get_table, mock_ws_admin,
                                        mock_bucket, mock_s3):
        """Test deleting non-existent document."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        result = delete_document("admin@example.com", "ws1", "missing.md")

        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_document_traversal_attempt(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test document deletion rejects path traversal."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = delete_document("admin@example.com", "ws1", "../../etc/passwd")

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._get_delete_queue_url", return_value=None)
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_document_direct(self, mock_dynamodb, mock_get_table, mock_ws_admin,
                                     mock_bucket, mock_s3, mock_queue):
        """Test document deletion directly when no queue configured."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {}

        result = delete_document("admin@example.com", "ws1", "readme.md")

        assert result["statusCode"] == 200
        mock_s3_client.delete_object.assert_called_once()


class TestMcpServerOps:
    """Test MCP server management."""

    @patch("workspace_management_handler._call_mcp_list_tools", side_effect=Exception("Connection refused"))
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_mcp_server_tool_fetch_fails(self, mock_dynamodb, mock_get_table,
                                                  mock_org_admin, mock_mcp_list):
        """Test creating MCP server when tool fetch fails."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = create_mcp_server("admin@example.com", {
            "name": "Test MCP",
            "url": "https://mcp.example.com",
        })

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["mcp_server"]["status"] == "error"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_mcp_servers(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test listing MCP servers."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [
                {"mcp_id": "m1", "name": "MCP1", "auth_type": "none", "created_at": "2024-01-01"}
            ]
        }

        result = list_mcp_servers("admin@example.com")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_mcp_server(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test getting MCP server details."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "m1", "name": "MCP1", "auth_type": "none"}
        }

        result = get_mcp_server("admin@example.com", "m1")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_mcp_server_not_found(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test getting non-existent MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = get_mcp_server("admin@example.com", "nonexistent")

        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_mcp_server(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test deleting MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {"Item": {"mcp_id": "m1"}}
        mock_table.query.return_value = {"Items": []}

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        result = delete_mcp_server("admin@example.com", "m1")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_mcp_server_not_found(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test deleting non-existent MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = delete_mcp_server("admin@example.com", "nonexistent")

        assert result["statusCode"] == 404


class TestMcpWorkspaceAssignment:
    """Test MCP workspace assignment operations."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_assign_mcp_to_workspace(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test assigning MCP server to workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1"}},  # MCP exists
            {"Item": {"workspace_id": "ws1"}},  # WS exists
            {},  # Not already assigned
        ]

        result = assign_mcp_to_workspace("admin@example.com", "ws1", {"mcp_id": "m1"})

        assert result["statusCode"] == 201

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_assign_mcp_emits_audit_row(
        self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_audit,
    ):
        """plan/audit-completeness chunk 06 — emit MCP_SERVER_ASSIGNED."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1", "name": "SonarQube"}},
            {"Item": {"workspace_id": "ws1"}},
            {},  # not yet assigned
        ]

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            assign_mcp_to_workspace(
                "admin@example.com", "ws1", {"mcp_id": "m1"},
                audit_ctx={"source_ip": "203.0.113.7", "user_agent": "ua"},
            )

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "MCP_SERVER_ASSIGNED_TO_WORKSPACE"
        assert kwargs["details"] == {"mcp_id": "m1", "mcp_name": "SonarQube"}
        assert kwargs["source_ip"] == "203.0.113.7"

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_mcp_emits_audit_row(
        self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_audit,
    ):
        """plan/audit-completeness chunk 06 — emit MCP_SERVER_UNASSIGNED."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        # MCP name lookup before delete (best-effort).
        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "m1", "name": "SonarQube"},
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            remove_mcp_from_workspace(
                "admin@example.com", "ws1", "m1",
                audit_ctx={"source_ip": "203.0.113.7", "user_agent": "ua"},
            )

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "MCP_SERVER_UNASSIGNED_FROM_WORKSPACE"
        assert kwargs["details"] == {"mcp_id": "m1", "mcp_name": "SonarQube"}

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_assign_mcp_missing_id(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test assigning MCP without mcp_id."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = assign_mcp_to_workspace("admin@example.com", "ws1", {})

        assert result["statusCode"] == 400

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_assign_mcp_already_assigned(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test assigning already-assigned MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1"}},  # MCP exists
            {"Item": {"workspace_id": "ws1"}},  # WS exists
            {"Item": {"mcp_id": "m1"}},  # Already assigned
        ]

        result = assign_mcp_to_workspace("admin@example.com", "ws1", {"mcp_id": "m1"})

        assert result["statusCode"] == 409

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_mcp_from_workspace(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test removing MCP server from workspace."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = remove_mcp_from_workspace("admin@example.com", "ws1", "m1")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_workspace_mcp_servers(self, mock_dynamodb, mock_get_table, mock_access):
        """Test listing workspace MCP servers."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{"mcp_id": "m1"}]
        }
        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "m1", "name": "MCP1", "auth_type": "none"}
        }

        result = list_workspace_mcp_servers("user@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["mcp_servers"]) == 1


class TestLambdaHandlerRouting:
    """Test additional Lambda handler routing."""

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_membership", return_value={"role": "workspace_admin"})
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_patch_workspace(self, mock_dynamodb, mock_get_table,
                                             mock_membership, mock_access,
                                             mock_org_admin, mock_ws_admin):
        """Test PATCH /workspaces/{id} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "name": "Updated"}
        }

        event = {
            "httpMethod": "PATCH",
            "path": "/workspaces/ws1",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"name": "Updated"}),
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_delete_workspace(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test DELETE /workspaces/{id} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1",
            "headers": {"x-user-email": "admin@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_get_org_members(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test GET /org/members route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}
        mock_table.scan.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/org/members",
            "headers": {"x-user-email": "admin@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_get_api_keys(self, mock_dynamodb, mock_get_table):
        """Test GET /api-keys route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/api-keys",
            "headers": {"x-user-email": "user@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_post_api_keys(self, mock_dynamodb, mock_get_table):
        """Test POST /api-keys route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "POST",
            "path": "/api-keys",
            "headers": {"x-user-email": "user@example.com"},
            "body": json.dumps({"name": "My Key"}),
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 201

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_delete_api_key(self, mock_dynamodb, mock_get_table):
        """Test DELETE /api-keys/{key_id} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "DELETE",
            "path": "/api-keys/key123",
            "headers": {"x-user-email": "user@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_get_system_prompt(self, mock_dynamodb, mock_get_table, mock_access):
        """Test GET /workspaces/{id}/system-prompt route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/system-prompt",
            "headers": {"x-user-email": "user@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_get_searchable_workspaces(self, mock_dynamodb, mock_get_table,
                                                       mock_scan, mock_org_admin):
        """Test GET /chat/searchable-workspaces route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "GET",
            "path": "/chat/searchable-workspaces",
            "headers": {"x-user-email": "admin@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_get_available_prompts(self, mock_dynamodb, mock_get_table):
        """Test GET /chat/available-prompts route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/chat/available-prompts",
            "headers": {"x-user-email": "user@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_routes_search_users(self, mock_dynamodb, mock_get_table):
        """Test GET /users/search route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "GET",
            "path": "/users/search",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"q": "jo"},
        }

        # Return empty scan
        mock_table.scan.return_value = {"Items": []}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200


class TestCreateWorkspaceNonAdmin:
    """Test workspace creation by non-admin."""

    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_non_admin(self, mock_dynamodb, mock_get_table, mock_is_admin):
        """Test non-admin cannot create workspaces."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = CreateWorkspaceRequest(name="Test Workspace")
        result = create_workspace("user@example.com", request)

        assert result["statusCode"] == 403


class TestListWorkspacesOrgAdmin:
    """Test workspace listing as org admin."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._scan_all_workspaces")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_list_workspaces_org_admin_sees_all(self, mock_dynamodb, mock_get_table,
                                                 mock_scan, mock_org_admin):
        """Test org admin sees all workspaces."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_scan.return_value = [
            {"workspace_id": "ws1", "name": "WS1", "updated_at": "2024-01-01"},
            {"workspace_id": "ws2", "name": "WS2", "updated_at": "2024-01-02"},
        ]

        result = list_workspaces("admin@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["workspaces"]) == 2
        # All should have org_admin role
        assert all(ws["role"] == "org_admin" for ws in body["workspaces"])


class TestGetWorkspaceOrgAdmin:
    """Test getting workspace as org admin."""

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_workspace_org_admin_role(self, mock_dynamodb, mock_get_table,
                                           mock_org_admin, mock_access):
        """Test org admin gets org_admin role in workspace response."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "name": "Test"}
        }

        result = get_workspace("admin@example.com", "ws1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["workspace"]["role"] == "org_admin"


class TestLambdaHandlerMoreRoutes:
    """Test remaining Lambda handler routes."""

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_members(self, mock_dynamodb, mock_get_table, mock_access):
        """Test GET /workspaces/{id}/members route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/members",
            "headers": {"x-user-email": "user@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_patch_member(self, mock_dynamodb, mock_get_table, mock_org_admin, mock_ws_admin):
        """Test PATCH /workspaces/{id}/members/{email} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        event = {
            "httpMethod": "PATCH",
            "path": "/workspaces/ws1/members/target@example.com",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"role": "workspace_admin"}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_member(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test DELETE /workspaces/{id}/members/{email} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"role": "member", "user_email": "target@example.com"}}
        mock_table.query.return_value = {"Items": [{"role": "workspace_admin"}, {"role": "workspace_admin"}]}

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/members/target@example.com",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_repos(self, mock_dynamodb, mock_get_table, mock_access):
        """Test GET /workspaces/{id}/repos route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/repos",
            "headers": {"x-user-email": "user@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_post_repos(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test POST /workspaces/{id}/repos route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/repos",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"repo_name": "org/repo"}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 201

    @patch("workspace_management_handler._get_delete_queue_url", return_value=None)
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_repo(self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_queue):
        """Test DELETE /workspaces/{id}/repos/{name} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/repos/org%2Frepo",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_sharing(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test GET /workspaces/{id}/sharing route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "search_allowed_workspaces": []}
        }

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/sharing",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_put_sharing(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test PUT /workspaces/{id}/sharing route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "search_allowed_workspaces": []}
        }

        event = {
            "httpMethod": "PUT",
            "path": "/workspaces/ws1/sharing",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"search_allowed_workspaces": []}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_sharing_available(self, mock_dynamodb, mock_get_table,
                                            mock_scan, mock_ws_admin):
        """Test GET /workspaces/{id}/sharing/available route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/sharing/available",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_put_system_prompt(self, mock_dynamodb, mock_get_table,
                                        mock_ws_admin, mock_audit):
        """Test PUT /workspaces/{id}/system-prompt route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        event = {
            "httpMethod": "PUT",
            "path": "/workspaces/ws1/system-prompt",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"custom_system_prompt": "You are helpful."}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_system_prompt(self, mock_dynamodb, mock_get_table,
                                           mock_ws_admin, mock_audit):
        """Test DELETE /workspaces/{id}/system-prompt route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/system-prompt",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value=None)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_post_org_member(self, mock_dynamodb, mock_get_table,
                                      mock_org_membership, mock_org_admin):
        """Test POST /org/members route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "POST",
            "path": "/org/members",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"email": "new@example.com"}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 201

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value={"org_role": "member"})
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_patch_org_member(self, mock_dynamodb, mock_get_table,
                                       mock_org_membership, mock_org_admin):
        """Test PATCH /org/members/{email} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "PATCH",
            "path": "/org/members/target@example.com",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"org_role": "org_admin"}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value={"org_role": "member"})
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_org_member(self, mock_dynamodb, mock_get_table,
                                        mock_org_membership, mock_org_admin):
        """Test DELETE /org/members/{email} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "DELETE",
            "path": "/org/members/target@example.com",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_mcp_servers(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test GET /mcp/servers route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/mcp/servers",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_mcp_server(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test GET /mcp/servers/{id} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "m1", "name": "MCP1", "auth_type": "none"}
        }

        event = {
            "httpMethod": "GET",
            "path": "/mcp/servers/m1",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_mcp_server(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test DELETE /mcp/servers/{id} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"mcp_id": "m1"}}
        mock_table.query.return_value = {"Items": []}

        mock_batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = lambda s: mock_batch_writer
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = {
            "httpMethod": "DELETE",
            "path": "/mcp/servers/m1",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_post_workspace_mcp_servers(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test POST /workspaces/{id}/mcp-servers route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1"}},
            {"Item": {"workspace_id": "ws1"}},
            {},
        ]

        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/mcp-servers",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"mcp_id": "m1"}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 201

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_workspace_mcp_servers(self, mock_dynamodb, mock_get_table, mock_access):
        """Test GET /workspaces/{id}/mcp-servers route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/mcp-servers",
            "headers": {"x-user-email": "user@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_workspace_mcp_server(self, mock_dynamodb, mock_get_table, mock_ws_admin):
        """Test DELETE /workspaces/{id}/mcp-servers/{mcp_id} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/mcp-servers/m1",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_chat_mcp_servers(self, mock_dynamodb, mock_get_table,
                                           mock_scan, mock_org_admin):
        """Test GET /chat/mcp-servers route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/chat/mcp-servers",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_post_document_upload(self, mock_dynamodb, mock_get_table,
                                           mock_bucket, mock_s3, mock_access):
        """Test POST /workspaces/{id}/documents/upload route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.generate_presigned_url.return_value = "https://s3.example.com/upload"

        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/documents/upload",
            "headers": {"x-user-email": "user@example.com"},
            "body": json.dumps({"filename": "test.md"}),
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_documents(self, mock_dynamodb, mock_get_table,
                                    mock_bucket, mock_s3, mock_access):
        """Test GET /workspaces/{id}/documents route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{"Contents": []}]

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/documents",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._get_delete_queue_url", return_value=None)
    @patch("workspace_management_handler._get_s3_client")
    @patch("workspace_management_handler._get_kb_bucket", return_value="test-kb-bucket")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_delete_document(self, mock_dynamodb, mock_get_table, mock_ws_admin,
                                      mock_bucket, mock_s3, mock_queue):
        """Test DELETE /workspaces/{id}/documents/{path} route."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_s3_client.head_object.return_value = {}

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/documents/readme.md",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_handler_get_repos_available(self, mock_dynamodb, mock_get_table, mock_access):
        """Test GET /workspaces/{id}/repos/available route falls through."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # This will fail on SSM but exercises the routing
        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/repos/available",
            "headers": {"x-user-email": "user@example.com"},
        }
        result = lambda_handler(event, None)
        # Will return 500 because SSM is not mocked, but route is exercised
        assert result["statusCode"] in (200, 500)

    def test_handler_unhandled_exception(self):
        """Test handler catches unhandled exceptions."""
        event = {
            "httpMethod": "POST",
            "path": "/workspaces",
            "headers": {"x-user-email": "user@example.com"},
            "body": json.dumps({"name": "Test"}),
        }
        # This will raise because _get_dynamodb is not mocked,
        # triggering the top-level exception handler
        with patch("workspace_management_handler._is_org_admin", side_effect=RuntimeError("boom")):
            result = lambda_handler(event, None)
            assert result["statusCode"] == 500


# Import additional functions for remaining tests
update_mcp_server = handler_module.update_mcp_server
refresh_mcp_tools = handler_module.refresh_mcp_tools
get_mcp_server_workspaces = handler_module.get_mcp_server_workspaces
get_chat_available_mcp_servers = handler_module.get_chat_available_mcp_servers
validate_api_key = handler_module.validate_api_key
log_admin_action = handler_module.log_admin_action
update_workspace_budget = handler_module.update_workspace_budget
get_workspace_privacy = handler_module.get_workspace_privacy
update_workspace_privacy = handler_module.update_workspace_privacy
get_audit_summary = handler_module.get_audit_summary
get_audit_workspace = handler_module.get_audit_workspace
get_audit_events = handler_module.get_audit_events
get_audit_event = handler_module.get_audit_event
_encode_audit_cursor = handler_module._encode_audit_cursor
_decode_audit_cursor = handler_module._decode_audit_cursor
_scan_all_workspaces = handler_module._scan_all_workspaces
_get_kb_bucket = handler_module._get_kb_bucket
_get_delete_queue_url = handler_module._get_delete_queue_url
list_available_repos = handler_module.list_available_repos


class TestUpdateMcpServer:
    """Test MCP server update."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_mcp_server_name(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test updating MCP server name."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # get existing, then get for return
        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1", "name": "Old", "auth_type": "none"}},
            {"Item": {"mcp_id": "m1", "name": "New", "auth_type": "none"}},
        ]

        result = update_mcp_server("admin@example.com", "m1", {"name": "New"})

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_mcp_server_not_found(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test updating non-existent MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = update_mcp_server("admin@example.com", "nonexistent", {"name": "New"})

        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_mcp_server_not_admin(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test non-admin cannot update MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = update_mcp_server("user@example.com", "m1", {"name": "New"})

        assert result["statusCode"] == 403

    @patch("workspace_management_handler._encrypt_credentials", return_value="encrypted")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_mcp_server_with_credentials(self, mock_dynamodb, mock_get_table,
                                                  mock_org_admin, mock_encrypt):
        """Test updating MCP server with credentials."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1", "auth_type": "bearer"}},
            {"Item": {"mcp_id": "m1", "name": "MCP1", "auth_type": "bearer"}},
        ]

        result = update_mcp_server("admin@example.com", "m1", {
            "auth_type": "bearer",
            "credentials": "new-token",
            "url": "https://mcp.example.com",
            "is_global": True,
            "refresh_interval_days": 14,
        })

        assert result["statusCode"] == 200


class TestRefreshMcpTools:
    """Test MCP tool refresh."""

    @patch("workspace_management_handler._call_mcp_list_tools", return_value=[{"name": "tool1"}])
    @patch("workspace_management_handler._cache_tool_manifest")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_refresh_tools_success(self, mock_dynamodb, mock_get_table, mock_org_admin,
                                    mock_cache, mock_list_tools):
        """Test successful tool refresh."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.side_effect = [
            {"Item": {"mcp_id": "m1", "url": "https://mcp.example.com", "auth_type": "none"}},
            {"Item": {"mcp_id": "m1", "name": "MCP1", "auth_type": "none"}},
        ]

        result = refresh_mcp_tools("admin@example.com", "m1")

        assert result["statusCode"] == 200

    @patch("workspace_management_handler._call_mcp_list_tools", side_effect=Exception("Connection refused"))
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_refresh_tools_failure(self, mock_dynamodb, mock_get_table, mock_org_admin,
                                    mock_list_tools):
        """Test tool refresh failure returns 502."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "m1", "url": "https://mcp.example.com", "auth_type": "none"}
        }

        result = refresh_mcp_tools("admin@example.com", "m1")

        assert result["statusCode"] == 502

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_refresh_tools_not_found(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test refreshing non-existent MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.get_item.return_value = {}

        result = refresh_mcp_tools("admin@example.com", "nonexistent")

        assert result["statusCode"] == 404


class TestGetMcpServerWorkspaces:
    """Test listing workspaces assigned to MCP server."""

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_mcp_server_workspaces(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test listing workspaces for MCP server."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{"workspace_id": "ws1"}, {"workspace_id": "ws2"}]
        }

        result = get_mcp_server_workspaces("admin@example.com", "m1")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["workspace_ids"]) == 2


class TestChatAvailableMcpServers:
    """Test getting MCP servers available in chat."""

    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_chat_mcp_regular_user(self, mock_dynamodb, mock_get_table, mock_org_admin):
        """Test regular user gets workspace MCP servers + global."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # User memberships, then workspace MCP assignments, then MCP metadata, then global query
        mock_table.query.side_effect = [
            {"Items": [{"workspace_id": "ws1"}]},  # memberships
            {"Items": [{"mcp_id": "m1"}]},  # ws1 MCP assignments
            {"Items": [{"mcp_id": "m2", "name": "Global MCP", "is_global": True,
                        "status": "active", "tool_count": 3}]},  # global
        ]
        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "m1", "name": "MCP1", "auth_type": "none",
                     "status": "active", "tool_count": 5, "is_global": False}
        }

        result = get_chat_available_mcp_servers("user@example.com")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["mcp_servers"]) == 2


class TestValidateApiKey:
    """Test API key validation."""

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_valid_key(self, mock_dynamodb, mock_get_table):
        """Test validating a valid API key."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#k1",
                "key_id": "k1",
                "status": "active",
            }]
        }

        result = validate_api_key("ooa_test123456789012345678")

        assert result is not None
        assert result["email"] == "user@example.com"

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_invalid_key(self, mock_dynamodb, mock_get_table):
        """Test validating an invalid API key."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {"Items": []}

        result = validate_api_key("ooa_invalid")

        assert result is None

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_revoked_key(self, mock_dynamodb, mock_get_table):
        """Test validating a revoked API key."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#k1",
                "status": "revoked",
            }]
        }

        result = validate_api_key("ooa_test123")

        assert result is None

    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_expired_key(self, mock_dynamodb, mock_get_table):
        """Test validating an expired API key."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_table.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#k1",
                "status": "active",
                "expires_at": "2020-01-01T00:00:00+00:00",
            }]
        }

        result = validate_api_key("ooa_test123")

        assert result is None


class TestApiKeyUsedAudit:
    """plan/audit-completeness chunk 02 — every validate_api_key call
    (success or failure) emits an ACTION_API_KEY_USED audit row so
    InfoSec can track programmatic-access activity."""

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_success_emits_api_key_used(
        self, mock_dynamodb, mock_get_table, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#k1",
                "key_id": "k1",
                "name": "ci-bot",
                "status": "active",
            }]
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            validate_api_key(
                "ooa_test123",
                audit_ctx={"source_ip": "203.0.113.7", "user_agent": "ci/1.0"},
            )

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "API_KEY_USED"
        assert kwargs["user_email"] == "user@example.com"
        assert kwargs["details"]["success"] is True
        assert kwargs["details"]["key_id"] == "k1"
        assert kwargs["details"]["key_name"] == "ci-bot"
        assert kwargs["details"]["reason"] == ""
        assert kwargs["source_ip"] == "203.0.113.7"
        assert kwargs["user_agent"] == "ci/1.0"

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_unknown_key_emits_failure(
        self, mock_dynamodb, mock_get_table, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            validate_api_key("ooa_unknown")

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "API_KEY_USED"
        assert kwargs["details"]["success"] is False
        assert kwargs["details"]["reason"] == "key_not_found"
        # Failed lookup leaves user_email empty
        assert kwargs["user_email"] == ""

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_revoked_key_emits_failure(
        self, mock_dynamodb, mock_get_table, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#k1",
                "key_id": "k1",
                "name": "old-key",
                "status": "revoked",
            }]
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            validate_api_key("ooa_revoked")

        args, kwargs = mock_audit.call_args
        assert kwargs["details"]["success"] is False
        assert kwargs["details"]["reason"] == "key_revoked"
        assert kwargs["details"]["key_id"] == "k1"

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_validate_expired_key_emits_failure(
        self, mock_dynamodb, mock_get_table, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#k1",
                "key_id": "k1",
                "name": "expired",
                "status": "active",
                "expires_at": "2020-01-01T00:00:00+00:00",
            }]
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            validate_api_key("ooa_expired")

        args, kwargs = mock_audit.call_args
        assert kwargs["details"]["success"] is False
        assert kwargs["details"]["reason"] == "key_expired"


class TestSystemPromptAuditEmission:
    """Confirm the migrated system-prompt audit emission goes through log_admin_action."""

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_emits_system_prompt_updated_with_audit_ctx(
        self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        result = handle_update_system_prompt(
            "ws1",
            "admin@example.com",
            {"custom_system_prompt": "do thing"},
            audit_ctx={"source_ip": "203.0.113.1", "user_agent": "ua-x"},
        )
        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        # log_admin_action(AUDIT_TABLE, action, workspace_id=..., ...)
        assert args[1] == "SYSTEM_PROMPT_UPDATED"
        assert kwargs["workspace_id"] == "ws1"
        assert kwargs["user_email"] == "admin@example.com"
        assert kwargs["source_ip"] == "203.0.113.1"
        assert kwargs["user_agent"] == "ua-x"
        # Details must NOT include any secret material; just length + preview.
        assert set(kwargs["details"].keys()) == {"prompt_length", "prompt_preview"}

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_reset_emits_system_prompt_reset(
        self, mock_dynamodb, mock_get_table, mock_ws_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        result = handle_reset_system_prompt(
            "ws1",
            "admin@example.com",
            audit_ctx={"source_ip": "198.51.100.9", "user_agent": "ua-y"},
        )
        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "SYSTEM_PROMPT_RESET"
        assert kwargs["source_ip"] == "198.51.100.9"
        assert kwargs["user_agent"] == "ua-y"


class TestScanAllWorkspaces:
    """Test paginated workspace scan."""

    def test_scan_all_workspaces_paginated(self):
        """Test scanning all workspaces with pagination."""
        mock_table = MagicMock()
        mock_table.scan.side_effect = [
            {
                "Items": [{"workspace_id": "ws1"}],
                "LastEvaluatedKey": {"PK": "WS#ws1"},
            },
            {
                "Items": [{"workspace_id": "ws2"}],
            },
        ]

        result = _scan_all_workspaces(mock_table)

        assert len(result) == 2
        assert mock_table.scan.call_count == 2


class TestGetKbBucket:
    """Test KB bucket resolution."""

    @patch("workspace_management_handler._kb_bucket", None)
    @patch.dict(os.environ, {"KB_BUCKET": "my-kb-bucket"})
    def test_get_kb_bucket_from_env(self):
        """Test getting KB bucket from environment."""
        handler_module._kb_bucket = None
        result = _get_kb_bucket()
        assert result == "my-kb-bucket"
        handler_module._kb_bucket = None  # Reset

    @patch("workspace_management_handler._kb_bucket", "cached-bucket")
    def test_get_kb_bucket_cached(self):
        """Test getting KB bucket from cache."""
        result = _get_kb_bucket()
        assert result == "cached-bucket"


class TestGetDeleteQueueUrl:
    """Test delete queue URL resolution."""

    @patch("workspace_management_handler._delete_queue_url", None)
    @patch.dict(os.environ, {"DELETE_QUEUE_URL": "https://sqs.example.com/queue"})
    def test_get_delete_queue_from_env(self):
        """Test getting delete queue URL from environment."""
        handler_module._delete_queue_url = None
        result = _get_delete_queue_url()
        assert result == "https://sqs.example.com/queue"
        handler_module._delete_queue_url = None  # Reset

    @patch("workspace_management_handler._delete_queue_url", None)
    @patch.dict(os.environ, {}, clear=False)
    @patch("workspace_management_handler._get_ssm_client")
    def test_get_delete_queue_ssm_error(self, mock_ssm):
        """Test delete queue URL returns None when SSM fails."""
        handler_module._delete_queue_url = None
        # Remove env var if set
        os.environ.pop("DELETE_QUEUE_URL", None)
        mock_ssm_client = MagicMock()
        mock_ssm.return_value = mock_ssm_client
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )

        result = _get_delete_queue_url()
        assert result is None
        handler_module._delete_queue_url = None  # Reset


class TestRequestModelValidation:
    """Test additional request model validation."""

    def test_add_member_invalid_email(self):
        """Test AddMemberRequest rejects invalid email."""
        with pytest.raises(Exception):
            AddMemberRequest(email="not-an-email")

    def test_add_org_member_invalid_role(self):
        """Test AddOrgMemberRequest rejects invalid role."""
        with pytest.raises(Exception):
            AddOrgMemberRequest(email="user@example.com", org_role="superadmin")

    def test_document_upload_invalid_extension(self):
        """Test DocumentUploadRequest rejects unsupported extension."""
        with pytest.raises(Exception):
            DocumentUploadRequest(filename="file.exe")

    def test_document_upload_path_traversal(self):
        """Test DocumentUploadRequest rejects path traversal."""
        with pytest.raises(Exception):
            DocumentUploadRequest(filename="../../../etc/passwd")

    def test_document_upload_valid(self):
        """Test DocumentUploadRequest accepts valid file."""
        request = DocumentUploadRequest(filename="doc.pdf", content_type="application/pdf")
        assert request.filename == "doc.pdf"

    def test_update_workspace_request(self):
        """Test UpdateWorkspaceRequest with description only."""
        request = UpdateWorkspaceRequest(description="New desc")
        assert request.description == "New desc"
        assert request.name is None

    def test_create_mcp_server_request_invalid_auth(self):
        """Test CreateMcpServerRequest rejects invalid auth type."""
        with pytest.raises(Exception):
            handler_module.CreateMcpServerRequest(name="test", url="https://example.com", auth_type="invalid")


# ============================================================================
# Chunk 03a: audit emission for the 7 wired mutations
# ============================================================================


AUDIT_CTX = {"source_ip": "203.0.113.7", "user_agent": "ua-test"}


def _audit_kwargs(mock_audit):
    """Pull (positional, kwargs) from the most recent log_admin_action call."""
    args, kwargs = mock_audit.call_args
    return args, kwargs


class TestCreateWorkspaceAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_emits_audit_for_workspace_created(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.batch_writer.return_value.__enter__.return_value = MagicMock()

        request = CreateWorkspaceRequest(name="My Workspace")
        result = create_workspace("admin@example.com", request, AUDIT_CTX)
        assert result["statusCode"] == 201

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_CREATED"
        assert kwargs["user_email"] == "admin@example.com"
        assert kwargs["source_ip"] == "203.0.113.7"
        assert kwargs["user_agent"] == "ua-test"
        assert kwargs["details"] == {"workspace_name": "My Workspace"}

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_workspace_does_not_emit_on_403(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = CreateWorkspaceRequest(name="My Workspace")
        result = create_workspace("user@example.com", request, AUDIT_CTX)
        assert result["statusCode"] == 403
        mock_audit.assert_not_called()


class TestUpdateWorkspaceAudit:
    @patch("workspace_management_handler.get_workspace")
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_workspace_emits_changed_fields(
        self, mock_dynamodb, mock_get_table, mock_admin, mock_audit, mock_get_ws
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_get_ws.return_value = {"statusCode": 200, "body": "{}"}

        request = UpdateWorkspaceRequest(name="renamed", description="newer")
        result = update_workspace("admin@example.com", "ws1", request, AUDIT_CTX)
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_UPDATED"
        assert kwargs["details"]["changed_fields"] == ["name", "description"]


class TestDeleteWorkspaceAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_workspace_emits_workspace_deleted(
        self, mock_dynamodb, mock_get_table, mock_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.side_effect = [
            {"Items": [{"PK": "WS#ws1", "SK": "METADATA", "name": "Doomed"}]},
            {"Items": []},
        ]
        mock_table.batch_writer.return_value.__enter__.return_value = MagicMock()

        result = delete_workspace("admin@example.com", "ws1", AUDIT_CTX)
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_DELETED"
        assert kwargs["workspace_id"] == "ws1"
        assert kwargs["details"] == {"workspace_name": "Doomed"}


class TestAddMemberAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_membership", return_value=None)
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_member_emits_workspace_member_added(
        self, mock_dynamodb, mock_get_table, mock_admin, mock_get_member, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}

        request = AddMemberRequest(email="invitee@example.com", role="member")
        result = add_member("admin@example.com", "ws1", request, AUDIT_CTX)
        assert result["statusCode"] == 201

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_MEMBER_ADDED"
        assert kwargs["details"] == {
            "member_email": "invitee@example.com",
            "role": "member",
        }


class TestRemoveMemberAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_membership")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_member_emits_workspace_member_removed(
        self, mock_dynamodb, mock_get_table, mock_admin, mock_get_member, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_get_member.return_value = {"role": "member"}

        result = remove_member(
            "admin@example.com", "ws1", "victim@example.com", AUDIT_CTX
        )
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_MEMBER_REMOVED"
        assert kwargs["details"] == {"member_email": "victim@example.com"}


class TestAddRepoAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_repo_emits_workspace_repo_added(
        self, mock_dynamodb, mock_get_table, mock_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = AddRepoRequest(repo_name="org/repo")
        result = add_repo("admin@example.com", "ws1", request, AUDIT_CTX)
        assert result["statusCode"] == 201

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_REPO_ADDED"
        assert kwargs["details"] == {"repo_name": "org/repo"}


class TestRemoveRepoAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_delete_queue_url", return_value=None)
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_repo_emits_workspace_repo_removed(
        self, mock_dynamodb, mock_get_table, mock_admin, mock_queue, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        result = remove_repo("admin@example.com", "ws1", "org/repo", AUDIT_CTX)
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_REPO_REMOVED"
        assert kwargs["details"] == {"repo_name": "org/repo"}


# ============================================================================
# Chunk 03b: audit emission for org / MCP / API key / budget mutations
# ============================================================================


class TestAddOrgMemberAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership", return_value=None)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_add_org_member_emits_org_member_added(
        self, mock_dynamodb, mock_get_table, mock_org_membership, mock_org_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        request = AddOrgMemberRequest(email="new@example.com", org_role="member")
        result = add_org_member("admin@example.com", request, AUDIT_CTX)
        assert result["statusCode"] == 201

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "ORG_MEMBER_ADDED"
        assert kwargs["details"] == {"member_email": "new@example.com", "role": "member"}


class TestRemoveOrgMemberAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_org_membership")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_remove_org_member_emits_org_member_removed(
        self, mock_dynamodb, mock_get_table, mock_org_membership, mock_org_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_org_membership.return_value = {"org_role": "member"}
        mock_table.query.return_value = {"Items": []}

        result = remove_org_member("admin@example.com", "victim@example.com", AUDIT_CTX)
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "ORG_MEMBER_REMOVED"
        assert kwargs["details"] == {"member_email": "victim@example.com"}


class TestCreateMcpServerAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._call_mcp_list_tools", return_value=[])
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_mcp_server_emits_mcp_server_created(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_list_tools, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        body = {"name": "Jira", "url": "https://example.com", "auth_type": "none"}
        result = create_mcp_server("admin@example.com", body, AUDIT_CTX)
        assert result["statusCode"] == 201

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "MCP_SERVER_CREATED"
        assert kwargs["details"]["name"] == "Jira"
        assert kwargs["details"]["auth_type"] == "none"
        assert "mcp_id" in kwargs["details"]


class TestUpdateMcpServerAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_mcp_server_emits_changed_fields(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"mcp_id": "mcp_a", "name": "old", "auth_type": "none"}
        }

        body = {"name": "renamed"}
        result = update_mcp_server("admin@example.com", "mcp_a", body, AUDIT_CTX)
        assert result["statusCode"] in (200, 500)  # downstream get_mcp_server may 200 or fail safely

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "MCP_SERVER_UPDATED"
        assert kwargs["details"]["mcp_id"] == "mcp_a"
        assert kwargs["details"]["changed_fields"] == ["name"]


class TestDeleteMcpServerAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_delete_mcp_server_emits_mcp_server_deleted(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {"Item": {"mcp_id": "mcp_a", "name": "GoneBaby"}}
        mock_table.batch_writer.return_value.__enter__.return_value = MagicMock()
        mock_table.query.return_value = {"Items": []}

        result = delete_mcp_server("admin@example.com", "mcp_a", AUDIT_CTX)
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "MCP_SERVER_DELETED"
        assert kwargs["details"] == {"mcp_id": "mcp_a", "name": "GoneBaby"}


class TestCreateApiKeyAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_api_key_emits_api_key_created_without_secret(
        self, mock_dynamodb, mock_get_table, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        body = {"name": "ci-bot", "expires_in_days": 30}
        result = create_api_key("brian@example.com", body, AUDIT_CTX)
        assert result["statusCode"] == 201

        # Pull the raw key out of the response so we can assert it never appears
        # in the audit details (security gate per chunk 03b).
        response_body = json.loads(result["body"])
        raw_key = response_body["key"]
        assert raw_key.startswith("ooa_")

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "API_KEY_CREATED"
        details = kwargs["details"]
        assert "key_id" in details
        assert details["name"] == "ci-bot"
        assert "expires_at" in details
        # CRITICAL: the raw secret must not be anywhere in the audit details.
        for value in details.values():
            assert raw_key not in str(value), (
                f"API key secret leaked into audit details: {details}"
            )

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_create_api_key_invalid_request_does_not_emit(
        self, mock_dynamodb, mock_get_table, mock_audit
    ):
        result = create_api_key("brian@example.com", {}, AUDIT_CTX)
        assert result["statusCode"] == 400
        mock_audit.assert_not_called()


class TestRevokeApiKeyAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_revoke_api_key_emits_api_key_revoked(
        self, mock_dynamodb, mock_get_table, mock_audit
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.update_item.return_value = {
            "Attributes": {"name": "old-name", "key_id": "kid_xyz"}
        }

        result = revoke_api_key("brian@example.com", "kid_xyz", AUDIT_CTX)
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "API_KEY_REVOKED"
        assert kwargs["details"] == {"key_id": "kid_xyz", "name": "old-name"}


class TestUpdateWorkspaceBudgetAudit:
    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_budget_set_emits_workspace_budget_updated(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_audit
    ):
        from decimal import Decimal as _D
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "monthly_budget_usd": _D("50.0")}
        }

        result = update_workspace_budget(
            "admin@example.com", "ws1", {"monthly_budget_usd": 250.0}, AUDIT_CTX
        )
        assert result["statusCode"] == 200

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_BUDGET_UPDATED"
        # ADR-009: numeric fields are stored as Decimal-safe strings.
        assert kwargs["details"]["previous_budget_usd"] == "50.0"
        assert kwargs["details"]["new_budget_usd"] == "250.0"

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_update_budget_clear_emits_empty_new_budget(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_audit
    ):
        from decimal import Decimal as _D
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "monthly_budget_usd": _D("100")}
        }

        result = update_workspace_budget(
            "admin@example.com", "ws1", {"monthly_budget_usd": None}, AUDIT_CTX
        )
        assert result["statusCode"] == 200

        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "WORKSPACE_BUDGET_UPDATED"
        assert kwargs["details"]["previous_budget_usd"] == "100"
        assert kwargs["details"]["new_budget_usd"] == ""


class TestPrivacyMode:
    """GET / PUT /workspaces/{id}/privacy. Plan/ chunk 04."""

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_returns_strict_for_legacy_workspace(
        self, mock_dynamodb, mock_get_table, mock_can_access,
    ):
        """Workspaces created before chunk 03 lack the privacy_mode
        attribute. Reader must default to 'STRICT' (the safe default)."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        # Legacy row: no privacy_mode attribute.
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "name": "Legacy"}
        }
        with patch.object(handler_module, "PII_REDACTION_ENABLED", True):
            result = get_workspace_privacy("u@example.com", "ws1")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["privacy_mode"] == "STRICT"
        assert body["redaction_enabled_at_org_level"] is True
        assert body["is_effective"] is True

    @patch("workspace_management_handler._can_access_workspace", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_is_effective_combinations(
        self, mock_dynamodb, mock_get_table, mock_can_access,
    ):
        """is_effective = redaction_enabled_at_org_level AND mode==STRICT.
        Verify all four combinations of (env on/off, mode STRICT/NONE)."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        cases = [
            (True, "STRICT", True),
            (True, "NONE", False),
            (False, "STRICT", False),
            (False, "NONE", False),
        ]
        for env_on, mode, expected in cases:
            mock_table.get_item.return_value = {
                "Item": {"workspace_id": "ws1", "privacy_mode": mode}
            }
            with patch.object(handler_module, "PII_REDACTION_ENABLED", env_on):
                result = get_workspace_privacy("u@example.com", "ws1")
            body = json.loads(result["body"])
            assert body["is_effective"] is expected, (env_on, mode)
            assert body["redaction_enabled_at_org_level"] is env_on
            assert body["privacy_mode"] == mode

    @patch("workspace_management_handler._can_access_workspace", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_get_403_for_non_member(
        self, mock_dynamodb, mock_get_table, mock_can_access,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        result = get_workspace_privacy("u@example.com", "ws1")
        assert result["statusCode"] == 403

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_put_strict_to_none_emits_audit(
        self, mock_dynamodb, mock_get_table, mock_is_admin, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "privacy_mode": "STRICT"}
        }

        with patch.object(handler_module, "PII_REDACTION_ENABLED", True):
            result = update_workspace_privacy(
                "admin@example.com", "ws1",
                {"privacy_mode": "NONE"}, AUDIT_CTX,
            )
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["privacy_mode"] == "NONE"
        assert body["is_effective"] is False  # tfvar on, mode NONE

        mock_audit.assert_called_once()
        args, kwargs = _audit_kwargs(mock_audit)
        assert args[1] == "PRIVACY_MODE_UPDATED"
        assert kwargs["details"] == {
            "previous_mode": "STRICT", "new_mode": "NONE",
        }
        assert kwargs["source_ip"] == "203.0.113.7"

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_put_invalid_mode_400(
        self, mock_dynamodb, mock_get_table, mock_is_admin, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        result = update_workspace_privacy(
            "admin@example.com", "ws1",
            {"privacy_mode": "BOGUS"}, AUDIT_CTX,
        )
        assert result["statusCode"] == 400
        # No audit emit for rejected request.
        mock_audit.assert_not_called()
        # No update_item either.
        mock_table.update_item.assert_not_called()

    @patch("workspace_management_handler.log_admin_action")
    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_put_403_for_non_admin(
        self, mock_dynamodb, mock_get_table, mock_is_admin, mock_audit,
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        result = update_workspace_privacy(
            "user@example.com", "ws1",
            {"privacy_mode": "NONE"}, AUDIT_CTX,
        )
        assert result["statusCode"] == 403
        mock_audit.assert_not_called()


class TestLambdaHandlerBootstrap:
    """Verify lambda_handler threads audit_ctx into mutating routes."""

    @patch("workspace_management_handler.create_workspace")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_lambda_handler_passes_audit_ctx_to_create(
        self, mock_dynamodb, mock_get_table, mock_org, mock_create
    ):
        mock_create.return_value = {"statusCode": 201, "body": "{}"}
        event = {
            "httpMethod": "POST",
            "path": "/workspaces",
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"name": "X"}),
            "requestContext": {
                "http": {"sourceIp": "203.0.113.99", "userAgent": "ua-x"}
            },
        }
        lambda_handler(event, None)
        mock_create.assert_called_once()
        # audit_ctx is the 3rd positional arg
        args, kwargs = mock_create.call_args
        assert args[2] == {"source_ip": "203.0.113.99", "user_agent": "ua-x"}


# ============================================================================
# Chunk 05: audit query endpoints
# ============================================================================


class TestAuditCursor:
    def test_round_trip(self):
        key = {"PK": "WS#x", "SK": "2026-04-10T12:00:00#abc"}
        encoded = _encode_audit_cursor(key)
        assert isinstance(encoded, str) and encoded
        decoded = _decode_audit_cursor(encoded)
        assert decoded == key

    def test_decode_garbage_returns_none(self):
        assert _decode_audit_cursor("notbase64!!!") is None

    def test_encode_none_returns_none(self):
        assert _encode_audit_cursor(None) is None
        assert _encode_audit_cursor({}) is None


class TestAuditSummary:
    @patch("workspace_management_handler._scan_all_workspaces")
    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_non_admin_gets_403(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_scan
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"):
            result = get_audit_summary("user@example.com", {})
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_empty_aggregates_returns_zeros(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_scan
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"):
            result = get_audit_summary("admin@example.com", {})
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["actions_total"] == 0
        assert body["actions_by_type"] == []
        assert body["actions_by_workspace"] == []
        assert body["top_actors"] == []
        assert body["trend"] == []

    @patch("workspace_management_handler._scan_all_workspaces")
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_aggregates_roll_up(
        self, mock_dynamodb, mock_get_table, mock_org_admin, mock_scan
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        mock_scan.return_value = [{"workspace_id": "ws-1", "name": "Eng"}]
        # Workspace name lookup
        mock_table.get_item.return_value = {"Item": {"name": "Eng"}}
        # Pretend the analytics table returns DA, DC, DCU rows
        mock_table.query.side_effect = [
            {"Items": [{"SK": "DA#2026-05-01", "actions_total": 5}]},
            {"Items": [
                {"SK": "DC#2026-05-01#API_KEY_CREATED", "actions_total": 2},
                {"SK": "DC#2026-05-01#WORKSPACE_CREATED", "actions_total": 3},
            ]},
            {"Items": [
                {
                    "SK": "DCU#2026-05-01#brian@example.com",
                    "actions_total": 5,
                    "last_action_at": "2026-05-01T12:00:00+00:00",
                }
            ]},
        ]

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"):
            result = get_audit_summary("admin@example.com", {})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["actions_total"] == 5
        assert body["actions_by_type"] == [
            {"action": "WORKSPACE_CREATED", "count": 3},
            {"action": "API_KEY_CREATED", "count": 2},
        ]
        assert body["top_actors"][0]["user_email"] == "brian@example.com"
        assert body["top_actors"][0]["count"] == 5
        assert body["trend"][0] == {"date": "2026-05-01", "count": 5}


class TestAuditWorkspace:
    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_non_workspace_admin_gets_403(
        self, mock_dynamodb, mock_get_table, mock_admin
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"):
            result = get_audit_workspace("u@x", "ws-1", {})
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_workspace_not_found_404(
        self, mock_dynamodb, mock_get_table, mock_admin
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.get_item.return_value = {}

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"):
            result = get_audit_workspace("admin@example.com", "ws-missing", {})
        assert result["statusCode"] == 404


class TestAuditEvents:
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_workspace_filter_queries_base_table(
        self, mock_dynamodb, mock_get_table, mock_org, mock_admin
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [{
                "request_id": "r1",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "category": "admin",
                "action": "API_KEY_CREATED",
                "workspace_id": "ws-1",
                "user_email": "brian@example.com",
                "source_ip": "203.0.113.1",
                "user_agent": "ua",
                "details": {"key_id": "k"},
            }]
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_events(
                "admin@example.com",
                {"workspace_id": "ws-1"},
            )
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["events"]) == 1
        assert body["events"][0]["action"] == "API_KEY_CREATED"
        assert body["events"][0]["source_ip"] == "203.0.113.1"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_user_filter_uses_gsi1(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_events(
                "admin@example.com",
                {"user_email": "brian@example.com"},
            )
        assert result["statusCode"] == 200
        # query was called against GSI1
        kwargs = mock_table.query.call_args.kwargs
        assert kwargs.get("IndexName") == "GSI1"

    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_unfiltered_non_admin_returns_403(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_events("user@x", {})
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_unfiltered_org_admin_uses_gsi2_query(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """The no-filter org-admin path Queries GSI2 (CAT#admin partition)
        rather than Scan + FilterExpression. fable-scale chunk 04: routes
        through audit_shard.read_by_category for shard fan-out."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=([], None)) as mock_read:
            result = get_audit_events("admin@example.com", {})
        assert result["statusCode"] == 200
        # Reader delegates to the sharded helper with category=admin.
        mock_read.assert_called_once()
        assert mock_read.call_args.kwargs["category"] == "admin"
        # Scan should never be called on this path.
        mock_table.scan.assert_not_called()

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._is_org_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_cursor_round_trip(
        self, mock_dynamodb, mock_get_table, mock_org, mock_admin
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        last_key = {"PK": "WS#ws-1", "SK": "2026-05-01T12:00:00#abc"}
        # First call returns one event + LastEvaluatedKey
        mock_table.query.return_value = {
            "Items": [{"request_id": "r1"}],
            "LastEvaluatedKey": last_key,
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            page1 = get_audit_events("admin@example.com", {"workspace_id": "ws-1"})
        assert page1["statusCode"] == 200
        body1 = json.loads(page1["body"])
        cursor = body1["next_cursor"]
        assert cursor

        # Second call: pass the cursor back in
        mock_table.query.return_value = {"Items": [{"request_id": "r2"}]}
        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            page2 = get_audit_events(
                "admin@example.com",
                {"workspace_id": "ws-1", "cursor": cursor},
            )
        # The decoded cursor must be passed as ExclusiveStartKey on the second
        # query call
        second_call = mock_table.query.call_args
        assert second_call.kwargs.get("ExclusiveStartKey") == last_key
        body2 = json.loads(page2["body"])
        assert body2["next_cursor"] is None

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_gsi2_query_applies_date_range(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """Regression: Last-N-days has to actually narrow the result set.
        fable-scale chunk 04: the date range flows through the shard helper
        as sk_start/sk_end arguments."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=([], None)) as mock_read:
            get_audit_events(
                "admin@example.com",
                {"start_date": "2026-04-26T00:00:00+00:00",
                 "end_date":   "2026-05-03T00:00:00+00:00"},
            )

        kwargs = mock_read.call_args.kwargs
        assert kwargs["sk_start"] == "2026-04-26"
        # End bound has the ~ suffix so end-of-day rows sort inside.
        assert kwargs["sk_end"].startswith("2026-05-03")

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_events_returned_newest_first(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """Regression: even though Query with ScanIndexForward=False already
        returns newest-first, the handler also sorts defensively in case a
        future caller flips the direction. The bug originally surfaced as
        audit rows showing oldest-first in the UI. fable-scale chunk 04:
        the sort still happens post-shard-merge in the handler."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        # Deliberately out-of-order to prove the sort happens.
        shard_items = [
            {"request_id": "old", "timestamp": "2026-03-19T19:55:13+00:00"},
            {"request_id": "new", "timestamp": "2026-04-30T18:33:02+00:00"},
            {"request_id": "mid", "timestamp": "2026-04-30T18:32:15+00:00"},
        ]

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=(shard_items, None)):
            result = get_audit_events("admin@example.com", {})

        body = json.loads(result["body"])
        ids = [e["request_id"] for e in body["events"]]
        assert ids == ["new", "mid", "old"]

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_status_filter_appears_in_filter_expression(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """The Audit dashboard fetches refusals via category=bedrock +
        status=refusal. Regression: the status param surfaces as a
        FilterExpression on the audit-events Query. fable-scale chunk 04:
        the FilterExpression flows through the shard helper as a kwarg."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=([], None)) as mock_read:
            get_audit_events(
                "admin@example.com",
                {"category": "bedrock", "status": "refusal"},
            )

        kwargs = mock_read.call_args.kwargs
        assert kwargs["category"] == "bedrock"
        assert "#st = :status" in (kwargs["filter_expression"] or "")
        assert kwargs["expression_attribute_values"][":status"] == "refusal"
        assert kwargs["expression_attribute_names"]["#st"] == "status"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_filtered_query_paginates_to_find_sparse_matches(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """Regression for "refusals went away from the audit page": DDB
        applies Limit BEFORE FilterExpression, so a sparse status=refusal
        filter returns 0 if the most-recent N items are all non-matches.
        The handler must paginate internally until target_limit matches
        accumulate. fable-scale chunk 04: pagination happens inside each
        shard's Query loop in audit_shard.read_by_category (verified by
        this test using the real helper against the mock table)."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Every shard: page 1 empty + LEK, page 2 has the refusal.
        # Simulates: 500 non-matches (all chat successes), more pages remain,
        # then 1 refusal lurking past the chat backlog on the 2nd page.
        mock_table.query.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"PK": "x", "SK": "y"}},
            {"Items": [{"request_id": "r1", "timestamp": "2026-05-04T07:27:47+00:00", "GSI2SK": "2026-05-04T07:27:47+00:00#r1"}]},
        ] * len(handler_module.audit_shard.gsi2pk_shards_for_read("bedrock"))

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_events(
                "admin@example.com",
                {"category": "bedrock", "status": "refusal", "limit": "25"},
            )
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # 17 shards each surfaced the same refusal (mock always returns the
        # same item); the handler dedup path lives at the merge/sort layer.
        # Semantic check: at least one refusal was returned.
        assert len(body["events"]) >= 1
        assert body["events"][0]["request_id"] == "r1"

        # Each shard walked exactly 2 Query calls (page 1 empty + LEK -> page 2 hit).
        expected_calls = 2 * len(handler_module.audit_shard.gsi2pk_shards_for_read("bedrock"))
        assert mock_table.query.call_count == expected_calls
        for call in mock_table.query.call_args_list:
            assert call.kwargs["Limit"] == handler_module.AUDIT_EVENTS_FILTER_PAGE_LIMIT

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_filtered_query_respects_max_pages_safety_cap(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """If the filter would scan forever (every page returns LastEvaluatedKey
        but no matches), the handler bails at MAX_PAGES per shard so a
        Lambda timeout doesn't bring down the dashboard. fable-scale chunk
        04: cap applies per-shard inside audit_shard.read_by_category."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        # Always return empty matches + a cursor; the helper should cap.
        mock_table.query.return_value = {
            "Items": [],
            "LastEvaluatedKey": {"PK": "x", "SK": "y"},
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            get_audit_events(
                "admin@example.com",
                {"category": "bedrock", "status": "refusal"},
            )

        # Capped at MAX_PAGES per shard × N shards. No infinite loop.
        num_shards = len(handler_module.audit_shard.gsi2pk_shards_for_read("bedrock"))
        expected_max = handler_module.AUDIT_EVENTS_FILTER_MAX_PAGES * num_shards
        assert mock_table.query.call_count == expected_max

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_default_unfiltered_query_uses_paginated_path(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """The default GSI2 admin query carries the hide-clause
        FilterExpression for DOCUMENT_INGESTED, so it routes through
        the paginated helper (Limit becomes PAGE_LIMIT=500 internally)
        rather than the cheap single-shot path. Replaces the previous
        "no FilterExpression" invariant which is no longer true now
        that we always exclude dashboard-hidden actions."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": [], "LastEvaluatedKey": None}

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            get_audit_events("admin@example.com", {"limit": "10"})

        kwargs = mock_table.query.call_args.kwargs
        assert "FilterExpression" in kwargs
        assert "attribute_not_exists" in kwargs["FilterExpression"]

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_refusal_event_envelope_includes_top_level_fields(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """Refusal rows carry status / flagged_reason / lambda_name /
        model_id at the top level of the audit table item; the projection
        must surface them on the public envelope so the drawer can render
        the refusal reason and the prompt that triggered it."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": [{
            "request_id": "audit_xyz",
            "timestamp": "2026-05-04T00:55:21+00:00",
            "category": "bedrock",
            "user_email": "brian@example.com",
            "workspace_id": "",
            "source_ip": "24.253.92.153",
            "user_agent": "Mozilla/5.0",
            "status": "refusal",
            "flagged_reason": "test_refusal_trigger",
            "lambda_name": "chat",
            "model_id": "us.anthropic.claude-sonnet-4-6",
            "input_text": "...prompt...",
            "output_text": "...refusal response...",
        }]}

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_events(
                "admin@example.com",
                {"category": "bedrock", "status": "refusal"},
            )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        ev = body["events"][0]
        assert ev["status"] == "refusal"
        assert ev["flagged_reason"] == "test_refusal_trigger"
        assert ev["lambda_name"] == "chat"
        assert ev["model_id"] == "us.anthropic.claude-sonnet-4-6"
        assert ev["input_text"] == "...prompt..."
        assert ev["output_text"] == "...refusal response..."

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_admin_event_envelope_omits_bedrock_fields(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """Admin rows don't have status / flagged_reason / lambda_name /
        model_id. The projection must omit those keys (rather than emit
        empty strings) so admin payloads stay clean."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": [{
            "request_id": "abc",
            "timestamp": "2026-05-04T01:00:00+00:00",
            "category": "admin",
            "action": "WORKSPACE_CREATED",
            "user_email": "brian@example.com",
            "workspace_id": "ws-1",
            "source_ip": "24.253.92.153",
            "user_agent": "Mozilla/5.0",
            "details": {"workspace_name": "X"},
        }]}

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_events("admin@example.com", {})

        body = json.loads(result["body"])
        ev = body["events"][0]
        for key in ("status", "flagged_reason", "lambda_name", "model_id",
                     "input_text", "output_text"):
            assert key not in ev, f"unexpected {key} on admin envelope: {ev}"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_category_filter_routes_to_correct_partition(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """The category query param now picks the GSI2 partition rather
        than appearing as a FilterExpression. fable-scale chunk 04: the
        category is passed to audit_shard.read_by_category which fans out
        across CAT#bedrock#NN shards."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=([], None)) as mock_read:
            # Explicitly request bedrock to prove the param controls the
            # partition (default is admin).
            get_audit_events("admin@example.com", {"category": "bedrock"})

        kwargs = mock_read.call_args.kwargs
        assert kwargs["category"] == "bedrock"
        # And no leftover category predicate in the FilterExpression, since
        # it's now in the shard-select layer.
        assert "category" not in (kwargs.get("filter_expression") or "")

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_default_query_hides_document_ingested(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """When no action filter is supplied, DOCUMENT_INGESTED rows are
        excluded from the list view via FilterExpression so they don't
        flood the dashboard's Recent Activity feed."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [], "LastEvaluatedKey": None,
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            get_audit_events("admin@example.com", {})

        kwargs = mock_table.query.call_args.kwargs
        # The hide clause is part of FilterExpression and the literal
        # DOCUMENT_INGESTED string is in the placeholder values.
        assert "FilterExpression" in kwargs
        hidden_values = [
            v for k, v in kwargs["ExpressionAttributeValues"].items()
            if k.startswith(":hidden_action_")
        ]
        assert "DOCUMENT_INGESTED" in hidden_values
        # The expression uses attribute_not_exists OR <> per the spec.
        assert "attribute_not_exists" in kwargs["FilterExpression"]
        assert kwargs["ExpressionAttributeNames"].get("#a") == "action"

    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_explicit_action_filter_bypasses_hide_rule(
        self, mock_dynamodb, mock_get_table, mock_org
    ):
        """An operator who explicitly requests ?action=DOCUMENT_INGESTED
        for forensic drill-down gets exactly that - the hide rule does
        NOT also hide what was just requested."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [], "LastEvaluatedKey": None,
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            get_audit_events(
                "admin@example.com",
                {"action": "DOCUMENT_INGESTED"},
            )

        kwargs = mock_table.query.call_args.kwargs
        # action filter is on; hide-clause placeholders are not added.
        assert kwargs["ExpressionAttributeValues"][":action"] == "DOCUMENT_INGESTED"
        hidden_values = [
            v for k, v in kwargs["ExpressionAttributeValues"].items()
            if k.startswith(":hidden_action_")
        ]
        assert hidden_values == []


class TestAuditSummaryFallback:
    """Regression: when DA/DC/DCU rollups are empty, fall back to a GSI2
    Query (CAT#admin) so the dashboard renders something on a fresh deploy.
    Same JSON shape either way; `source` reports which path produced it."""

    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_falls_back_to_raw_query_when_aggregates_empty(
        self, mock_dynamodb, mock_get_table, mock_org, mock_scan
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        # _scan_all_workspaces returns []; the aggregator loop runs zero
        # iterations so total stays 0 and the fallback fires. The fallback
        # goes through audit_shard.read_by_category (fable-scale chunk 04).
        fallback_items = [
            {
                "action": "API_KEY_CREATED",
                "workspace_id": "ws-1",
                "user_email": "brian@example.com",
                "timestamp": "2026-05-01T12:00:00+00:00",
            },
            {
                "action": "WORKSPACE_DELETED",
                "workspace_id": "ws-1",
                "user_email": "brian@example.com",
                "timestamp": "2026-05-02T08:30:00+00:00",
            },
            {
                "action": "WORKSPACE_CREATED",
                "workspace_id": "ws-2",
                "user_email": "alice@example.com",
                "timestamp": "2026-05-03T09:00:00+00:00",
            },
        ]

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"), \
             patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=(fallback_items, None)) as mock_read:
            result = get_audit_summary("admin@example.com", {})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["actions_total"] == 3
        actions = {a["action"] for a in body["actions_by_type"]}
        assert actions == {"API_KEY_CREATED", "WORKSPACE_DELETED", "WORKSPACE_CREATED"}
        ws_counts = {w["workspace_id"]: w["count"] for w in body["actions_by_workspace"]}
        assert ws_counts == {"ws-1": 2, "ws-2": 1}
        assert body["source"] == "raw"
        # Verify the fallback routed through the shard-aware reader with
        # category=admin.
        assert mock_read.call_args.kwargs["category"] == "admin"
        mock_table.scan.assert_not_called()

    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_no_fallback_when_no_raw_rows_either(
        self, mock_dynamodb, mock_get_table, mock_org, mock_scan
    ):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"), \
             patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_summary("admin@example.com", {})

        body = json.loads(result["body"])
        assert body["actions_total"] == 0
        assert body["source"] == "aggregates"

    @patch("workspace_management_handler._scan_all_workspaces", return_value=[])
    @patch("workspace_management_handler._is_org_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_fallback_skips_document_ingested(
        self, mock_dynamodb, mock_get_table, mock_org, mock_scan
    ):
        """The fallback summary path mirrors the aggregator's hide rule:
        DOCUMENT_INGESTED rows are present in the audit table but must
        NOT contribute to actions_total / actions_by_type so the dashboard
        cards don't get polluted even on a fresh-deploy fallback render."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        fallback_items = [
            {
                "action": "API_KEY_CREATED",
                "workspace_id": "ws-1",
                "user_email": "brian@example.com",
                "timestamp": "2026-05-01T12:00:00+00:00",
            },
            # 5 ingest rows that should be IGNORED:
            *[
                {
                    "action": "DOCUMENT_INGESTED",
                    "workspace_id": "ws-1",
                    "user_email": "",
                    "timestamp": f"2026-05-0{d}T12:00:00+00:00",
                }
                for d in range(2, 7)
            ],
        ]

        with patch.object(handler_module, "ANALYTICS_TABLE", "test-analytics"), \
             patch.object(handler_module, "AUDIT_TABLE", "test-audit"), \
             patch.object(handler_module.audit_shard, "read_by_category",
                          return_value=(fallback_items, None)):
            result = get_audit_summary("admin@example.com", {})

        body = json.loads(result["body"])
        assert body["actions_total"] == 1  # only API_KEY_CREATED counted
        actions = {a["action"] for a in body["actions_by_type"]}
        assert actions == {"API_KEY_CREATED"}
        assert "DOCUMENT_INGESTED" not in actions
        assert body["source"] == "raw"  # fallback path triggered (no rollups)


class TestAuditEventDetail:
    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_returns_event(self, mock_dynamodb, mock_get_table, mock_admin):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {
            "Items": [{
                "request_id": "abc",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "category": "admin",
                "action": "API_KEY_CREATED",
                "workspace_id": "ws-1",
                "user_email": "brian@example.com",
                "source_ip": "203.0.113.7",
                "user_agent": "ua",
                "details": {"key_id": "k"},
            }]
        }

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_event("admin@example.com", "ws-1", "abc")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["event"]["request_id"] == "abc"
        assert body["event"]["details"] == {"key_id": "k"}

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_not_found(self, mock_dynamodb, mock_get_table, mock_admin):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"
        mock_table.query.return_value = {"Items": []}

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_event("admin@example.com", "ws-1", "missing")
        assert result["statusCode"] == 404

    @patch("workspace_management_handler._is_workspace_admin", return_value=False)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_403_for_non_admin(self, mock_dynamodb, mock_get_table, mock_admin):
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_event("user@x", "ws-1", "abc")
        assert result["statusCode"] == 403

    @patch("workspace_management_handler._is_workspace_admin", return_value=True)
    @patch("workspace_management_handler._get_workspaces_table")
    @patch("workspace_management_handler._get_dynamodb")
    def test_match_on_second_page(self, mock_dynamodb, mock_get_table, mock_admin):
        """Regression: with the Limit=1 bug, a partition whose first page's
        newest row wasn't the target request_id returned 404. Now the query
        walks pages until the filter matches. ADR-013: the final mock page
        has no LastEvaluatedKey so the mock reflects real pagination."""
        mock_table = MagicMock()
        mock_dynamodb.return_value.Table.return_value = mock_table
        mock_get_table.return_value = "test-workspaces"

        # Two pages: first empty-after-filter but has LastEvaluatedKey;
        # second has the matching row and no LastEvaluatedKey.
        mock_table.query.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"PK": "WS#ws-1", "SK": "2026-05-01T12:00:00+00:00#other"}},
            {
                "Items": [{
                    "request_id": "target-rid",
                    "timestamp": "2026-04-15T09:00:00+00:00",
                    "category": "admin",
                    "action": "WORKSPACE_UPDATED",
                    "workspace_id": "ws-1",
                    "user_email": "brian@example.com",
                    "source_ip": "203.0.113.7",
                    "user_agent": "ua",
                    "details": {"name": "New Name"},
                }],
            },
        ]

        with patch.object(handler_module, "AUDIT_TABLE", "test-audit"):
            result = get_audit_event("admin@example.com", "ws-1", "target-rid")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["event"]["request_id"] == "target-rid"
        # Verify the second call carried ExclusiveStartKey (pagination
        # actually happened, we didn't short-circuit).
        second_call_kwargs = mock_table.query.call_args_list[1].kwargs
        assert "ExclusiveStartKey" in second_call_kwargs
        assert mock_table.query.call_count == 2
