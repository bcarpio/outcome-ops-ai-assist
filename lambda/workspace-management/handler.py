"""
Workspace Management Lambda for OutcomeOps AI Assist

Handles workspace CRUD operations, membership management, org administration,
and cross-workspace sharing. Workspaces are team/project groupings that can
contain multiple repos and OAuth integrations (Confluence, Jira, GitHub).

Access Control:
    - org_admin: Platform admin, can manage ALL workspaces
    - workspace_admin: Can manage their specific workspace (members, integrations, sharing)
    - member: Basic workspace access (view, query, upload)

API Endpoints:
    Workspaces:
        POST   /workspaces                        - Create workspace
        GET    /workspaces                        - List user's workspaces
        GET    /workspaces/{id}                   - Get workspace
        PATCH  /workspaces/{id}                   - Update workspace
        DELETE /workspaces/{id}                   - Delete workspace

    Membership:
        POST   /workspaces/{id}/members           - Add member
        GET    /workspaces/{id}/members           - List members
        PATCH  /workspaces/{id}/members/{email}   - Update member role
        DELETE /workspaces/{id}/members/{email}   - Remove member

    Repos:
        GET    /workspaces/{id}/repos/available   - List available repos from GitHub
        GET    /workspaces/{id}/repos             - List workspace repos
        POST   /workspaces/{id}/repos             - Add repo to workspace
        DELETE /workspaces/{id}/repos/{name}      - Remove repo from workspace

    Sharing (cross-workspace search access):
        GET    /workspaces/{id}/sharing           - Get which workspaces can search this one
        PUT    /workspaces/{id}/sharing           - Update search access list
        GET    /workspaces/{id}/sharing/available - List workspaces for sharing dropdown

    Org Management (org_admin only):
        GET    /org/members                       - List all org members
        POST   /org/members                       - Add org member
        PATCH  /org/members/{email}               - Update org role
        DELETE /org/members/{email}               - Remove org member

    Chat:
        GET    /chat/searchable-workspaces        - List workspaces user can query
        GET    /chat/mcp-servers                  - List MCP servers available for chat

    MCP Server Management (org_admin only):
        POST   /mcp/servers                       - Create MCP server
        GET    /mcp/servers                       - List all MCP servers
        GET    /mcp/servers/{id}                  - Get MCP server details
        PATCH  /mcp/servers/{id}                  - Update MCP server
        DELETE /mcp/servers/{id}                  - Delete MCP server
        POST   /mcp/servers/{id}/refresh          - Refresh tool manifest

    MCP Workspace Assignments:
        POST   /workspaces/{id}/mcp-servers       - Assign MCP server to workspace
        GET    /workspaces/{id}/mcp-servers       - List workspace MCP servers
        DELETE /workspaces/{id}/mcp-servers/{mid}  - Remove MCP server from workspace

    API Keys (any authenticated user):
        POST   /api-keys                          - Create personal API key
        GET    /api-keys                          - List user's API keys
        DELETE /api-keys/{key_id}                 - Revoke API key

Security: Invoked via IAM-authenticated Lambda Function URL from Fargate.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError, field_validator

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Cached clients and config
_dynamodb = None
_ssm_client = None
_s3_client = None
_sqs_client = None
_github_client = None
_workspaces_table = None
_kb_bucket = None
_delete_queue_url = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _get_kb_bucket() -> str:
    """Get knowledge base bucket name from environment or SSM."""
    global _kb_bucket
    if _kb_bucket is not None:
        return _kb_bucket

    # Try environment variable first
    _kb_bucket = os.environ.get("KB_BUCKET")
    if _kb_bucket:
        return _kb_bucket

    # Fall back to SSM
    ssm = _get_ssm_client()
    response = ssm.get_parameter(Name=f"/{ENV}/{APP_NAME}/s3/kb-bucket")
    _kb_bucket = response["Parameter"]["Value"]
    return _kb_bucket


def _get_delete_queue_url() -> Optional[str]:
    """Get delete queue URL from environment or SSM."""
    global _delete_queue_url
    if _delete_queue_url is not None:
        return _delete_queue_url

    # Try environment variable first
    _delete_queue_url = os.environ.get("DELETE_QUEUE_URL")
    if _delete_queue_url:
        return _delete_queue_url

    # Fall back to SSM
    try:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(Name=f"/{ENV}/{APP_NAME}/sqs/delete-queue-url")
        _delete_queue_url = response["Parameter"]["Value"]
        return _delete_queue_url
    except ClientError:
        logger.warning("Delete queue URL not configured")
        return None


def _get_workspaces_table():
    global _workspaces_table
    if _workspaces_table is None:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(
            Name=f"/{ENV}/{APP_NAME}/dynamodb/workspaces-table"
        )
        _workspaces_table = response["Parameter"]["Value"]
    return _workspaces_table


# ============================================================================
# Request/Response Models
# ============================================================================


class CreateWorkspaceRequest(BaseModel):
    """Request model for creating a workspace."""

    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class UpdateWorkspaceRequest(BaseModel):
    """Request model for updating a workspace."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)


class AddMemberRequest(BaseModel):
    """Request model for adding a member to a workspace."""

    email: str
    role: str = Field(default="member")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("workspace_admin", "member"):
            raise ValueError("role must be 'workspace_admin' or 'member'")
        return v


class UpdateMemberRequest(BaseModel):
    """Request model for updating a member's role."""

    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("workspace_admin", "member"):
            raise ValueError("role must be 'workspace_admin' or 'member'")
        return v


class AddRepoRequest(BaseModel):
    """Request model for adding a repo to a workspace."""

    repo_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("repo_name")
    @classmethod
    def strip_repo_name(cls, v: str) -> str:
        return v.strip()


class UpdateSharingRequest(BaseModel):
    """Request model for updating workspace sharing (search access)."""

    search_allowed_workspaces: List[str] = Field(default_factory=list)


class AddOrgMemberRequest(BaseModel):
    """Request model for adding an org member."""

    email: str
    org_role: str = Field(default="member")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v

    @field_validator("org_role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("org_admin", "member"):
            raise ValueError("org_role must be 'org_admin' or 'member'")
        return v


class UpdateOrgMemberRequest(BaseModel):
    """Request model for updating an org member's role."""

    org_role: str

    @field_validator("org_role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("org_admin", "member"):
            raise ValueError("org_role must be 'org_admin' or 'member'")
        return v


class DocumentUploadRequest(BaseModel):
    """Request model for generating document upload URL."""

    filename: str = Field(..., min_length=1, max_length=500)
    content_type: str = Field(default="application/octet-stream")
    folder_path: Optional[str] = Field(default="")

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        v = v.strip()
        # Validate extension
        allowed_extensions = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".html"}
        ext = v.lower().rsplit(".", 1)[-1] if "." in v else ""
        if f".{ext}" not in allowed_extensions:
            raise ValueError(f"File type .{ext} not supported. Allowed: {', '.join(allowed_extensions)}")
        return v


class CreateApiKeyRequest(BaseModel):
    """Request model for creating a personal API key."""

    name: str = Field(..., min_length=1, max_length=100)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class CreateMcpServerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1, max_length=2048)
    auth_type: str = Field(default="none")
    credentials: Optional[str] = None
    is_global: bool = Field(default=False)
    refresh_interval_days: int = Field(default=7, ge=1, le=90)

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str) -> str:
        if v not in ("none", "api_key", "bearer"):
            raise ValueError("auth_type must be 'none', 'api_key', or 'bearer'")
        return v


class UpdateMcpServerRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    auth_type: Optional[str] = None
    credentials: Optional[str] = None
    is_global: Optional[bool] = None
    refresh_interval_days: Optional[int] = Field(default=None, ge=1, le=90)

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("none", "api_key", "bearer"):
            raise ValueError("auth_type must be 'none', 'api_key', or 'bearer'")
        return v


# ============================================================================
# Helper Functions
# ============================================================================


def generate_id() -> str:
    """Generate a unique ID."""
    return secrets.token_urlsafe(16)


