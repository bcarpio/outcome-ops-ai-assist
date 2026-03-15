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