def _response(status_code: int, body: Dict) -> Dict:
    """Build HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _error(status_code: int, message: str) -> Dict:
    """Build error response."""
    return _response(status_code, {"error": message})


def _get_user_from_request(event: Dict) -> Optional[str]:
    """Extract user email from request headers (set by Fargate proxy).

    Normalizes email to lowercase for consistent DynamoDB lookups,
    since Azure AD may return different case than expected.
    """
    headers = event.get("headers") or {}
    email = headers.get("x-user-email")
    return email.lower() if email else None


def _parse_path(path: str) -> Tuple[str, ...]:
    """Parse path into components."""
    parts = [p for p in path.strip("/").split("/") if p]
    return tuple(parts)


# ============================================================================
# KMS Helpers (for MCP credential encryption)
# ============================================================================

_kms_client = None


def _get_kms_client():
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms")
    return _kms_client


def _get_kms_key_arn():
    return os.environ.get("OAUTH_KMS_KEY_ARN", "")


def _encrypt_credentials(plaintext: str) -> str:
    kms = _get_kms_client()
    response = kms.encrypt(KeyId=_get_kms_key_arn(), Plaintext=plaintext.encode())
    return base64.b64encode(response["CiphertextBlob"]).decode()


def _decrypt_credentials(encrypted: str) -> str:
    kms = _get_kms_client()
    response = kms.decrypt(CiphertextBlob=base64.b64decode(encrypted))
    return response["Plaintext"].decode()


# ============================================================================
# Workspace Operations
# ============================================================================


def create_workspace(user_email: str, request: CreateWorkspaceRequest) -> Dict:
    """Create a new workspace and add creator as admin."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    workspace_id = generate_id()
    now = datetime.now(timezone.utc).isoformat()

    # Create workspace metadata with sharing defaults
    workspace_item = {
        "PK": f"WS#{workspace_id}",
        "SK": "METADATA",
        "workspace_id": workspace_id,
        "name": request.name,
        "description": request.description or "",
        "created_at": now,
        "updated_at": now,
        "created_by": user_email,
        "search_allowed_workspaces": [],  # No sharing by default
    }

    # Create membership for creator (admin)
    member_item = {
        "PK": f"USER#{user_email}",
        "SK": f"WS_MEMBER#{workspace_id}",
        "GSI1PK": f"WS#{workspace_id}",
        "GSI1SK": f"USER#{user_email}",
        "workspace_id": workspace_id,
        "user_email": user_email,
        "role": "workspace_admin",
        "joined_at": now,
    }

    try:
        # Write both items
        with table.batch_writer() as batch:
            batch.put_item(Item=workspace_item)
            batch.put_item(Item=member_item)

        logger.info(f"Created workspace: {workspace_id} by {user_email}")
        return _response(201, {"workspace": workspace_item})
    except ClientError as e:
        logger.error(f"Error creating workspace: {e}")
        return _error(500, "Failed to create workspace")


def list_workspaces(user_email: str) -> Dict:
    """List workspaces for a user. Org admins see all workspaces."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        is_org_admin = _is_org_admin(table, user_email)

        if is_org_admin:
            # Org admins see all workspaces
            all_ws = _scan_all_workspaces(table)
            workspaces = []
            for ws in all_ws:
                ws["role"] = "org_admin"
                workspaces.append(ws)
        else:
            # Regular users see only their workspaces
            response = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_email}",
                    ":sk": "WS_MEMBER#",
                },
            )

            memberships = response.get("Items", [])

            # Fetch workspace metadata for each membership
            workspaces = []
            for membership in memberships:
                workspace_id = membership.get("workspace_id")
                ws_response = table.get_item(
                    Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"}
                )
                if "Item" in ws_response:
                    workspace = ws_response["Item"]
                    workspace["role"] = membership.get("role")
                    workspaces.append(workspace)

        # Sort by updated_at descending
        workspaces.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        return _response(200, {"workspaces": workspaces})
    except ClientError as e:
        logger.error(f"Error listing workspaces: {e}")
        return _error(500, "Failed to list workspaces")


def get_workspace(user_email: str, workspace_id: str) -> Dict:
    """Get workspace details."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check access (member or org admin)
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        # Get workspace metadata
        response = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "Workspace not found")

        workspace = response["Item"]

        # Get role - org_admin or workspace role
        if _is_org_admin(table, user_email):
            workspace["role"] = "org_admin"
        else:
            membership = _get_membership(table, user_email, workspace_id)
            workspace["role"] = membership.get("role") if membership else "member"

        return _response(200, {"workspace": workspace})
    except ClientError as e:
        logger.error(f"Error getting workspace: {e}")
        return _error(500, "Failed to get workspace")


def update_workspace(
    user_email: str, workspace_id: str, request: UpdateWorkspaceRequest
) -> Dict:
    """Update workspace details (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can update workspace")

        # Build update expression
        update_parts = []
        expr_values = {}
        now = datetime.now(timezone.utc).isoformat()

        if request.name is not None:
            update_parts.append("#n = :name")
            expr_values[":name"] = request.name
        if request.description is not None:
            update_parts.append("description = :desc")
            expr_values[":desc"] = request.description

        update_parts.append("updated_at = :ua")
        expr_values[":ua"] = now

        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames={"#n": "name"} if request.name else {},
            ExpressionAttributeValues=expr_values,
        )

        logger.info(f"Updated workspace: {workspace_id}")
        return get_workspace(user_email, workspace_id)
    except ClientError as e:
        logger.error(f"Error updating workspace: {e}")
        return _error(500, "Failed to update workspace")


def delete_workspace(user_email: str, workspace_id: str) -> Dict:
    """Delete workspace and all associated data (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can delete workspace")

        # Get all items for this workspace (metadata, repos, connections)
        ws_items = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"WS#{workspace_id}"},
        ).get("Items", [])

        # Get all membership items via GSI
        member_items = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :gsi1pk",
            ExpressionAttributeValues={":gsi1pk": f"WS#{workspace_id}"},
        ).get("Items", [])

        # Delete all items
        with table.batch_writer() as batch:
            for item in ws_items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            for item in member_items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

        logger.info(f"Deleted workspace: {workspace_id}")
        return _response(200, {"deleted": workspace_id})
    except ClientError as e:
        logger.error(f"Error deleting workspace: {e}")
        return _error(500, "Failed to delete workspace")


# ============================================================================
# Membership Operations
# ============================================================================


def _get_membership(table, user_email: str, workspace_id: str) -> Optional[Dict]:
    """Get user's membership in a workspace."""
    try:
        response = table.get_item(
            Key={"PK": f"USER#{user_email}", "SK": f"WS_MEMBER#{workspace_id}"}
        )
        return response.get("Item")
    except ClientError:
        return None


def _get_org_membership(table, user_email: str) -> Optional[Dict]:
    """Get user's org membership record."""
    try:
        response = table.get_item(
            Key={"PK": f"USER#{user_email}", "SK": "ORG_MEMBER"}
        )
        return response.get("Item")
    except ClientError:
        return None


def _get_org_role(table, user_email: str) -> str:
    """Get user's org role (org_admin or member). Returns 'member' if no record."""
    membership = _get_org_membership(table, user_email)
    if membership:
        return membership.get("org_role", "member")
    return "member"


def _is_org_admin(table, user_email: str) -> bool:
    """Check if user is an org admin."""
    return _get_org_role(table, user_email) == "org_admin"


def _is_workspace_admin(table, user_email: str, workspace_id: str) -> bool:
    """Check if user is a workspace admin (or org admin)."""
    # Org admins can manage any workspace
    if _is_org_admin(table, user_email):
        return True
    membership = _get_membership(table, user_email, workspace_id)
    return membership is not None and membership.get("role") == "workspace_admin"


def _can_access_workspace(table, user_email: str, workspace_id: str) -> bool:
    """Check if user can access a workspace (member or org admin)."""
    if _is_org_admin(table, user_email):
        return True
    return _get_membership(table, user_email, workspace_id) is not None


def add_member(
    user_email: str, workspace_id: str, request: AddMemberRequest
) -> Dict:
    """Add a member to workspace (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can add members")

        # Check if workspace exists
        ws_response = table.get_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"}
        )
        if "Item" not in ws_response:
            return _error(404, "Workspace not found")

        # Check if user is already a member
        existing = _get_membership(table, request.email, workspace_id)
        if existing:
            return _error(409, "User is already a member of this workspace")

        now = datetime.now(timezone.utc).isoformat()

        member_item = {
            "PK": f"USER#{request.email}",
            "SK": f"WS_MEMBER#{workspace_id}",
            "GSI1PK": f"WS#{workspace_id}",
            "GSI1SK": f"USER#{request.email}",
            "workspace_id": workspace_id,
            "user_email": request.email,
            "role": request.role,
            "joined_at": now,
        }

        table.put_item(Item=member_item)

        logger.info(f"Added member {request.email} to workspace {workspace_id}")
        return _response(201, {"member": member_item})
    except ClientError as e:
        logger.error(f"Error adding member: {e}")
        return _error(500, "Failed to add member")


def list_members(user_email: str, workspace_id: str) -> Dict:
    """List workspace members."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check access (member or org admin)
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        # Query members via GSI
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :gsi1pk AND begins_with(GSI1SK, :sk)",
            ExpressionAttributeValues={
                ":gsi1pk": f"WS#{workspace_id}",
                ":sk": "USER#",
            },
        )

        members = response.get("Items", [])

        # Sort by role (admins first) then by email
        members.sort(key=lambda x: (x.get("role") != "workspace_admin", x.get("user_email", "")))

        return _response(200, {"members": members})
    except ClientError as e:
        logger.error(f"Error listing members: {e}")
        return _error(500, "Failed to list members")


def update_member(
    user_email: str, workspace_id: str, member_email: str, request: UpdateMemberRequest
) -> Dict:
    """Update member role (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can update member roles")

        # Can't change own role if last admin
        if member_email == user_email and request.role != "workspace_admin":
            # Count admins
            members_response = table.query(
                IndexName="GSI1",
                KeyConditionExpression="GSI1PK = :gsi1pk",
                ExpressionAttributeValues={":gsi1pk": f"WS#{workspace_id}"},
            )
            admin_count = sum(
                1 for m in members_response.get("Items", [])
                if m.get("role") == "workspace_admin"
            )
            if admin_count <= 1:
                return _error(400, "Cannot remove last admin from workspace")

        # Check member exists
        existing = _get_membership(table, member_email, workspace_id)
        if not existing:
            return _error(404, "Member not found")

        table.update_item(
            Key={"PK": f"USER#{member_email}", "SK": f"WS_MEMBER#{workspace_id}"},
            UpdateExpression="SET #r = :role",
            ExpressionAttributeNames={"#r": "role"},
            ExpressionAttributeValues={":role": request.role},
        )

        logger.info(f"Updated member {member_email} role to {request.role}")
        return _response(200, {"email": member_email, "role": request.role})
    except ClientError as e:
        logger.error(f"Error updating member: {e}")
        return _error(500, "Failed to update member")


def remove_member(user_email: str, workspace_id: str, member_email: str) -> Dict:
    """Remove member from workspace (admin only, or self-removal)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        is_admin = _is_workspace_admin(table, user_email, workspace_id)
        is_self = user_email == member_email

        if not is_admin and not is_self:
            return _error(403, "Only workspace admins can remove other members")

        # Can't remove last admin
        if is_self and is_admin:
            members_response = table.query(
                IndexName="GSI1",
                KeyConditionExpression="GSI1PK = :gsi1pk",
                ExpressionAttributeValues={":gsi1pk": f"WS#{workspace_id}"},
            )
            admin_count = sum(
                1 for m in members_response.get("Items", [])
                if m.get("role") == "workspace_admin"
            )
            if admin_count <= 1:
                return _error(400, "Cannot remove last admin from workspace")

        # Check member exists
        existing = _get_membership(table, member_email, workspace_id)
        if not existing:
            return _error(404, "Member not found")

        table.delete_item(
            Key={"PK": f"USER#{member_email}", "SK": f"WS_MEMBER#{workspace_id}"}
        )

        logger.info(f"Removed member {member_email} from workspace {workspace_id}")
        return _response(200, {"removed": member_email})
    except ClientError as e:
        logger.error(f"Error removing member: {e}")
        return _error(500, "Failed to remove member")


# ============================================================================
# Repository Operations
# ============================================================================


def list_repos(user_email: str, workspace_id: str) -> Dict:
    """List repos in workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check access (member or org admin)
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        # Query repos for workspace
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":sk": "GITHUB_REPO#",
            },
        )

        repos = response.get("Items", [])

        return _response(200, {"repos": repos})
    except ClientError as e:
        logger.error(f"Error listing repos: {e}")
        return _error(500, "Failed to list repos")


def add_repo(user_email: str, workspace_id: str, request: AddRepoRequest) -> Dict:
    """Add repo to workspace (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can add repos")

        now = datetime.now(timezone.utc).isoformat()

        repo_item = {
            "PK": f"WS#{workspace_id}",
            "SK": f"GITHUB_REPO#{request.repo_name}",
            "workspace_id": workspace_id,
            "repo_name": request.repo_name,
            "enabled": True,
            "added_at": now,
            "added_by": user_email,
        }

        # Use condition to prevent overwrite
        table.put_item(
            Item=repo_item,
            ConditionExpression="attribute_not_exists(PK)",
        )

        logger.info(f"Added repo {request.repo_name} to workspace {workspace_id}")
        return _response(201, {"repo": repo_item})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(409, "Repo already exists in workspace")
        logger.error(f"Error adding repo: {e}")
        return _error(500, "Failed to add repo")


def remove_repo(user_email: str, workspace_id: str, repo_name: str) -> Dict:
    """Remove repo from workspace (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can remove repos")

        table.delete_item(
            Key={"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"}
        )

        logger.info(f"Removed repo {repo_name} from workspace {workspace_id}")
        return _response(200, {"removed": repo_name})
    except ClientError as e:
        logger.error(f"Error removing repo: {e}")
        return _error(500, "Failed to remove repo")


def list_available_repos(user_email: str, workspace_id: str) -> Dict:
    """List available repos from GitHub API."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check access (member or org admin)
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        # Get GitHub PAT from SSM
        ssm = _get_ssm_client()
        try:
            pat_response = ssm.get_parameter(
                Name=f"/{ENV}/{APP_NAME}/github/pat",
                WithDecryption=True,
            )
            github_pat = pat_response["Parameter"]["Value"]
        except ClientError:
            return _error(500, "GitHub PAT not configured")

        # Call GitHub API to list repos
        import urllib.request

        req = urllib.request.Request(
            "https://api.github.com/user/repos?per_page=100&sort=updated",
            headers={
                "Authorization": f"Bearer {github_pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            repos_data = json.loads(response.read().decode())

        # Extract relevant fields
        available_repos = [
            {
                "full_name": repo["full_name"],
                "name": repo["name"],
                "description": repo.get("description", ""),
                "private": repo["private"],
                "updated_at": repo["updated_at"],
            }
            for repo in repos_data
        ]

        return _response(200, {"repos": available_repos})
    except Exception as e:
        logger.error(f"Error listing available repos: {e}")
        return _error(500, f"Failed to list available repos: {str(e)}")


# ============================================================================
# Workspace Sharing Operations
# ============================================================================


def get_search_access(user_email: str, workspace_id: str) -> Dict:
    """Get which workspaces can search this workspace's content."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be workspace admin to view sharing settings
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can view sharing settings")

        # Get workspace metadata
        response = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "Workspace not found")

        workspace = response["Item"]
        allowed_ids = workspace.get("search_allowed_workspaces", [])

        # Fetch workspace names for the allowed IDs
        allowed_workspaces = []
        for ws_id in allowed_ids:
            ws_response = table.get_item(Key={"PK": f"WS#{ws_id}", "SK": "METADATA"})
            if "Item" in ws_response:
                allowed_workspaces.append({
                    "workspace_id": ws_id,
                    "name": ws_response["Item"].get("name", "Unknown"),
                })

        return _response(200, {
            "workspace_id": workspace_id,
            "search_allowed_workspaces": allowed_workspaces,
        })
    except ClientError as e:
        logger.error(f"Error getting search access: {e}")
        return _error(500, "Failed to get search access")


def update_search_access(
    user_email: str, workspace_id: str, request: UpdateSharingRequest
) -> Dict:
    """Update which workspaces can search this workspace's content."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be workspace admin to update sharing
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can update sharing settings")

        # Validate all workspace IDs exist
        for ws_id in request.search_allowed_workspaces:
            ws_response = table.get_item(Key={"PK": f"WS#{ws_id}", "SK": "METADATA"})
            if "Item" not in ws_response:
                return _error(400, f"Workspace not found: {ws_id}")

        # Update workspace metadata
        now = datetime.now(timezone.utc).isoformat()
        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            UpdateExpression="SET search_allowed_workspaces = :saw, updated_at = :ua",
            ExpressionAttributeValues={
                ":saw": request.search_allowed_workspaces,
                ":ua": now,
            },
        )

        logger.info(f"Updated sharing for {workspace_id}: {request.search_allowed_workspaces}")
        return get_search_access(user_email, workspace_id)
    except ClientError as e:
        logger.error(f"Error updating search access: {e}")
        return _error(500, "Failed to update search access")


def list_all_workspaces_for_sharing(user_email: str, workspace_id: str) -> Dict:
    """List all workspaces that could be granted search access (for UI dropdown)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be workspace admin
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can view sharing options")

        # Get all workspace metadata (paginated)
        all_ws = _scan_all_workspaces(table)

        workspaces = []
        for ws in all_ws:
            ws_id = ws.get("workspace_id")
            # Exclude the current workspace
            if ws_id and ws_id != workspace_id:
                workspaces.append({
                    "workspace_id": ws_id,
                    "name": ws.get("name", "Unknown"),
                })

        workspaces.sort(key=lambda x: x.get("name", "").lower())
        return _response(200, {"workspaces": workspaces})
    except ClientError as e:
        logger.error(f"Error listing workspaces for sharing: {e}")
        return _error(500, "Failed to list workspaces")


# ============================================================================
# User Search Operations
# ============================================================================


def search_users(user_email: str, query: str) -> Dict:
    """Search for existing users by email prefix.

    Collects unique users from PROFILE, ORG_MEMBER, and WS_MEMBER records,
    then filters by the search query. Used by the Add Member UI to find
    existing users to add to workspaces.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        query_lower = query.lower().strip()
        if len(query_lower) < 2:
            return _response(200, {"users": []})

        users_by_email = {}

        # Scan for records that have a user_email attribute
        scan_kwargs = {
            "FilterExpression": "attribute_exists(user_email)",
            "ProjectionExpression": "user_email, #n, SK, is_service_account",
            "ExpressionAttributeNames": {"#n": "name"},
        }
        while True:
            response = table.scan(**scan_kwargs)
            for item in response.get("Items", []):
                email = item.get("user_email", "")
                if not email or query_lower not in email.lower():
                    continue
                if email not in users_by_email:
                    users_by_email[email] = {
                        "user_email": email,
                        "name": item.get("name", ""),
                        "is_service_account": item.get("is_service_account", False),
                    }
                # Prefer name from PROFILE record
                if item.get("SK") == "PROFILE" and item.get("name"):
                    users_by_email[email]["name"] = item["name"]
                    users_by_email[email]["is_service_account"] = item.get(
                        "is_service_account", False
                    )
            if "LastEvaluatedKey" not in response:
                break
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        users = sorted(users_by_email.values(), key=lambda u: u["user_email"])
        return _response(200, {"users": users[:20]})
    except ClientError as e:
        logger.error(f"Error searching users: {e}")
        return _error(500, "Failed to search users")


# ============================================================================
# Org Management Operations
# ============================================================================


def list_org_members(user_email: str) -> Dict:
    """List all org members (org admin only).

    Derives the member list from two sources:
      1. ORG_MEMBER records (org admins and explicitly added members)
      2. WS_MEMBER records (anyone added to any workspace)

    This avoids sync issues where a user is in a workspace but missing
    from the org listing.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be org admin
        if not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can list org members")

        # Source 1: Explicit org membership records
        org_response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :gsi1pk AND begins_with(GSI1SK, :user_prefix)",
            ExpressionAttributeValues={
                ":gsi1pk": "ORG#default",
                ":user_prefix": "USER#",
            },
        )

        # Build lookup by email from org records
        members_by_email = {}
        for item in org_response.get("Items", []):
            email = item.get("user_email", "")
            if email:
                members_by_email[email] = {
                    "user_email": email,
                    "org_role": item.get("org_role", "member"),
                    "joined_at": item.get("joined_at", ""),
                    "is_service_account": item.get("is_service_account", False),
                    "workspaces": [],
                }

        # Source 2: All workspace membership records
        scan_kwargs = {
            "FilterExpression": "begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":sk": "WS_MEMBER#"},
        }
        while True:
            ws_response = table.scan(**scan_kwargs)
            for item in ws_response.get("Items", []):
                email = item.get("user_email", "")
                if not email:
                    continue
                ws_id = item.get("workspace_id", "")
                ws_role = item.get("role", "member")
                if email not in members_by_email:
                    members_by_email[email] = {
                        "user_email": email,
                        "org_role": "member",
                        "joined_at": item.get("joined_at", ""),
                        "workspaces": [],
                    }
                members_by_email[email]["workspaces"].append({
                    "workspace_id": ws_id,
                    "role": ws_role,
                })
            if "LastEvaluatedKey" not in ws_response:
                break
            scan_kwargs["ExclusiveStartKey"] = ws_response["LastEvaluatedKey"]

        members = list(members_by_email.values())

        # Sort: org_admins first, then by email
        members.sort(key=lambda x: (x.get("org_role") != "org_admin", x.get("user_email", "")))

        return _response(200, {"members": members})
    except ClientError as e:
        logger.error(f"Error listing org members: {e}")
        return _error(500, "Failed to list org members")


def add_org_member(user_email: str, request: AddOrgMemberRequest) -> Dict:
    """Add or invite an org member (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be org admin
        if not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can add org members")

        # Check if already a member
        existing = _get_org_membership(table, request.email)
        if existing:
            return _error(409, "User is already an org member")

        now = datetime.now(timezone.utc).isoformat()

        member_item = {
            "PK": f"USER#{request.email}",
            "SK": "ORG_MEMBER",
            "GSI1PK": "ORG#default",
            "GSI1SK": f"USER#{request.email}",
            "user_email": request.email,
            "org_role": request.org_role,
            "joined_at": now,
            "invited_by": user_email,
        }

        table.put_item(Item=member_item)

        logger.info(f"Added org member {request.email} with role {request.org_role}")
        return _response(201, {"member": member_item})
    except ClientError as e:
        logger.error(f"Error adding org member: {e}")
        return _error(500, "Failed to add org member")


def update_org_member(
    user_email: str, member_email: str, request: UpdateOrgMemberRequest
) -> Dict:
    """Update an org member's role (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be org admin
        if not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can update org member roles")

        # Can't demote self if last org admin
        if member_email == user_email and request.org_role != "org_admin":
            # Count org admins
            response = table.query(
                IndexName="GSI1",
                KeyConditionExpression="GSI1PK = :gsi1pk",
                ExpressionAttributeValues={":gsi1pk": "ORG#default"},
            )
            admin_count = sum(
                1 for m in response.get("Items", [])
                if m.get("org_role") == "org_admin"
            )
            if admin_count <= 1:
                return _error(400, "Cannot demote the last org admin")

        # Check member exists
        existing = _get_org_membership(table, member_email)
        if not existing:
            return _error(404, "Org member not found")

        table.update_item(
            Key={"PK": f"USER#{member_email}", "SK": "ORG_MEMBER"},
            UpdateExpression="SET org_role = :role",
            ExpressionAttributeValues={":role": request.org_role},
        )

        logger.info(f"Updated org member {member_email} role to {request.org_role}")
        return _response(200, {"email": member_email, "org_role": request.org_role})
    except ClientError as e:
        logger.error(f"Error updating org member: {e}")
        return _error(500, "Failed to update org member")


def remove_org_member(user_email: str, member_email: str) -> Dict:
    """Remove an org member (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be org admin
        if not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can remove org members")

        # Can't remove self if last org admin
        if member_email == user_email:
            response = table.query(
                IndexName="GSI1",
                KeyConditionExpression="GSI1PK = :gsi1pk",
                ExpressionAttributeValues={":gsi1pk": "ORG#default"},
            )
            admin_count = sum(
                1 for m in response.get("Items", [])
                if m.get("org_role") == "org_admin"
            )
            if admin_count <= 1:
                return _error(400, "Cannot remove the last org admin")

        # Remove org membership record if it exists (org admins have one)
        existing = _get_org_membership(table, member_email)
        if existing:
            table.delete_item(Key={"PK": f"USER#{member_email}", "SK": "ORG_MEMBER"})

        # Remove all workspace memberships for this user
        ws_response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"USER#{member_email}",
                ":sk": "WS_MEMBER#",
            },
        )
        for item in ws_response.get("Items", []):
            table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

        logger.info(f"Removed org member {member_email}")
        return _response(200, {"removed": member_email})
    except ClientError as e:
        logger.error(f"Error removing org member: {e}")
        return _error(500, "Failed to remove org member")


# ============================================================================
# Searchable Workspaces (for Chat)
# ============================================================================


def _scan_all_workspaces(table) -> List[Dict]:
    """Scan all workspace METADATA items with pagination."""
    items = []
    scan_kwargs = {
        "FilterExpression": "SK = :sk AND begins_with(PK, :pk_prefix)",
        "ExpressionAttributeValues": {":sk": "METADATA", ":pk_prefix": "WS#"},
    }
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def get_searchable_workspaces(user_email: str) -> Dict:
    """
    Get all workspaces a user can query in chat.

    Includes:
    - Workspaces user is a member of
    - Workspaces that have shared search access with user's workspaces
    - All workspaces if user is org admin
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        is_admin = _is_org_admin(table, user_email)
        logger.info(f"Searchable workspaces for {user_email}: is_org_admin={is_admin}")

        if is_admin:
            # Org admins can search all workspaces
            all_ws = _scan_all_workspaces(table)
            workspaces = [
                {
                    "workspace_id": ws.get("workspace_id"),
                    "name": ws.get("name"),
                    "access_type": "org_admin",
                }
                for ws in all_ws
            ]
            logger.info(f"Org admin {user_email}: returning {len(workspaces)} workspaces")
        else:
            workspaces = []
            seen_ids = set()

            # Get user's direct memberships
            member_response = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_email}",
                    ":sk": "WS_MEMBER#",
                },
            )

            user_workspace_ids = []
            for membership in member_response.get("Items", []):
                ws_id = membership.get("workspace_id")
                user_workspace_ids.append(ws_id)

                # Get workspace metadata
                ws_response = table.get_item(Key={"PK": f"WS#{ws_id}", "SK": "METADATA"})
                if "Item" in ws_response:
                    ws = ws_response["Item"]
                    if ws_id not in seen_ids:
                        workspaces.append({
                            "workspace_id": ws_id,
                            "name": ws.get("name"),
                            "access_type": "member",
                        })
                        seen_ids.add(ws_id)

            logger.info(
                f"User {user_email}: {len(user_workspace_ids)} direct memberships, "
                f"{len(workspaces)} member workspaces"
            )

            # Find workspaces that share with user's workspaces
            if user_workspace_ids:
                all_ws = _scan_all_workspaces(table)
                cross_count = 0
                for ws in all_ws:
                    ws_id = ws.get("workspace_id")
                    if ws_id in seen_ids:
                        continue

                    allowed = ws.get("search_allowed_workspaces", [])
                    if allowed and any(user_ws in allowed for user_ws in user_workspace_ids):
                        workspaces.append({
                            "workspace_id": ws_id,
                            "name": ws.get("name"),
                            "access_type": "shared",
                        })
                        seen_ids.add(ws_id)
                        cross_count += 1

                if cross_count > 0:
                    logger.info(f"User {user_email}: {cross_count} cross-workspace shared")

        # Sort by name
        workspaces.sort(key=lambda x: x.get("name", "").lower())

        logger.info(f"Returning {len(workspaces)} searchable workspaces for {user_email}")
        return _response(200, {"workspaces": workspaces})
    except ClientError as e:
        logger.error(f"Error getting searchable workspaces: {e}")
        return _error(500, "Failed to get searchable workspaces")


# ============================================================================
# Document Operations
# ============================================================================


def get_document_upload_url(
    user_email: str, workspace_id: str, request: DocumentUploadRequest
) -> Dict:
    """Generate a presigned URL for document upload."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check access (member or org admin can upload)
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        # Build S3 key
        # Structure: workspaces/{workspace_id}/uploads/{folder_path}/{filename}
        folder = request.folder_path.strip("/") if request.folder_path else ""
        if folder:
            s3_key = f"workspaces/{workspace_id}/uploads/{folder}/{request.filename}"
        else:
            s3_key = f"workspaces/{workspace_id}/uploads/{request.filename}"

        # Get KB bucket
        bucket = _get_kb_bucket()
        s3_client = _get_s3_client()

        # Generate presigned URL (1 hour expiry)
        presigned_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
                "ContentType": request.content_type,
            },
            ExpiresIn=3600,
        )

        logger.info(f"Generated upload URL for {s3_key} by {user_email}")
        return _response(200, {
            "upload_url": presigned_url,
            "s3_key": s3_key,
            "bucket": bucket,
            "expires_in": 3600,
        })
    except ClientError as e:
        logger.error(f"Error generating upload URL: {e}")
        return _error(500, "Failed to generate upload URL")


def list_documents(user_email: str, workspace_id: str, prefix: str = "") -> Dict:
    """List uploaded documents in workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check access (member or org admin)
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        bucket = _get_kb_bucket()
        s3_client = _get_s3_client()

        # List objects under workspaces/{workspace_id}/uploads/
        s3_prefix = f"workspaces/{workspace_id}/uploads/"
        if prefix:
            s3_prefix += prefix.strip("/") + "/"

        paginator = s3_client.get_paginator("list_objects_v2")
        documents = []

        for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
            for obj in page.get("Contents", []):
                # Extract relative path from S3 key
                key = obj["Key"]
                relative_path = key.replace(f"workspaces/{workspace_id}/uploads/", "")

                # Skip folder markers
                if key.endswith("/"):
                    continue

                documents.append({
                    "path": relative_path,
                    "s3_key": key,
                    "filename": relative_path.split("/")[-1],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })

        # Sort by last modified, newest first
        documents.sort(key=lambda x: x["last_modified"], reverse=True)

        return _response(200, {"documents": documents, "count": len(documents)})
    except ClientError as e:
        logger.error(f"Error listing documents: {e}")
        return _error(500, "Failed to list documents")


def delete_document(user_email: str, workspace_id: str, document_path: str) -> Dict:
    """Delete an uploaded document (admin only).

    Sends a message to the delete queue for async processing.
    The delete worker will:
    1. Delete the S3 object
    2. Delete associated vectors from S3 Vectors
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Check admin membership (only admins can delete)
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can delete documents")

        # Build S3 key from document path
        s3_key = f"workspaces/{workspace_id}/uploads/{document_path}"
        bucket = _get_kb_bucket()

        # Verify document exists
        s3_client = _get_s3_client()
        try:
            s3_client.head_object(Bucket=bucket, Key=s3_key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return _error(404, "Document not found")
            raise

        # Get delete queue URL
        queue_url = _get_delete_queue_url()

        if queue_url:
            # Send delete message to queue for async processing
            sqs_client = _get_sqs_client()
            sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps({
                    "action": "delete_document",
                    "bucket": bucket,
                    "s3_key": s3_key,
                    "workspace_id": workspace_id,
                    "source": "uploads",
                    "requested_by": user_email,
                }),
            )
            logger.info(f"Queued deletion of {s3_key} by {user_email}")
            return _response(200, {"message": "Document deletion queued", "path": document_path})
        else:
            # If no queue configured, delete directly
            s3_client.delete_object(Bucket=bucket, Key=s3_key)
            logger.info(f"Deleted {s3_key} directly by {user_email}")
            return _response(200, {"message": "Document deleted", "path": document_path})

    except ClientError as e:
        logger.error(f"Error deleting document: {e}")
        return _error(500, "Failed to delete document")


# ============================================================================
# MCP Server Operations (Chunks 02-04)
# ============================================================================


def _build_mcp_auth_headers(auth_type: str, encrypted_credentials: Optional[str] = None,
                            ssm_token_path: Optional[str] = None,
                            ssm_token_header: Optional[str] = None) -> Dict[str, str]:
    """Build auth headers for MCP server requests."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if auth_type == "bearer" and encrypted_credentials:
        token = _decrypt_credentials(encrypted_credentials)
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "api_key" and encrypted_credentials:
        key = _decrypt_credentials(encrypted_credentials)
        headers["X-API-Key"] = key
    # Catalog servers: read token from SSM and send as custom header
    if ssm_token_path and ssm_token_header:
        try:
            ssm = boto3.client("ssm")
            param = ssm.get_parameter(Name=ssm_token_path, WithDecryption=True)
            headers[ssm_token_header] = param["Parameter"]["Value"]
        except Exception as e:
            logger.warning(f"[workspace_handler] Failed to read SSM token {ssm_token_path}: {e}")
    return headers


def _mcp_request(url: str, headers: Dict[str, str], method: str, params: Optional[Dict] = None,
                 request_id: int = 1) -> Dict:
    """Send a JSON-RPC 2.0 request to an MCP server and return the parsed response."""
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params:
        payload["params"] = params

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        # Capture session ID if returned
        session_id = resp.headers.get("Mcp-Session-Id")
        data = json.loads(resp.read().decode())
        if session_id:
            data["_session_id"] = session_id
        return data


def _mcp_initialize(url: str, headers: Dict[str, str]) -> str:
    """Perform MCP protocol initialize handshake, return session ID."""
    init_params = {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "outcome-ops-ai-assist", "version": "1.0"},
    }
    data = _mcp_request(url, headers, "initialize", params=init_params, request_id=0)
    if "error" in data:
        raise Exception(f"MCP initialize error: {data['error'].get('message', 'Unknown error')}")
    return data.get("_session_id", "")


def _call_mcp_list_tools(url: str, auth_type: str, encrypted_credentials: Optional[str] = None,
                         ssm_token_path: Optional[str] = None,
                         ssm_token_header: Optional[str] = None) -> list:
    """Call MCP server's tools/list method via JSON-RPC 2.0 with proper initialization."""
    headers = _build_mcp_auth_headers(auth_type, encrypted_credentials,
                                      ssm_token_path, ssm_token_header)

    try:
        # Step 1: Initialize session
        session_id = _mcp_initialize(url, headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # Step 2: Send initialized notification (no id = notification)
        notif_payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }).encode()
        notif_req = urllib.request.Request(url, data=notif_payload, headers=headers, method="POST")
        urllib.request.urlopen(notif_req, timeout=10).read()

        # Step 3: List tools
        data = _mcp_request(url, headers, "tools/list", request_id=1)
        if "error" in data:
            raise Exception(f"MCP error: {data['error'].get('message', 'Unknown error')}")
        return data.get("result", {}).get("tools", [])
    except urllib.error.URLError as e:
        raise Exception(f"Failed to connect to MCP server: {e}")
    except Exception as e:
        if "MCP" in str(e) or "Failed to connect" in str(e):
            raise
        raise Exception(f"Failed to call MCP server: {e}")


def _cache_tool_manifest(table, mcp_id: str, tools: list) -> None:
    """Cache tool manifest in DynamoDB."""
    now = datetime.now(timezone.utc).isoformat()

    # Write TOOLS item
    table.put_item(Item={
        "PK": f"MCP#{mcp_id}",
        "SK": "TOOLS",
        "tools": tools,
        "tool_count": len(tools),
        "fetched_at": now,
    })

    # Update METADATA with tool count and refresh time
    table.update_item(
        Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"},
        UpdateExpression="SET last_refreshed_at = :lr, tool_count = :tc, #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":lr": now,
            ":tc": len(tools),
            ":s": "active",
        },
    )


def _is_manifest_stale(mcp_metadata: Dict) -> bool:
    """Check if tool manifest needs refreshing."""
    last_refreshed = mcp_metadata.get("last_refreshed_at")
    if not last_refreshed:
        return True

    refresh_days = int(mcp_metadata.get("refresh_interval_days", 7))
    last_dt = datetime.fromisoformat(last_refreshed.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - last_dt).days >= refresh_days


def create_mcp_server(user_email: str, body: Dict) -> Dict:
    """Create a new MCP server configuration."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    try:
        request = CreateMcpServerRequest(**body)
    except ValidationError as e:
        return _error(400, f"Invalid request: {e}")

    mcp_id = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc).isoformat()

    # Encrypt credentials if provided
    encrypted_credentials = None
    if request.credentials and request.auth_type != "none":
        encrypted_credentials = _encrypt_credentials(request.credentials)

    item = {
        "PK": f"MCP#{mcp_id}",
        "SK": "METADATA",
        "GSI1PK": "ORG#default",
        "GSI1SK": f"MCP#{mcp_id}",
        "mcp_id": mcp_id,
        "name": request.name,
        "url": request.url,
        "auth_type": request.auth_type,
        "is_global": request.is_global,
        "refresh_interval_days": request.refresh_interval_days,
        "status": "active",
        "tool_count": 0,
        "last_refreshed_at": None,
        "created_by": user_email,
        "created_at": now,
        "updated_at": now,
    }
    if encrypted_credentials:
        item["encrypted_credentials"] = encrypted_credentials

    table.put_item(Item=item)
    logger.info(f"[workspace_handler] Created MCP server: {mcp_id} ({request.name})")

    # Try to fetch tools (non-blocking - server is created regardless)
    try:
        tools = _call_mcp_list_tools(request.url, request.auth_type, encrypted_credentials)
        _cache_tool_manifest(table, mcp_id, tools)
        item["tool_count"] = len(tools)
        item["status"] = "active"
    except Exception as e:
        logger.warning(f"[workspace_handler] Failed to fetch tools for MCP server {mcp_id}: {e}")
        table.update_item(
            Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "error"},
        )
        item["status"] = "error"

    # Redact credentials before returning
    item.pop("encrypted_credentials", None)
    item["has_credentials"] = encrypted_credentials is not None

    return _response(201, {"mcp_server": item})


def list_mcp_servers(user_email: str) -> Dict:
    """List all MCP servers (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    try:
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
            ExpressionAttributeValues={
                ":pk": "ORG#default",
                ":sk": "MCP#",
            },
        )

        servers = []
        for item in response.get("Items", []):
            item.pop("encrypted_credentials", None)
            item["has_credentials"] = "encrypted_credentials" in item or item.get("auth_type", "none") != "none"
            servers.append(item)

        servers.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return _response(200, {"mcp_servers": servers})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error listing MCP servers: {e}", exc_info=True)
        return _error(500, "Failed to list MCP servers")


def get_mcp_server(user_email: str, mcp_id: str) -> Dict:
    """Get MCP server details (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    try:
        response = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "MCP server not found")

        item = response["Item"]
        has_creds = "encrypted_credentials" in item
        item.pop("encrypted_credentials", None)
        item["has_credentials"] = has_creds
        return _response(200, {"mcp_server": item})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error getting MCP server: {e}", exc_info=True)
        return _error(500, "Failed to get MCP server")


def update_mcp_server(user_email: str, mcp_id: str, body: Dict) -> Dict:
    """Update an MCP server configuration."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    try:
        request = UpdateMcpServerRequest(**body)
    except ValidationError as e:
        return _error(400, f"Invalid request: {e}")

    try:
        # Get existing
        response = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "MCP server not found")

        existing = response["Item"]
        now = datetime.now(timezone.utc).isoformat()

        update_parts = ["updated_at = :ua"]
        expr_values = {":ua": now}
        expr_names = {}

        if request.name is not None:
            update_parts.append("#n = :name")
            expr_values[":name"] = request.name
            expr_names["#n"] = "name"
        if request.url is not None:
            update_parts.append("#u = :url")
            expr_values[":url"] = request.url
            expr_names["#u"] = "url"
        if request.auth_type is not None:
            update_parts.append("auth_type = :at")
            expr_values[":at"] = request.auth_type
            if request.auth_type == "none":
                update_parts.append("encrypted_credentials = :ec")
                expr_values[":ec"] = None
        if request.credentials is not None:
            auth = request.auth_type or existing.get("auth_type", "none")
            if auth != "none":
                encrypted = _encrypt_credentials(request.credentials)
                update_parts.append("encrypted_credentials = :ec")
                expr_values[":ec"] = encrypted
        if request.is_global is not None:
            update_parts.append("is_global = :ig")
            expr_values[":ig"] = request.is_global
        if request.refresh_interval_days is not None:
            update_parts.append("refresh_interval_days = :rid")
            expr_values[":rid"] = request.refresh_interval_days

        update_kwargs = {
            "Key": {"PK": f"MCP#{mcp_id}", "SK": "METADATA"},
            "UpdateExpression": "SET " + ", ".join(update_parts),
            "ExpressionAttributeValues": expr_values,
        }
        if expr_names:
            update_kwargs["ExpressionAttributeNames"] = expr_names

        table.update_item(**update_kwargs)
        logger.info(f"[workspace_handler] Updated MCP server: {mcp_id}")
        return get_mcp_server(user_email, mcp_id)
    except ClientError as e:
        logger.error(f"[workspace_handler] Error updating MCP server: {e}", exc_info=True)
        return _error(500, "Failed to update MCP server")


def delete_mcp_server(user_email: str, mcp_id: str) -> Dict:
    """Delete an MCP server and all its assignments."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    # Verify it exists
    response = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
    if "Item" not in response:
        return _error(404, "MCP server not found")

    try:
        # Delete METADATA and TOOLS items
        with table.batch_writer() as batch:
            batch.delete_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
            batch.delete_item(Key={"PK": f"MCP#{mcp_id}", "SK": "TOOLS"})

        # Find and delete all workspace assignments via GSI
        assignments = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"MCP#{mcp_id}"},
        ).get("Items", [])

        if assignments:
            with table.batch_writer() as batch:
                for item in assignments:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

        logger.info(f"[workspace_handler] Deleted MCP server: {mcp_id} (and {len(assignments)} assignments)")
        return _response(200, {"deleted": mcp_id})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error deleting MCP server: {e}", exc_info=True)
        return _error(500, "Failed to delete MCP server")


def refresh_mcp_tools(user_email: str, mcp_id: str) -> Dict:
    """Refresh tool manifest for an MCP server."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    try:
        response = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error fetching MCP server metadata: {e}", exc_info=True)
        return _error(500, "Failed to fetch MCP server metadata")

    if "Item" not in response:
        return _error(404, "MCP server not found")

    metadata = response["Item"]

    try:
        tools = _call_mcp_list_tools(
            metadata["url"],
            metadata.get("auth_type", "none"),
            metadata.get("encrypted_credentials"),
            ssm_token_path=metadata.get("ssm_token_path"),
            ssm_token_header=metadata.get("ssm_token_header"),
        )
        _cache_tool_manifest(table, mcp_id, tools)
        logger.info(f"[workspace_handler] Refreshed MCP tools for {mcp_id}: {len(tools)} tools")
        return get_mcp_server(user_email, mcp_id)
    except Exception as e:
        logger.warning(f"[workspace_handler] Failed to refresh tools for MCP server {mcp_id}: {e}")
        table.update_item(
            Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "error"},
        )
        return _error(502, f"Failed to fetch tools from MCP server: {e}")


# ============================================================================
# MCP Workspace Assignment Operations (Chunk 04)
# ============================================================================


def assign_mcp_to_workspace(user_email: str, workspace_id: str, body: Dict) -> Dict:
    """Assign an MCP server to a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can manage MCP assignments")

    mcp_id = body.get("mcp_id")
    if not mcp_id:
        return _error(400, "mcp_id is required")

    try:
        # Verify MCP server exists
        mcp_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
        if "Item" not in mcp_resp:
            return _error(404, "MCP server not found")

        # Verify workspace exists
        ws_resp = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in ws_resp:
            return _error(404, "Workspace not found")

        # Check not already assigned
        existing = table.get_item(
            Key={"PK": f"WS#{workspace_id}", "SK": f"MCP_SERVER#{mcp_id}"}
        )
        if "Item" in existing:
            return _error(409, "MCP server already assigned to this workspace")

        now = datetime.now(timezone.utc).isoformat()
        item = {
            "PK": f"WS#{workspace_id}",
            "SK": f"MCP_SERVER#{mcp_id}",
            "GSI1PK": f"MCP#{mcp_id}",
            "GSI1SK": f"WS#{workspace_id}",
            "mcp_id": mcp_id,
            "workspace_id": workspace_id,
            "assigned_at": now,
            "assigned_by": user_email,
        }

        table.put_item(Item=item)
        logger.info(f"[workspace_handler] Assigned MCP {mcp_id} to workspace {workspace_id}")
        return _response(201, {"assignment": item})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error assigning MCP to workspace: {e}", exc_info=True)
        return _error(500, "Failed to assign MCP server")


def remove_mcp_from_workspace(user_email: str, workspace_id: str, mcp_id: str) -> Dict:
    """Remove an MCP server assignment from a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can manage MCP assignments")

    try:
        table.delete_item(Key={"PK": f"WS#{workspace_id}", "SK": f"MCP_SERVER#{mcp_id}"})
        logger.info(f"[workspace_handler] Removed MCP {mcp_id} from workspace {workspace_id}")
        return _response(200, {"removed": mcp_id})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error removing MCP from workspace: {e}", exc_info=True)
        return _error(500, "Failed to remove MCP server")


def get_mcp_server_workspaces(user_email: str, mcp_id: str) -> Dict:
    """List workspaces assigned to an MCP server (single query via GSI1)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can view MCP server assignments")

    try:
        # GSI1 reverse lookup: GSI1PK=MCP#{id}, GSI1SK=WS#{ws_id}
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"MCP#{mcp_id}",
                ":sk": "WS#",
            },
        )

        workspace_ids = [
            item.get("workspace_id") for item in response.get("Items", [])
            if item.get("workspace_id")
        ]

        return _response(200, {"workspace_ids": workspace_ids})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error getting MCP server workspaces: {e}", exc_info=True)
        return _error(500, "Failed to get MCP server workspaces")


def list_workspace_mcp_servers(user_email: str, workspace_id: str) -> Dict:
    """List MCP servers assigned to a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    try:
        # Get assignments
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":sk": "MCP_SERVER#",
            },
        )

        # Fetch metadata for each assigned server
        servers = []
        for assignment in response.get("Items", []):
            mcp_id = assignment.get("mcp_id")
            meta_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
            if "Item" in meta_resp:
                server = meta_resp["Item"]
                server.pop("encrypted_credentials", None)
                server["has_credentials"] = server.get("auth_type", "none") != "none"
                servers.append(server)

        return _response(200, {"mcp_servers": servers})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error listing workspace MCP servers: {e}", exc_info=True)
        return _error(500, "Failed to list workspace MCP servers")


def get_chat_available_mcp_servers(user_email: str) -> Dict:
    """List MCP servers available to user in chat (from workspaces + global)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        seen_mcp_ids = set()
        servers = []

        # Get user's workspace memberships
        is_admin = _is_org_admin(table, user_email)

        if is_admin:
            # Org admins get all workspaces
            all_ws = _scan_all_workspaces(table)
            workspace_ids = [ws["workspace_id"] for ws in all_ws]
        else:
            memberships = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_email}",
                    ":sk": "WS_MEMBER#",
                },
            ).get("Items", [])
            workspace_ids = [m["workspace_id"] for m in memberships]

        # Get MCP servers from each workspace
        for ws_id in workspace_ids:
            assignments = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"WS#{ws_id}",
                    ":sk": "MCP_SERVER#",
                },
            ).get("Items", [])

            for assignment in assignments:
                mcp_id = assignment.get("mcp_id")
                if mcp_id in seen_mcp_ids:
                    continue
                seen_mcp_ids.add(mcp_id)

                meta_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
                if "Item" in meta_resp:
                    server = meta_resp["Item"]
                    servers.append({
                        "mcp_id": server.get("mcp_id"),
                        "name": server.get("name"),
                        "status": server.get("status", "active"),
                        "tool_count": int(server.get("tool_count", 0)),
                        "is_global": server.get("is_global", False),
                    })

        # Also include global servers (deduplicated)
        global_resp = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
            ExpressionAttributeValues={
                ":pk": "ORG#default",
                ":sk": "MCP#",
            },
        )
        for item in global_resp.get("Items", []):
            if item.get("is_global") and item.get("mcp_id") not in seen_mcp_ids:
                seen_mcp_ids.add(item["mcp_id"])
                servers.append({
                    "mcp_id": item.get("mcp_id"),
                    "name": item.get("name"),
                    "status": item.get("status", "active"),
                    "tool_count": int(item.get("tool_count", 0)),
                    "is_global": True,
                })

        return _response(200, {"mcp_servers": servers})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error getting chat MCP servers: {e}", exc_info=True)
        return _error(500, "Failed to get available MCP servers")


# ============================================================================
# API Key Management Operations
# ============================================================================


def create_api_key(user_email: str, body: Dict) -> Dict:
    """Create a personal API key for MCP client authentication."""
    try:
        request = CreateApiKeyRequest(**body)
    except ValidationError as e:
        return _error(400, f"Invalid request: {e}")

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        key_id = str(secrets.token_urlsafe(16))
        raw_key = "ooa_" + secrets.token_hex(20)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:12]

        now = datetime.now(timezone.utc).isoformat()

        item = {
            "PK": f"USER#{user_email}",
            "SK": f"API_KEY#{key_id}",
            "GSI1PK": f"API_KEY#{key_hash}",
            "GSI1SK": f"API_KEY#{key_id}",
            "key_id": key_id,
            "key_hash": key_hash,
            "key_prefix": key_prefix,
            "name": request.name,
            "created_at": now,
            "status": "active",
        }

        if request.expires_in_days:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)
            ).isoformat()
            item["expires_at"] = expires_at

        table.put_item(Item=item)
        logger.info(f"[workspace_handler] Created API key {key_id} for {user_email}")

        response_body = {
            "key_id": key_id,
            "key": raw_key,
            "key_prefix": key_prefix,
            "name": request.name,
            "created_at": now,
        }
        if "expires_at" in item:
            response_body["expires_at"] = item["expires_at"]

        return _response(201, response_body)
    except ClientError as e:
        logger.error(f"[workspace_handler] Error creating API key: {e}", exc_info=True)
        return _error(500, "Failed to create API key")


def list_api_keys(user_email: str) -> Dict:
    """List API keys for the authenticated user."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_email}",
                ":sk": "API_KEY#",
            },
        )

        keys = []
        for item in response.get("Items", []):
            keys.append({
                "key_id": item.get("key_id"),
                "key_prefix": item.get("key_prefix"),
                "name": item.get("name"),
                "created_at": item.get("created_at"),
                "expires_at": item.get("expires_at"),
                "last_used_at": item.get("last_used_at"),
                "status": item.get("status"),
            })

        return _response(200, {"api_keys": keys})
    except ClientError as e:
        logger.error(f"[workspace_handler] Error listing API keys: {e}", exc_info=True)
        return _error(500, "Failed to list API keys")


def revoke_api_key(user_email: str, key_id: str) -> Dict:
    """Revoke an API key (users can only revoke their own keys)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        now = datetime.now(timezone.utc).isoformat()
        table.update_item(
            Key={"PK": f"USER#{user_email}", "SK": f"API_KEY#{key_id}"},
            UpdateExpression="SET #status = :status, revoked_at = :revoked_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "revoked",
                ":revoked_at": now,
            },
            ConditionExpression="attribute_exists(PK)",
        )
        logger.info(f"[workspace_handler] Revoked API key {key_id} for {user_email}")
        return _response(200, {"message": "API key revoked"})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(404, "API key not found")
        logger.error(f"[workspace_handler] Error revoking API key: {e}", exc_info=True)
        return _error(500, "Failed to revoke API key")


def validate_api_key(api_key: str) -> Optional[Dict]:
    """Validate an API key and return the user info, or None if invalid.

    This is an internal helper used by the MCP server, not an HTTP endpoint.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={
                ":pk": f"API_KEY#{key_hash}",
            },
        )

        items = response.get("Items", [])
        if not items:
            return None

        item = items[0]

        # Check status
        if item.get("status") != "active":
            return None

        # Check expiration
        expires_at = item.get("expires_at")
        if expires_at:
            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                return None

        # Extract email from PK (USER#{email})
        pk = item.get("PK", "")
        email = pk.replace("USER#", "") if pk.startswith("USER#") else None
        if not email:
            return None

        # Update last_used_at (best-effort)
        try:
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET last_used_at = :now",
                ExpressionAttributeValues={
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass  # Non-blocking

        return {"email": email, "key_id": item.get("key_id")}
    except ClientError as e:
        logger.error(f"[workspace_handler] Error validating API key: {e}", exc_info=True)
        return None


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    Main Lambda handler for workspace management.
    Routes requests based on HTTP method and path.
    """
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )

    # Get user email from header
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    logger.info(f"Workspace API: {http_method} {path} (user: {user_email})")

    # Parse path
    parts = _parse_path(path)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    try:
        # Route: POST /workspaces
        if parts == ("workspaces",) and http_method == "POST":
            request = CreateWorkspaceRequest(**body)
            return create_workspace(user_email, request)

        # Route: GET /workspaces
        if parts == ("workspaces",) and http_method == "GET":
            return list_workspaces(user_email)

        # Route: GET /workspaces/{id}
        if len(parts) == 2 and parts[0] == "workspaces" and http_method == "GET":
            return get_workspace(user_email, parts[1])

        # Route: PATCH /workspaces/{id}
        if len(parts) == 2 and parts[0] == "workspaces" and http_method == "PATCH":
            request = UpdateWorkspaceRequest(**body)
            return update_workspace(user_email, parts[1], request)

        # Route: DELETE /workspaces/{id}
        if len(parts) == 2 and parts[0] == "workspaces" and http_method == "DELETE":
            return delete_workspace(user_email, parts[1])

        # Route: POST /workspaces/{id}/members
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "members"
            and http_method == "POST"
        ):
            request = AddMemberRequest(**body)
            return add_member(user_email, parts[1], request)

        # Route: GET /workspaces/{id}/members
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "members"
            and http_method == "GET"
        ):
            return list_members(user_email, parts[1])

        # Route: PATCH /workspaces/{id}/members/{email}
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "members"
            and http_method == "PATCH"
        ):
            member_email = unquote(parts[3])
            request = UpdateMemberRequest(**body)
            return update_member(user_email, parts[1], member_email, request)

        # Route: DELETE /workspaces/{id}/members/{email}
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "members"
            and http_method == "DELETE"
        ):
            member_email = unquote(parts[3])
            return remove_member(user_email, parts[1], member_email)

        # Route: GET /workspaces/{id}/repos/available
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "repos"
            and parts[3] == "available"
            and http_method == "GET"
        ):
            return list_available_repos(user_email, parts[1])

        # Route: GET /workspaces/{id}/repos
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "repos"
            and http_method == "GET"
        ):
            return list_repos(user_email, parts[1])

        # Route: POST /workspaces/{id}/repos
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "repos"
            and http_method == "POST"
        ):
            request = AddRepoRequest(**body)
            return add_repo(user_email, parts[1], request)

        # Route: DELETE /workspaces/{id}/repos/{name}
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "repos"
            and http_method == "DELETE"
        ):
            repo_name = unquote(parts[3])
            return remove_repo(user_email, parts[1], repo_name)

        # ====================================================================
        # Document Upload Routes
        # ====================================================================

        # Route: POST /workspaces/{id}/documents/upload
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "documents"
            and parts[3] == "upload"
            and http_method == "POST"
        ):
            request = DocumentUploadRequest(**body)
            return get_document_upload_url(user_email, parts[1], request)

        # Route: GET /workspaces/{id}/documents
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "documents"
            and http_method == "GET"
        ):
            # Get optional prefix from query string
            query_params = event.get("queryStringParameters") or {}
            prefix = query_params.get("prefix", "")
            return list_documents(user_email, parts[1], prefix)

        # Route: DELETE /workspaces/{id}/documents/{path...}
        # The path can contain slashes, so we need to handle it specially
        if (
            len(parts) >= 4
            and parts[0] == "workspaces"
            and parts[2] == "documents"
            and http_method == "DELETE"
        ):
            # Reconstruct the document path (everything after 'documents')
            document_path = "/".join(unquote(p) for p in parts[3:])
            return delete_document(user_email, parts[1], document_path)

        # ====================================================================
        # Workspace Sharing Routes
        # ====================================================================

        # Route: GET /workspaces/{id}/sharing
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "sharing"
            and http_method == "GET"
        ):
            return get_search_access(user_email, parts[1])

        # Route: PUT /workspaces/{id}/sharing
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "sharing"
            and http_method == "PUT"
        ):
            request = UpdateSharingRequest(**body)
            return update_search_access(user_email, parts[1], request)

        # Route: GET /workspaces/{id}/sharing/available
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "sharing"
            and parts[3] == "available"
            and http_method == "GET"
        ):
            return list_all_workspaces_for_sharing(user_email, parts[1])

        # ====================================================================
        # User Search Routes
        # ====================================================================

        # Route: GET /users/search?q=...
        if parts == ("users", "search") and http_method == "GET":
            query_string = event.get("queryStringParameters") or {}
            q = query_string.get("q", "")
            return search_users(user_email, q)

        # ====================================================================
        # Org Management Routes
        # ====================================================================

        # Route: GET /org/members
        if parts == ("org", "members") and http_method == "GET":
            return list_org_members(user_email)

        # Route: POST /org/members
        if parts == ("org", "members") and http_method == "POST":
            request = AddOrgMemberRequest(**body)
            return add_org_member(user_email, request)

        # Route: PATCH /org/members/{email}
        if (
            len(parts) == 3
            and parts[0] == "org"
            and parts[1] == "members"
            and http_method == "PATCH"
        ):
            member_email = unquote(parts[2])
            request = UpdateOrgMemberRequest(**body)
            return update_org_member(user_email, member_email, request)

        # Route: DELETE /org/members/{email}
        if (
            len(parts) == 3
            and parts[0] == "org"
            and parts[1] == "members"
            and http_method == "DELETE"
        ):
            member_email = unquote(parts[2])
            return remove_org_member(user_email, member_email)

        # ====================================================================
        # Chat Workspace Access Routes
        # ====================================================================

        # Route: GET /chat/searchable-workspaces
        if parts == ("chat", "searchable-workspaces") and http_method == "GET":
            return get_searchable_workspaces(user_email)

        # ====================================================================
        # MCP Server Management Routes
        # ====================================================================

        # Route: POST /mcp/servers
        if parts == ("mcp", "servers") and http_method == "POST":
            return create_mcp_server(user_email, body)

        # Route: GET /mcp/servers
        if parts == ("mcp", "servers") and http_method == "GET":
            return list_mcp_servers(user_email)

        # Route: GET /mcp/servers/{id}
        if (
            len(parts) == 3
            and parts[0] == "mcp"
            and parts[1] == "servers"
            and http_method == "GET"
        ):
            return get_mcp_server(user_email, parts[2])

        # Route: PATCH /mcp/servers/{id}
        if (
            len(parts) == 3
            and parts[0] == "mcp"
            and parts[1] == "servers"
            and http_method == "PATCH"
        ):
            return update_mcp_server(user_email, parts[2], body)

        # Route: DELETE /mcp/servers/{id}
        if (
            len(parts) == 3
            and parts[0] == "mcp"
            and parts[1] == "servers"
            and http_method == "DELETE"
        ):
            return delete_mcp_server(user_email, parts[2])

        # Route: POST /mcp/servers/{id}/refresh
        if (
            len(parts) == 4
            and parts[0] == "mcp"
            and parts[1] == "servers"
            and parts[3] == "refresh"
            and http_method == "POST"
        ):
            return refresh_mcp_tools(user_email, parts[2])

        # Route: GET /mcp/servers/{id}/workspaces
        if (
            len(parts) == 4
            and parts[0] == "mcp"
            and parts[1] == "servers"
            and parts[3] == "workspaces"
            and http_method == "GET"
        ):
            return get_mcp_server_workspaces(user_email, parts[2])

        # ====================================================================
        # MCP Workspace Assignment Routes
        # ====================================================================

        # Route: POST /workspaces/{id}/mcp-servers
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "mcp-servers"
            and http_method == "POST"
        ):
            return assign_mcp_to_workspace(user_email, parts[1], body)

        # Route: GET /workspaces/{id}/mcp-servers
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "mcp-servers"
            and http_method == "GET"
        ):
            return list_workspace_mcp_servers(user_email, parts[1])

        # Route: DELETE /workspaces/{id}/mcp-servers/{mcp_id}
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "mcp-servers"
            and http_method == "DELETE"
        ):
            return remove_mcp_from_workspace(user_email, parts[1], parts[3])

        # Route: GET /chat/mcp-servers
        if parts == ("chat", "mcp-servers") and http_method == "GET":
            return get_chat_available_mcp_servers(user_email)

        # ====================================================================
        # API Key Management Routes
        # ====================================================================

        # Route: POST /api-keys
        if parts == ("api-keys",) and http_method == "POST":
            return create_api_key(user_email, body)

        # Route: GET /api-keys
        if parts == ("api-keys",) and http_method == "GET":
            return list_api_keys(user_email)

        # Route: DELETE /api-keys/{key_id}
        if (
            len(parts) == 2
            and parts[0] == "api-keys"
            and http_method == "DELETE"
        ):
            return revoke_api_key(user_email, parts[1])

        return _error(404, "Not found")

    except ValidationError as e:
        return _error(400, f"Invalid request: {e}")
    except Exception as e:
        logger.error(f"Unhandled error: {http_method} {path}: {e}", exc_info=True)
        return _error(500, f"Internal server error: {type(e).__name__}: {e}")
