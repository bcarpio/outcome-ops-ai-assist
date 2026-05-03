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

    System Prompt:
        GET    /workspaces/{id}/system-prompt     - Get custom system prompt
        PUT    /workspaces/{id}/system-prompt     - Set custom system prompt (admin only)
        DELETE /workspaces/{id}/system-prompt     - Reset to default (admin only)

    Org Management (org_admin only):
        GET    /org/members                       - List all org members
        POST   /org/members                       - Add org member
        PATCH  /org/members/{email}               - Update org role
        DELETE /org/members/{email}               - Remove org member

    Chat:
        GET    /chat/searchable-workspaces        - List workspaces user can query
        GET    /chat/available-prompts            - List workspaces with custom prompts
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
import posixpath
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError, field_validator

from shared.audit_writer import (
    ACTION_API_KEY_CREATED,
    ACTION_API_KEY_REVOKED,
    ACTION_MCP_SERVER_CREATED,
    ACTION_MCP_SERVER_DELETED,
    ACTION_MCP_SERVER_UPDATED,
    ACTION_ORG_MEMBER_ADDED,
    ACTION_ORG_MEMBER_REMOVED,
    ACTION_SYSTEM_PROMPT_RESET,
    ACTION_SYSTEM_PROMPT_UPDATED,
    ACTION_WORKSPACE_BUDGET_UPDATED,
    ACTION_WORKSPACE_CREATED,
    ACTION_WORKSPACE_DELETED,
    ACTION_WORKSPACE_MEMBER_ADDED,
    ACTION_WORKSPACE_MEMBER_REMOVED,
    ACTION_WORKSPACE_REPO_ADDED,
    ACTION_WORKSPACE_REPO_REMOVED,
    ACTION_WORKSPACE_UPDATED,
    extract_request_context,
    log_admin_action,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
ANALYTICS_TABLE = os.environ.get("ANALYTICS_TABLE", "")

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


def _get_audit_table() -> str:
    """Get audit logs table name from AUDIT_TABLE env var."""
    return AUDIT_TABLE


def _get_analytics_table() -> str:
    """Get analytics aggregates table name from ANALYTICS_TABLE env var."""
    return ANALYTICS_TABLE


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
        # Reject null bytes and control characters
        if "\x00" in v or any(ord(c) < 32 for c in v):
            raise ValueError("Filename must not contain null bytes or control characters")
        # Reject path traversal sequences
        if ".." in v or "/" in v or "\\" in v:
            raise ValueError("Filename must not contain path separators or '..' sequences")
        # Validate extension
        allowed_extensions = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".html"}
        ext = v.lower().rsplit(".", 1)[-1] if "." in v else ""
        if f".{ext}" not in allowed_extensions:
            raise ValueError(f"File type .{ext} not supported. Allowed: {', '.join(allowed_extensions)}")
        return v

    @field_validator("folder_path")
    @classmethod
    def validate_folder_path(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        v = v.strip()
        # Reject path traversal sequences
        if ".." in v or "\\" in v:
            raise ValueError("Folder path must not contain '..' sequences or backslashes")
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


def create_workspace(
    user_email: str,
    request: CreateWorkspaceRequest,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Create a new workspace and add creator as admin (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    # Only org admins can create workspaces
    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can create workspaces")

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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_CREATED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"workspace_name": request.name},
        )

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
    user_email: str,
    workspace_id: str,
    request: UpdateWorkspaceRequest,
    audit_ctx: Optional[Dict[str, str]] = None,
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
        changed_fields = []
        now = datetime.now(timezone.utc).isoformat()

        if request.name is not None:
            update_parts.append("#n = :name")
            expr_values[":name"] = request.name
            changed_fields.append("name")
        if request.description is not None:
            update_parts.append("description = :desc")
            expr_values[":desc"] = request.description
            changed_fields.append("description")

        update_parts.append("updated_at = :ua")
        expr_values[":ua"] = now

        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames={"#n": "name"} if request.name else {},
            ExpressionAttributeValues=expr_values,
        )

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_UPDATED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"changed_fields": changed_fields},
        )

        logger.info(f"Updated workspace: {workspace_id}")
        return get_workspace(user_email, workspace_id)
    except ClientError as e:
        logger.error(f"Error updating workspace: {e}")
        return _error(500, "Failed to update workspace")


def delete_workspace(
    user_email: str,
    workspace_id: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

        # Capture the workspace name before deletion so the audit row carries
        # something human-readable.
        workspace_name = ""
        for item in ws_items:
            if item.get("SK") == "METADATA":
                workspace_name = item.get("name", "")
                break

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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_DELETED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"workspace_name": workspace_name},
        )

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
    user_email: str,
    workspace_id: str,
    request: AddMemberRequest,
    audit_ctx: Optional[Dict[str, str]] = None,
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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_MEMBER_ADDED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"member_email": request.email, "role": request.role},
        )

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

        # Check member exists
        existing = _get_membership(table, member_email, workspace_id)
        if not existing:
            return _error(404, "Member not found")

        # Prevent privilege escalation: only org admins can demote workspace admins
        target_is_admin = existing.get("role") == "workspace_admin"
        demoting = request.role != "workspace_admin"
        if target_is_admin and demoting and not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can demote workspace admins")

        # Can't demote the last admin
        if target_is_admin and demoting:
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
                return _error(400, "Cannot demote the last admin of a workspace")

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


def remove_member(
    user_email: str,
    workspace_id: str,
    member_email: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Remove member from workspace (admin only, or self-removal)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        is_admin = _is_workspace_admin(table, user_email, workspace_id)
        is_self = user_email == member_email

        if not is_admin and not is_self:
            return _error(403, "Only workspace admins can remove other members")

        # Check member exists
        existing = _get_membership(table, member_email, workspace_id)
        if not existing:
            return _error(404, "Member not found")

        # Can't remove the last admin from a workspace
        if existing.get("role") == "workspace_admin":
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
                return _error(400, "Cannot remove the last admin from a workspace")

        table.delete_item(
            Key={"PK": f"USER#{member_email}", "SK": f"WS_MEMBER#{workspace_id}"}
        )

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_MEMBER_REMOVED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"member_email": member_email},
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


def add_repo(
    user_email: str,
    workspace_id: str,
    request: AddRepoRequest,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_REPO_ADDED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"repo_name": request.repo_name},
        )

        logger.info(f"Added repo {request.repo_name} to workspace {workspace_id}")
        return _response(201, {"repo": repo_item})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(409, "Repo already exists in workspace")
        logger.error(f"Error adding repo: {e}")
        return _error(500, "Failed to add repo")


def remove_repo(
    user_email: str,
    workspace_id: str,
    repo_name: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

        # Queue cleanup of S3 documents and vector embeddings for this repo
        queue_url = _get_delete_queue_url()
        if queue_url:
            sqs_client = _get_sqs_client()
            sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps({
                    "action": "delete_prefix",
                    "workspace_id": workspace_id,
                    "source": "github",
                    "prefix": repo_name + "/",
                }),
            )
            logger.info(f"Queued cleanup for {workspace_id}/github/{repo_name}/")

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_REPO_REMOVED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"repo_name": repo_name},
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


def add_org_member(
    user_email: str,
    request: AddOrgMemberRequest,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_ORG_MEMBER_ADDED,
            workspace_id="",
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"member_email": request.email, "role": request.org_role},
        )

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


def remove_org_member(
    user_email: str,
    member_email: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_ORG_MEMBER_REMOVED,
            workspace_id="",
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"member_email": member_email},
        )

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


def get_available_prompts(user_email: str) -> Dict:
    """
    Get workspaces with custom system prompts that the user can access.

    Includes workspaces the user is a direct member of and workspaces
    that have shared search access with the user's workspaces.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        is_admin = _is_org_admin(table, user_email)

        # Get user's direct workspace memberships
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
            if ws_id:
                user_workspace_ids.append(ws_id)

        # Collect all workspace IDs to check for prompts
        ws_ids_to_check = set(user_workspace_ids)

        if is_admin:
            # Org admins see all workspaces with prompts
            all_ws = _scan_all_workspaces(table)
            for ws in all_ws:
                ws_id = ws.get("workspace_id")
                if ws_id:
                    ws_ids_to_check.add(ws_id)
        elif user_workspace_ids:
            # Also include workspaces shared with the user's workspaces
            all_ws = _scan_all_workspaces(table)
            for ws in all_ws:
                ws_id = ws.get("workspace_id")
                if ws_id and ws_id not in ws_ids_to_check:
                    allowed = ws.get("search_allowed_workspaces", [])
                    if allowed and any(uws in allowed for uws in user_workspace_ids):
                        ws_ids_to_check.add(ws_id)

        prompts = []
        for ws_id in ws_ids_to_check:
            ws_response = table.get_item(
                Key={"PK": f"WS#{ws_id}", "SK": "METADATA"},
                ProjectionExpression="#n, custom_system_prompt",
                ExpressionAttributeNames={"#n": "name"},
            )
            if "Item" in ws_response:
                ws = ws_response["Item"]
                custom_prompt = ws.get("custom_system_prompt")
                if custom_prompt:
                    prompts.append({
                        "workspace_id": ws_id,
                        "workspace_name": ws.get("name", ""),
                        "prompt_preview": custom_prompt[:200],
                    })

        # Sort by workspace name
        prompts.sort(key=lambda x: x.get("workspace_name", "").lower())

        logger.info("Returning %s available prompts for %s", len(prompts), user_email)
        return _response(200, {"prompts": prompts})
    except ClientError as e:
        logger.error("Error getting available prompts: %s", e)
        return _error(500, "Failed to get available prompts")


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

        # Defense-in-depth: verify resolved key stays within workspace prefix
        expected_prefix = f"workspaces/{workspace_id}/uploads/"
        normalized = posixpath.normpath(s3_key)
        if not normalized.startswith(expected_prefix.rstrip("/")):
            return _error(400, "Invalid file path")

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

        # Sanitize prefix - reject traversal attempts
        if prefix and (".." in prefix or "\\" in prefix):
            return _error(400, "Invalid prefix")

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

        # Sanitize document path - reject traversal attempts
        if ".." in document_path or "\\" in document_path:
            return _error(400, "Invalid document path")

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

    merged_headers = dict(headers)
    merged_headers.setdefault("Content-Type", "application/json")
    merged_headers.setdefault("Accept", "application/json, text/event-stream")

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=merged_headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        # Capture session ID if returned
        session_id = resp.headers.get("Mcp-Session-Id")
        raw = resp.read().decode()
        content_type = resp.headers.get("Content-Type") or ""

        # Handle SSE responses (Streamable HTTP servers may return text/event-stream)
        if "text/event-stream" in content_type:
            for line in raw.splitlines():
                if line.startswith("data: "):
                    raw = line[6:]
                    break

        data = json.loads(raw)
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


def create_mcp_server(
    user_email: str,
    body: Dict,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

    ctx = audit_ctx or {}
    log_admin_action(
        AUDIT_TABLE,
        ACTION_MCP_SERVER_CREATED,
        workspace_id="",
        user_email=user_email,
        source_ip=ctx.get("source_ip", ""),
        user_agent=ctx.get("user_agent", ""),
        details={
            "mcp_id": mcp_id,
            "name": request.name,
            "auth_type": request.auth_type,
        },
    )

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


def update_mcp_server(
    user_email: str,
    mcp_id: str,
    body: Dict,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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
        changed_fields: List[str] = []

        if request.name is not None:
            update_parts.append("#n = :name")
            expr_values[":name"] = request.name
            expr_names["#n"] = "name"
            changed_fields.append("name")
        if request.url is not None:
            update_parts.append("#u = :url")
            expr_values[":url"] = request.url
            expr_names["#u"] = "url"
            changed_fields.append("url")
        if request.auth_type is not None:
            update_parts.append("auth_type = :at")
            expr_values[":at"] = request.auth_type
            if request.auth_type == "none":
                update_parts.append("encrypted_credentials = :ec")
                expr_values[":ec"] = None
            changed_fields.append("auth_type")
        if request.credentials is not None:
            auth = request.auth_type or existing.get("auth_type", "none")
            if auth != "none":
                encrypted = _encrypt_credentials(request.credentials)
                update_parts.append("encrypted_credentials = :ec")
                expr_values[":ec"] = encrypted
            changed_fields.append("credentials")
        if request.is_global is not None:
            update_parts.append("is_global = :ig")
            expr_values[":ig"] = request.is_global
            changed_fields.append("is_global")
        if request.refresh_interval_days is not None:
            update_parts.append("refresh_interval_days = :rid")
            expr_values[":rid"] = request.refresh_interval_days
            changed_fields.append("refresh_interval_days")

        update_kwargs = {
            "Key": {"PK": f"MCP#{mcp_id}", "SK": "METADATA"},
            "UpdateExpression": "SET " + ", ".join(update_parts),
            "ExpressionAttributeValues": expr_values,
        }
        if expr_names:
            update_kwargs["ExpressionAttributeNames"] = expr_names

        table.update_item(**update_kwargs)

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_MCP_SERVER_UPDATED,
            workspace_id="",
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={
                "mcp_id": mcp_id,
                "name": request.name if request.name is not None else existing.get("name", ""),
                "auth_type": request.auth_type if request.auth_type is not None else existing.get("auth_type", ""),
                "changed_fields": changed_fields,
            },
        )

        logger.info(f"[workspace_handler] Updated MCP server: {mcp_id}")
        return get_mcp_server(user_email, mcp_id)
    except ClientError as e:
        logger.error(f"[workspace_handler] Error updating MCP server: {e}", exc_info=True)
        return _error(500, "Failed to update MCP server")


def delete_mcp_server(
    user_email: str,
    mcp_id: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Delete an MCP server and all its assignments."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    if not _is_org_admin(table, user_email):
        return _error(403, "Only org admins can manage MCP servers")

    # Verify it exists
    response = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
    if "Item" not in response:
        return _error(404, "MCP server not found")

    mcp_name = response["Item"].get("name", "")

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

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_MCP_SERVER_DELETED,
            workspace_id="",
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"mcp_id": mcp_id, "name": mcp_name},
        )

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
        logger.info("[workspace_handler] Assigned MCP %s to workspace %s", mcp_id, workspace_id)
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


def create_api_key(
    user_email: str,
    body: Dict,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
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

        # Audit row deliberately excludes the raw key. key_prefix is the
        # standard identifier for revocation lookups.
        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_API_KEY_CREATED,
            workspace_id="",
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={
                "key_id": key_id,
                "name": request.name,
                "expires_at": item.get("expires_at", ""),
                "key_prefix": key_prefix,
            },
        )

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


def revoke_api_key(
    user_email: str,
    key_id: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Revoke an API key (users can only revoke their own keys)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        now = datetime.now(timezone.utc).isoformat()
        update_response = table.update_item(
            Key={"PK": f"USER#{user_email}", "SK": f"API_KEY#{key_id}"},
            UpdateExpression="SET #status = :status, revoked_at = :revoked_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "revoked",
                ":revoked_at": now,
            },
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_OLD",
        )
        old_item = update_response.get("Attributes") or {}

        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_API_KEY_REVOKED,
            workspace_id="",
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={"key_id": key_id, "name": old_item.get("name", "")},
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
# Audit Logging
# ============================================================================


# ============================================================================
# System Prompt Management
# ============================================================================


def handle_get_system_prompt(workspace_id: str, user_email: str) -> Dict:
    """Get the custom system prompt for a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Any workspace member can read the system prompt
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        # Get workspace metadata
        response = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "Workspace not found")

        workspace = response["Item"]
        custom_prompt = workspace.get("custom_system_prompt")

        return _response(200, {
            "custom_system_prompt": custom_prompt,
            "updated_at": workspace.get("custom_prompt_updated_at"),
            "updated_by": workspace.get("custom_prompt_updated_by"),
            "is_default": custom_prompt is None,
        })
    except ClientError as e:
        logger.error(f"Error getting system prompt: {e}")
        return _error(500, "Failed to get system prompt")


def handle_update_system_prompt(
    workspace_id: str,
    user_email: str,
    body: Dict,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Set a custom system prompt for a workspace (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Only workspace admins or org admins can update
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can update the system prompt")

        # Validate request body
        custom_system_prompt = body.get("custom_system_prompt")
        if not custom_system_prompt or not isinstance(custom_system_prompt, str):
            return _error(400, "custom_system_prompt is required and must be a non-empty string")

        custom_system_prompt = custom_system_prompt.strip()
        if not custom_system_prompt:
            return _error(400, "custom_system_prompt must not be empty")

        if len(custom_system_prompt) > 10000:
            return _error(400, "custom_system_prompt must not exceed 10,000 characters")

        # Check workspace exists
        response = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "Workspace not found")

        now = datetime.now(timezone.utc).isoformat()

        # Update workspace metadata
        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            UpdateExpression="SET custom_system_prompt = :p, custom_prompt_updated_at = :t, custom_prompt_updated_by = :u, updated_at = :t",
            ExpressionAttributeValues={
                ":p": custom_system_prompt,
                ":t": now,
                ":u": user_email,
            },
        )

        # Write audit log
        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_SYSTEM_PROMPT_UPDATED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={
                "prompt_length": len(custom_system_prompt),
                "prompt_preview": custom_system_prompt[:200],
            },
        )

        logger.info(f"Updated system prompt for workspace {workspace_id} by {user_email}")
        return _response(200, {
            "custom_system_prompt": custom_system_prompt,
            "updated_at": now,
            "updated_by": user_email,
            "is_default": False,
        })
    except ClientError as e:
        logger.error(f"Error updating system prompt: {e}")
        return _error(500, "Failed to update system prompt")


def handle_reset_system_prompt(
    workspace_id: str,
    user_email: str,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Reset workspace system prompt to default (admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Only workspace admins or org admins can reset
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can reset the system prompt")

        # Check workspace exists
        response = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in response:
            return _error(404, "Workspace not found")

        now = datetime.now(timezone.utc).isoformat()

        # Remove custom prompt attributes
        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            UpdateExpression="REMOVE custom_system_prompt, custom_prompt_updated_at, custom_prompt_updated_by SET updated_at = :t",
            ExpressionAttributeValues={":t": now},
        )

        # Write audit log
        ctx = audit_ctx or {}
        log_admin_action(
            AUDIT_TABLE,
            ACTION_SYSTEM_PROMPT_RESET,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
        )

        logger.info(f"Reset system prompt for workspace {workspace_id} by {user_email}")
        return _response(200, {"is_default": True})
    except ClientError as e:
        logger.error(f"Error resetting system prompt: {e}")
        return _error(500, "Failed to reset system prompt")


# ============================================================================
# Workspace Budget Management
# Each workspace stores an optional ``monthly_budget_usd`` on its METADATA
# item. The cost-alert Lambda uses this per-workspace value when present and
# falls back to the global ``cost_alert_workspace_monthly_usd`` tfvar when
# the field is absent or zero.
# ============================================================================


def _coerce_budget(raw: Any) -> Optional[float]:
    """Parse and validate a budget value. Returns None for unset/zero/invalid."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("monthly_budget_usd must be a number")
    if value < 0:
        raise ValueError("monthly_budget_usd must be >= 0")
    if value > 1_000_000:
        raise ValueError("monthly_budget_usd must be <= 1,000,000")
    if value == 0:
        return None
    return round(value, 2)


def get_workspace_budget(user_email: str, workspace_id: str) -> Dict:
    """Read the per-workspace monthly budget."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        if not _can_access_workspace(table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        resp = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in resp:
            return _error(404, "Workspace not found")

        ws = resp["Item"]
        budget = ws.get("monthly_budget_usd")
        try:
            budget_value = float(budget) if budget is not None else None
        except (TypeError, ValueError):
            budget_value = None

        return _response(200, {
            "workspace_id": workspace_id,
            "monthly_budget_usd": budget_value,
            "updated_at": ws.get("budget_updated_at"),
            "updated_by": ws.get("budget_updated_by"),
            "is_default": budget_value is None,
        })
    except ClientError as e:
        logger.error(f"Error getting workspace budget: {e}")
        return _error(500, "Failed to get workspace budget")


def update_workspace_budget(
    user_email: str,
    workspace_id: str,
    body: Dict,
    audit_ctx: Optional[Dict[str, str]] = None,
) -> Dict:
    """Set or clear the per-workspace monthly budget (org admin only).

    Budgets are a governance control, so edits are restricted to org admins
    — workspace admins can't silently raise their own spending cap.

    Pass ``monthly_budget_usd: 0`` (or null/missing) to clear the override and
    fall back to the org default.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        if not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can update workspace budgets")

        try:
            budget = _coerce_budget(body.get("monthly_budget_usd"))
        except ValueError as e:
            return _error(400, str(e))

        resp = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in resp:
            return _error(404, "Workspace not found")

        previous_budget = resp["Item"].get("monthly_budget_usd")
        # Decimal -> str for ADR-009 round-trip safety in the audit row.
        previous_budget_str = str(previous_budget) if previous_budget is not None else ""

        now = datetime.now(timezone.utc).isoformat()
        ctx = audit_ctx or {}

        if budget is None:
            table.update_item(
                Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
                UpdateExpression=(
                    "REMOVE monthly_budget_usd, budget_updated_by "
                    "SET budget_updated_at = :t, updated_at = :t"
                ),
                ExpressionAttributeValues={":t": now},
            )
            log_admin_action(
                AUDIT_TABLE,
                ACTION_WORKSPACE_BUDGET_UPDATED,
                workspace_id=workspace_id,
                user_email=user_email,
                source_ip=ctx.get("source_ip", ""),
                user_agent=ctx.get("user_agent", ""),
                details={
                    "previous_budget_usd": previous_budget_str,
                    "new_budget_usd": "",
                },
            )
            logger.info(f"Cleared budget for workspace {workspace_id} by {user_email}")
            return _response(200, {
                "workspace_id": workspace_id,
                "monthly_budget_usd": None,
                "updated_at": now,
                "updated_by": user_email,
                "is_default": True,
            })

        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET monthly_budget_usd = :b, budget_updated_at = :t, "
                "budget_updated_by = :u, updated_at = :t"
            ),
            ExpressionAttributeValues={
                ":b": Decimal(str(budget)),
                ":t": now,
                ":u": user_email,
            },
        )
        log_admin_action(
            AUDIT_TABLE,
            ACTION_WORKSPACE_BUDGET_UPDATED,
            workspace_id=workspace_id,
            user_email=user_email,
            source_ip=ctx.get("source_ip", ""),
            user_agent=ctx.get("user_agent", ""),
            details={
                "previous_budget_usd": previous_budget_str,
                "new_budget_usd": str(budget),
            },
        )
        logger.info(f"Set budget=${budget:.2f} for workspace {workspace_id} by {user_email}")
        return _response(200, {
            "workspace_id": workspace_id,
            "monthly_budget_usd": budget,
            "updated_at": now,
            "updated_by": user_email,
            "is_default": False,
        })
    except ClientError as e:
        logger.error(f"Error updating workspace budget: {e}")
        return _error(500, "Failed to update workspace budget")


# ============================================================================
# Main Handler
# ============================================================================


# ============================================================================
# Analytics Operations
# ============================================================================

ANALYTICS_MAX_RANGE_DAYS = 90
ANALYTICS_DEFAULT_RANGE_DAYS = 30


def _parse_analytics_date_range(
    query_params: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse start_date / end_date query params.

    Returns (start_iso, end_iso, error_message). Defaults to last 30 days;
    enforces the 90-day maximum range.
    """
    now = datetime.now(timezone.utc)
    end_raw = query_params.get("end_date")
    start_raw = query_params.get("start_date")

    try:
        if end_raw:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        else:
            end_dt = now

        if start_raw:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        else:
            start_dt = end_dt - timedelta(days=ANALYTICS_DEFAULT_RANGE_DAYS)
    except ValueError as e:
        return None, None, f"Invalid date format: {e}"

    if start_dt > end_dt:
        return None, None, "start_date must be before end_date"

    if (end_dt - start_dt).days > ANALYTICS_MAX_RANGE_DAYS:
        return None, None, f"Date range exceeds max of {ANALYTICS_MAX_RANGE_DAYS} days"

    return start_dt.isoformat(), end_dt.isoformat(), None


def _get_user_workspace_ids(table, user_email: str) -> List[str]:
    """Return the list of workspace IDs the user belongs to."""
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"USER#{user_email}",
            ":sk": "WS_MEMBER#",
        },
    )
    return [m["workspace_id"] for m in response.get("Items", []) if m.get("workspace_id")]


def _agg_num(item: Dict, key: str) -> float:
    """Decimal-tolerant accessor for aggregate numeric fields."""
    raw = item.get(key, 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _agg_int(item: Dict, key: str) -> int:
    raw = item.get(key, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _query_aggregates(
    analytics_table,
    workspace_id: str,
    start_date: str,
    end_date: str,
    prefixes: List[str],
) -> List[Dict]:
    """Query the analytics aggregates table for a workspace and date range.

    Issues one Query per SK prefix (DT/DM/DU) to avoid over-reading. The
    prefix bound ``{prefix}#{end_date}~`` is inclusive of the end date because
    ``~`` (0x7E) sorts after any ASCII character a real SK would end with.
    """
    items: List[Dict] = []
    for prefix in prefixes:
        query_kwargs: Dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk AND SK BETWEEN :s AND :e",
            "ExpressionAttributeValues": {
                ":pk": f"WS#{workspace_id}",
                ":s": f"{prefix}#{start_date}",
                ":e": f"{prefix}#{end_date}~",
            },
        }
        while True:
            response = analytics_table.query(**query_kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def _parse_sk_date_and_suffix(sk: str) -> Tuple[str, str]:
    """Return (date, suffix) from a rollup SK like ``DT#2026-04-01`` or
    ``DM#2026-04-01#us.anthropic.sonnet``. If there is no suffix, an empty
    string is returned for it.
    """
    parts = sk.split("#", 2)
    if len(parts) < 2:
        return "", ""
    date = parts[1]
    suffix = parts[2] if len(parts) > 2 else ""
    return date, suffix


def _get_workspace_names(table, workspace_ids: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for ws_id in set(workspace_ids):
        try:
            resp = table.get_item(Key={"PK": f"WS#{ws_id}", "SK": "METADATA"})
            if "Item" in resp:
                names[ws_id] = resp["Item"].get("name") or ws_id
        except ClientError:
            continue
    return names


def _date_only(iso: str) -> str:
    return iso[:10] if iso else ""


def get_analytics_budgets(user_email: str) -> Dict:
    """Return per-workspace budgets alongside month-to-date spend (org admin only).

    Used by the admin budgets management page. Combines workspace metadata
    (name + current budget override) with the DT# daily-totals rollups for
    the current calendar month so the UI can show actual vs budget side by
    side without needing multiple round trips.
    """
    analytics_table_name = _get_analytics_table()
    if not analytics_table_name:
        return _error(500, "Analytics table not configured")

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    analytics_table = dynamodb.Table(analytics_table_name)

    try:
        if not _is_org_admin(ws_table, user_email):
            return _error(403, "Org admin access required")

        all_ws = _scan_all_workspaces(ws_table)

        now = datetime.now(timezone.utc)
        month_start = now.strftime("%Y-%m-01")
        month_end = now.strftime("%Y-%m-%d")
        month_key = now.strftime("%Y-%m")

        items = []
        for ws in all_ws:
            wid = ws.get("workspace_id")
            if not wid:
                continue

            raw_budget = ws.get("monthly_budget_usd")
            try:
                budget = float(raw_budget) if raw_budget is not None else None
            except (TypeError, ValueError):
                budget = None

            # Sum current-month DT# rollups for this workspace.
            mtd_spend = 0.0
            try:
                resp = analytics_table.query(
                    KeyConditionExpression="PK = :pk AND SK BETWEEN :s AND :e",
                    ExpressionAttributeValues={
                        ":pk": f"WS#{wid}",
                        ":s": f"DT#{month_start}",
                        ":e": f"DT#{month_end}~",
                    },
                    ProjectionExpression="cost_usd",
                )
                for item in resp.get("Items", []):
                    try:
                        mtd_spend += float(item.get("cost_usd", 0))
                    except (TypeError, ValueError):
                        continue
            except ClientError as e:
                logger.warning(
                    f"[analytics-budgets] failed to read MTD for {wid}: {e}"
                )

            items.append({
                "workspace_id": wid,
                "name": ws.get("name") or wid,
                "monthly_budget_usd": budget,
                "is_default": budget is None,
                "mtd_spend_usd": round(mtd_spend, 4),
                "updated_at": ws.get("budget_updated_at"),
                "updated_by": ws.get("budget_updated_by"),
            })

        items.sort(key=lambda x: x["mtd_spend_usd"], reverse=True)

        return _response(200, {
            "workspaces": items,
            "month": month_key,
        })
    except ClientError as e:
        logger.error(f"Error listing workspace budgets: {e}", exc_info=True)
        return _error(500, "Failed to list workspace budgets")


def get_analytics_summary(user_email: str, query_params: Dict[str, Any]) -> Dict:
    """Return org-level aggregated analytics by reading daily roll-ups."""
    analytics_table_name = _get_analytics_table()
    if not analytics_table_name:
        return _error(500, "Analytics table not configured")

    start_iso, end_iso, err = _parse_analytics_date_range(query_params)
    if err:
        return _error(400, err)

    start_date = _date_only(start_iso)
    end_date = _date_only(end_iso)

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    analytics_table = dynamodb.Table(analytics_table_name)

    try:
        is_admin = _is_org_admin(ws_table, user_email)
        filter_workspace_id = query_params.get("workspace_id")

        if filter_workspace_id:
            if not _can_access_workspace(ws_table, user_email, filter_workspace_id):
                return _error(403, "Not a member of this workspace")
            workspace_ids = [filter_workspace_id]
            is_ws_admin = _is_workspace_admin(ws_table, user_email, filter_workspace_id)
        elif is_admin:
            all_ws = _scan_all_workspaces(ws_table)
            workspace_ids = [ws["workspace_id"] for ws in all_ws if ws.get("workspace_id")]
            is_ws_admin = True
        else:
            workspace_ids = _get_user_workspace_ids(ws_table, user_email)
            is_ws_admin = any(
                _is_workspace_admin(ws_table, user_email, wid) for wid in workspace_ids
            )

        workspace_names = _get_workspace_names(ws_table, workspace_ids)

        # Admins see aggregated totals; regular members see only their own
        # usage, which means reading per-user rollups instead of day totals.
        show_all_users = is_admin or is_ws_admin
        prefixes = ["DT", "DM"] if show_all_users else ["DU"]

        total_cost = 0.0
        total_queries = 0
        total_input_tokens = 0
        total_output_tokens = 0
        by_model: Dict[str, Dict[str, float]] = {}
        by_workspace: Dict[str, Dict[str, Any]] = {}
        by_date: Dict[str, Dict[str, float]] = {}

        for ws_id in workspace_ids:
            items = _query_aggregates(
                analytics_table, ws_id, start_date, end_date, prefixes
            )
            for item in items:
                sk = item.get("SK", "")
                date, suffix = _parse_sk_date_and_suffix(sk)
                cost = _agg_num(item, "cost_usd")
                queries = _agg_int(item, "queries")
                in_tokens = _agg_int(item, "input_tokens")
                out_tokens = _agg_int(item, "output_tokens")

                if sk.startswith("DT#") or sk.startswith("DU#"):
                    # Day totals (admins) or per-user-per-day (members).
                    # Either way these represent queries for one day, so they
                    # feed workspace / daily_trend totals without double-count.
                    if sk.startswith("DU#"):
                        email = suffix
                        if email != user_email:
                            continue  # members only see themselves
                    total_cost += cost
                    total_queries += queries
                    total_input_tokens += in_tokens
                    total_output_tokens += out_tokens

                    ws_bucket = by_workspace.setdefault(
                        ws_id,
                        {
                            "workspace_id": ws_id,
                            "name": workspace_names.get(ws_id, ws_id),
                            "queries": 0,
                            "cost_usd": 0.0,
                        },
                    )
                    ws_bucket["queries"] += queries
                    ws_bucket["cost_usd"] += cost

                    if date:
                        day_bucket = by_date.setdefault(
                            date, {"queries": 0, "cost_usd": 0.0}
                        )
                        day_bucket["queries"] += queries
                        day_bucket["cost_usd"] += cost

                elif sk.startswith("DM#"):
                    # Admin view only; members don't query DM rows.
                    model_id = suffix or "unknown"
                    bucket = by_model.setdefault(
                        model_id, {"queries": 0, "cost_usd": 0.0}
                    )
                    bucket["queries"] += queries
                    bucket["cost_usd"] += cost

        by_model_out = {
            model: {"queries": v["queries"], "cost_usd": round(v["cost_usd"], 4)}
            for model, v in by_model.items()
        }
        by_workspace_out = sorted(
            (
                {
                    "workspace_id": v["workspace_id"],
                    "name": v["name"],
                    "queries": v["queries"],
                    "cost_usd": round(v["cost_usd"], 4),
                }
                for v in by_workspace.values()
            ),
            key=lambda x: x["cost_usd"],
            reverse=True,
        )
        daily_trend = sorted(
            (
                {"date": d, "queries": v["queries"], "cost_usd": round(v["cost_usd"], 4)}
                for d, v in by_date.items()
            ),
            key=lambda x: x["date"],
        )

        return _response(200, {
            "total_cost_usd": round(total_cost, 4),
            "total_queries": total_queries,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "by_model": by_model_out,
            "by_workspace": by_workspace_out,
            "daily_trend": daily_trend,
            "start_date": start_iso,
            "end_date": end_iso,
        })
    except ClientError as e:
        logger.error(f"Error getting analytics summary: {e}", exc_info=True)
        return _error(500, "Failed to get analytics summary")


def get_analytics_users(user_email: str, query_params: Dict[str, Any]) -> Dict:
    """Return top users by query count, reading DU rollups."""
    analytics_table_name = _get_analytics_table()
    if not analytics_table_name:
        return _error(500, "Analytics table not configured")

    start_iso, end_iso, err = _parse_analytics_date_range(query_params)
    if err:
        return _error(400, err)

    try:
        limit = int(query_params.get("limit", "20"))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    start_date = _date_only(start_iso)
    end_date = _date_only(end_iso)

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    analytics_table = dynamodb.Table(analytics_table_name)

    try:
        is_admin = _is_org_admin(ws_table, user_email)
        filter_workspace_id = query_params.get("workspace_id")

        if filter_workspace_id:
            if not _is_workspace_admin(ws_table, user_email, filter_workspace_id):
                return _error(403, "Workspace admin access required")
            workspace_ids = [filter_workspace_id]
        elif is_admin:
            all_ws = _scan_all_workspaces(ws_table)
            workspace_ids = [ws["workspace_id"] for ws in all_ws if ws.get("workspace_id")]
        else:
            user_ws_ids = _get_user_workspace_ids(ws_table, user_email)
            workspace_ids = [
                wid for wid in user_ws_ids
                if _is_workspace_admin(ws_table, user_email, wid)
            ]
            if not workspace_ids:
                return _error(403, "Admin access required")

        users: Dict[str, Dict[str, Any]] = {}
        for ws_id in workspace_ids:
            items = _query_aggregates(
                analytics_table, ws_id, start_date, end_date, ["DU"]
            )
            for item in items:
                _, email = _parse_sk_date_and_suffix(item.get("SK", ""))
                if not email:
                    continue
                bucket = users.setdefault(
                    email,
                    {
                        "email": email,
                        "queries": 0,
                        "cost_usd": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "last_active": "",
                    },
                )
                bucket["queries"] += _agg_int(item, "queries")
                bucket["cost_usd"] += _agg_num(item, "cost_usd")
                bucket["input_tokens"] += _agg_int(item, "input_tokens")
                bucket["output_tokens"] += _agg_int(item, "output_tokens")
                last = item.get("last_active_ts", "")
                if last and last > bucket["last_active"]:
                    bucket["last_active"] = last

        sorted_users = sorted(users.values(), key=lambda u: u["queries"], reverse=True)[:limit]
        for u in sorted_users:
            u["cost_usd"] = round(u["cost_usd"], 4)

        return _response(200, {"users": sorted_users})
    except ClientError as e:
        logger.error(f"Error getting analytics users: {e}", exc_info=True)
        return _error(500, "Failed to get analytics users")


def get_analytics_workspace(
    user_email: str, workspace_id: str, query_params: Dict[str, Any]
) -> Dict:
    """Return detailed analytics for a single workspace from aggregates."""
    analytics_table_name = _get_analytics_table()
    if not analytics_table_name:
        return _error(500, "Analytics table not configured")

    start_iso, end_iso, err = _parse_analytics_date_range(query_params)
    if err:
        return _error(400, err)

    start_date = _date_only(start_iso)
    end_date = _date_only(end_iso)

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    analytics_table = dynamodb.Table(analytics_table_name)

    try:
        if not _can_access_workspace(ws_table, user_email, workspace_id):
            return _error(403, "Not a member of this workspace")

        ws_response = ws_table.get_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"}
        )
        if "Item" not in ws_response:
            return _error(404, "Workspace not found")
        workspace_name = ws_response["Item"].get("name") or workspace_id

        items = _query_aggregates(
            analytics_table, workspace_id, start_date, end_date, ["DT", "DM", "DU"]
        )

        total_cost = 0.0
        total_queries = 0
        total_duration_ms = 0
        refusal_count = 0
        error_count = 0
        by_user: Dict[str, Dict[str, Any]] = {}
        by_model: Dict[str, Dict[str, float]] = {}
        by_date: Dict[str, Dict[str, float]] = {}

        for item in items:
            sk = item.get("SK", "")
            date, suffix = _parse_sk_date_and_suffix(sk)
            cost = _agg_num(item, "cost_usd")
            queries = _agg_int(item, "queries")

            if sk.startswith("DT#"):
                total_cost += cost
                total_queries += queries
                total_duration_ms += _agg_int(item, "total_duration_ms")
                refusal_count += _agg_int(item, "refusal_count")
                error_count += _agg_int(item, "error_count")
                if date:
                    bucket = by_date.setdefault(
                        date, {"queries": 0, "cost_usd": 0.0}
                    )
                    bucket["queries"] += queries
                    bucket["cost_usd"] += cost
            elif sk.startswith("DM#"):
                model_id = suffix or "unknown"
                bucket = by_model.setdefault(
                    model_id, {"queries": 0, "cost_usd": 0.0}
                )
                bucket["queries"] += queries
                bucket["cost_usd"] += cost
            elif sk.startswith("DU#"):
                email = suffix
                if not email:
                    continue
                bucket = by_user.setdefault(
                    email, {"email": email, "queries": 0, "cost_usd": 0.0}
                )
                bucket["queries"] += queries
                bucket["cost_usd"] += cost

        avg_duration_ms = int(total_duration_ms / total_queries) if total_queries else 0

        by_user_out = sorted(
            (
                {
                    "email": v["email"],
                    "queries": v["queries"],
                    "cost_usd": round(v["cost_usd"], 4),
                }
                for v in by_user.values()
            ),
            key=lambda x: x["cost_usd"],
            reverse=True,
        )
        by_model_out = {
            model: {"queries": v["queries"], "cost_usd": round(v["cost_usd"], 4)}
            for model, v in by_model.items()
        }
        daily_trend = sorted(
            (
                {"date": d, "queries": v["queries"], "cost_usd": round(v["cost_usd"], 4)}
                for d, v in by_date.items()
            ),
            key=lambda x: x["date"],
        )

        return _response(200, {
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "total_cost_usd": round(total_cost, 4),
            "total_queries": total_queries,
            "avg_duration_ms": avg_duration_ms,
            "refusal_count": refusal_count,
            "error_count": error_count,
            "start_date": start_iso,
            "end_date": end_iso,
            "by_user": by_user_out,
            "by_model": by_model_out,
            "daily_trend": daily_trend,
        })
    except ClientError as e:
        logger.error(f"Error getting workspace analytics: {e}", exc_info=True)
        return _error(500, "Failed to get workspace analytics")


# ============================================================================
# Audit Query Endpoints (chunk 05)
# ============================================================================

# /audit/events server-side caps. The no-filter org-admin path is the most
# expensive, so we keep it small to discourage uncapped scans.
AUDIT_EVENTS_MAX_LIMIT = 200
AUDIT_EVENTS_DEFAULT_LIMIT = 50
AUDIT_EVENTS_NO_FILTER_CAP = 50


def _decimal_to_native(value: Any) -> Any:
    """Recursively convert Decimal -> int/float for JSON-friendly output."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if Decimal(as_int) == value else float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_native(v) for v in value]
    if isinstance(value, set):
        return list(value)
    return value


def _encode_audit_cursor(last_evaluated_key: Optional[Dict]) -> Optional[str]:
    """base64-encode a DDB LastEvaluatedKey so it round-trips through HTTP."""
    if not last_evaluated_key:
        return None
    try:
        return base64.urlsafe_b64encode(
            json.dumps(_decimal_to_native(last_evaluated_key)).encode("utf-8")
        ).decode("ascii")
    except (TypeError, ValueError):
        return None


def _decode_audit_cursor(cursor: str) -> Optional[Dict]:
    if not cursor:
        return None
    try:
        return json.loads(
            base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        )
    except (ValueError, TypeError):
        return None


def _audit_event_from_item(item: Dict) -> Dict:
    """Project a raw audit row into the public event JSON envelope."""
    return {
        "request_id": item.get("request_id", ""),
        "timestamp": item.get("timestamp", ""),
        "category": item.get("category", "bedrock"),
        "action": item.get("action", ""),
        "user_email": item.get("user_email", ""),
        "workspace_id": item.get("workspace_id", ""),
        "source_ip": item.get("source_ip", ""),
        "user_agent": item.get("user_agent", ""),
        "details": _decimal_to_native(item.get("details") or {}),
    }


def _query_admin_aggregates(
    analytics_table,
    workspace_id: str,
    start_date: str,
    end_date: str,
) -> List[Dict]:
    """Read DA / DC / DCU rollups for one workspace and a date range."""
    return _query_aggregates(
        analytics_table,
        workspace_id,
        start_date,
        end_date,
        ["DA", "DC", "DCU"],
    )


# Cap for the on-demand fallback that scans raw audit rows when aggregates
# are empty. Keeps the org-admin "I just deployed" experience snappy without
# letting a multi-month range racks up read units.
AUDIT_SUMMARY_FALLBACK_SCAN_CAP = 1000


def _summarize_admin_events(items: List[Dict]) -> Dict[str, Any]:
    """Build the get_audit_summary response shape from raw admin-row items.

    Used as a fallback when the analytics-aggregator hasn't emitted any
    DA/DC/DCU rows yet (fresh deploy, pre-chunk-02 historical rows, or
    while a stream re-drive is catching up). Always-correct but slower
    than reading the rollups.
    """
    actions_total = 0
    by_action: Dict[str, int] = {}
    by_workspace: Dict[str, int] = {}
    by_actor: Dict[str, Dict[str, Any]] = {}
    by_date: Dict[str, int] = {}

    for item in items:
        action = item.get("action") or ""
        if not action:
            # Skip rows that aren't admin-shaped (legacy bedrock rows that
            # slipped through the category filter on a Scan).
            continue
        ws_id = item.get("workspace_id") or ""
        ts = item.get("timestamp") or ""
        date = ts[:10] if ts else ""
        email = (item.get("user_email") or "").lower()

        actions_total += 1
        by_action[action] = by_action.get(action, 0) + 1
        if ws_id:
            by_workspace[ws_id] = by_workspace.get(ws_id, 0) + 1
        if date:
            by_date[date] = by_date.get(date, 0) + 1
        if email:
            bucket = by_actor.setdefault(
                email, {"user_email": email, "count": 0, "last_active": ""}
            )
            bucket["count"] += 1
            if ts and ts > bucket["last_active"]:
                bucket["last_active"] = ts

    return {
        "actions_total": actions_total,
        "by_action": by_action,
        "by_workspace": by_workspace,
        "by_actor": by_actor,
        "by_date": by_date,
    }


def _scan_admin_events_for_range(
    audit_table,
    start_date: str,
    end_date: str,
    limit: int = AUDIT_SUMMARY_FALLBACK_SCAN_CAP,
) -> List[Dict]:
    """Capped scan of admin-category audit rows in a date range."""
    items: List[Dict] = []
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "category = :cat AND #ts BETWEEN :s AND :e",
        "ExpressionAttributeNames": {"#ts": "timestamp"},
        "ExpressionAttributeValues": {
            ":cat": "admin",
            ":s": start_date,
            ":e": f"{end_date}~",
        },
        "Limit": limit,
    }
    try:
        response = audit_table.scan(**scan_kwargs)
        items.extend(response.get("Items", []) or [])
    except ClientError as e:
        logger.warning(
            f"[workspace-management] audit fallback scan failed: {e}"
        )
    return items


def get_audit_summary(user_email: str, query_params: Dict[str, Any]) -> Dict:
    """Aggregated audit counts across all workspaces (org admin only).

    Reads DA / DC / DCU rollup rows produced by the analytics-aggregator. An
    empty range returns zeros and empty arrays - never 500.
    """
    analytics_table_name = _get_analytics_table()
    if not analytics_table_name:
        return _error(500, "Analytics table not configured")

    start_iso, end_iso, err = _parse_analytics_date_range(query_params)
    if err:
        return _error(400, err)

    start_date = _date_only(start_iso)
    end_date = _date_only(end_iso)

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    analytics_table = dynamodb.Table(analytics_table_name)

    try:
        if not _is_org_admin(ws_table, user_email):
            return _error(403, "Org admin access required")

        all_ws = _scan_all_workspaces(ws_table)
        workspace_ids = [w["workspace_id"] for w in all_ws if w.get("workspace_id")]
        workspace_names = _get_workspace_names(ws_table, workspace_ids)

        actions_total = 0
        by_action: Dict[str, int] = {}
        by_workspace: Dict[str, int] = {}
        by_actor: Dict[str, Dict[str, Any]] = {}
        by_date: Dict[str, int] = {}

        for ws_id in workspace_ids:
            items = _query_admin_aggregates(
                analytics_table, ws_id, start_date, end_date
            )
            for item in items:
                sk = item.get("SK", "")
                date, suffix = _parse_sk_date_and_suffix(sk)
                count = _agg_int(item, "actions_total")

                if sk.startswith("DA#"):
                    actions_total += count
                    by_workspace[ws_id] = by_workspace.get(ws_id, 0) + count
                    if date:
                        by_date[date] = by_date.get(date, 0) + count
                elif sk.startswith("DC#"):
                    action = suffix or "UNKNOWN"
                    by_action[action] = by_action.get(action, 0) + count
                elif sk.startswith("DCU#"):
                    email = suffix
                    if not email:
                        continue
                    bucket = by_actor.setdefault(
                        email,
                        {"user_email": email, "count": 0, "last_active": ""},
                    )
                    bucket["count"] += count
                    last_action_at = item.get("last_action_at") or ""
                    if last_action_at and last_action_at > bucket["last_active"]:
                        bucket["last_active"] = last_action_at

        # Fallback: if the aggregator hasn't produced any rollups yet (fresh
        # deploy, historical pre-chunk-02 rows, or in-flight stream re-drive)
        # compute the same shape on-demand from the raw audit table. Capped
        # scan; bounded cost. Self-healing as new admin actions flow.
        fallback_used = False
        if actions_total == 0:
            audit_table_name = _get_audit_table()
            if audit_table_name:
                audit_table = dynamodb.Table(audit_table_name)
                raw_items = _scan_admin_events_for_range(
                    audit_table, start_date, end_date
                )
                if raw_items:
                    fallback_used = True
                    fb = _summarize_admin_events(raw_items)
                    actions_total = fb["actions_total"]
                    by_action = fb["by_action"]
                    by_workspace = fb["by_workspace"]
                    by_actor = fb["by_actor"]
                    by_date = fb["by_date"]

        actions_by_type = sorted(
            ({"action": a, "count": c} for a, c in by_action.items()),
            key=lambda x: x["count"],
            reverse=True,
        )
        actions_by_workspace = sorted(
            (
                {
                    "workspace_id": wid,
                    "workspace_name": workspace_names.get(wid, wid),
                    "count": c,
                }
                for wid, c in by_workspace.items()
            ),
            key=lambda x: x["count"],
            reverse=True,
        )
        top_actors = sorted(by_actor.values(), key=lambda x: x["count"], reverse=True)[:25]
        trend = sorted(
            ({"date": d, "count": c} for d, c in by_date.items()),
            key=lambda x: x["date"],
        )

        return _response(200, {
            "actions_total": actions_total,
            "actions_by_type": actions_by_type,
            "actions_by_workspace": actions_by_workspace,
            "top_actors": top_actors,
            "trend": trend,
            "start_date": start_iso,
            "end_date": end_iso,
            "source": "raw" if fallback_used else "aggregates",
        })
    except ClientError as e:
        logger.error(f"[workspace-management] Error in audit summary: {e}", exc_info=True)
        return _error(500, "Failed to get audit summary")


def get_audit_workspace(
    user_email: str, workspace_id: str, query_params: Dict[str, Any]
) -> Dict:
    """Per-workspace audit summary (org-admin or workspace-admin)."""
    analytics_table_name = _get_analytics_table()
    if not analytics_table_name:
        return _error(500, "Analytics table not configured")

    start_iso, end_iso, err = _parse_analytics_date_range(query_params)
    if err:
        return _error(400, err)
    start_date = _date_only(start_iso)
    end_date = _date_only(end_iso)

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    analytics_table = dynamodb.Table(analytics_table_name)

    try:
        if not _is_workspace_admin(ws_table, user_email, workspace_id):
            return _error(403, "Workspace admin access required")

        ws_resp = ws_table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        if "Item" not in ws_resp:
            return _error(404, "Workspace not found")
        workspace_name = ws_resp["Item"].get("name") or workspace_id

        items = _query_admin_aggregates(
            analytics_table, workspace_id, start_date, end_date
        )

        actions_total = 0
        by_action: Dict[str, int] = {}
        by_actor: Dict[str, Dict[str, Any]] = {}
        by_date: Dict[str, int] = {}

        for item in items:
            sk = item.get("SK", "")
            date, suffix = _parse_sk_date_and_suffix(sk)
            count = _agg_int(item, "actions_total")
            if sk.startswith("DA#"):
                actions_total += count
                if date:
                    by_date[date] = by_date.get(date, 0) + count
            elif sk.startswith("DC#"):
                action = suffix or "UNKNOWN"
                by_action[action] = by_action.get(action, 0) + count
            elif sk.startswith("DCU#"):
                email = suffix
                if not email:
                    continue
                bucket = by_actor.setdefault(
                    email,
                    {"user_email": email, "count": 0, "last_active": ""},
                )
                bucket["count"] += count
                last_action_at = item.get("last_action_at") or ""
                if last_action_at and last_action_at > bucket["last_active"]:
                    bucket["last_active"] = last_action_at

        return _response(200, {
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "actions_total": actions_total,
            "actions_by_type": sorted(
                ({"action": a, "count": c} for a, c in by_action.items()),
                key=lambda x: x["count"],
                reverse=True,
            ),
            "top_actors": sorted(
                by_actor.values(), key=lambda x: x["count"], reverse=True
            )[:25],
            "trend": sorted(
                ({"date": d, "count": c} for d, c in by_date.items()),
                key=lambda x: x["date"],
            ),
            "start_date": start_iso,
            "end_date": end_iso,
        })
    except ClientError as e:
        logger.error(f"[workspace-management] Error in audit workspace: {e}", exc_info=True)
        return _error(500, "Failed to get workspace audit summary")


def _parse_audit_limit(raw: Optional[str], default: int) -> int:
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, AUDIT_EVENTS_MAX_LIMIT)


def _audit_sk_range(start_date: str, end_date: str) -> Tuple[str, str]:
    """SK lexicographic bounds for an inclusive date range. Audit SKs are
    `<iso_timestamp>#<request_id>`, so prefixing with the date works."""
    return start_date, f"{end_date}~"


def get_audit_events(user_email: str, query_params: Dict[str, Any]) -> Dict:
    """Paginated raw audit events with optional filters."""
    audit_table_name = _get_audit_table()
    if not audit_table_name:
        return _error(500, "Audit table not configured")

    start_iso, end_iso, err = _parse_analytics_date_range(query_params)
    if err:
        return _error(400, err)

    workspace_id = (query_params.get("workspace_id") or "").strip()
    user_filter = (query_params.get("user_email") or "").strip()
    action_filter = (query_params.get("action") or "").strip()
    category_filter = (query_params.get("category") or "").strip()
    cursor = (query_params.get("cursor") or "").strip()

    sk_start, sk_end = _audit_sk_range(_date_only(start_iso), _date_only(end_iso))

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    audit_table = dynamodb.Table(audit_table_name)

    is_org_admin = _is_org_admin(ws_table, user_email)

    if workspace_id:
        if not _is_workspace_admin(ws_table, user_email, workspace_id):
            return _error(403, "Workspace admin access required")
        limit = _parse_audit_limit(query_params.get("limit"), AUDIT_EVENTS_DEFAULT_LIMIT)
        query_kwargs: Dict[str, Any] = {
            "KeyConditionExpression": (
                "PK = :pk AND SK BETWEEN :s AND :e"
            ),
            "ExpressionAttributeValues": {
                ":pk": f"WS#{workspace_id}",
                ":s": sk_start,
                ":e": sk_end,
            },
            "Limit": limit,
            "ScanIndexForward": False,
        }
    elif user_filter:
        if not is_org_admin:
            return _error(403, "Org admin access required to query by user")
        limit = _parse_audit_limit(query_params.get("limit"), AUDIT_EVENTS_DEFAULT_LIMIT)
        query_kwargs = {
            "IndexName": "GSI1",
            "KeyConditionExpression": (
                "GSI1PK = :pk AND GSI1SK BETWEEN :s AND :e"
            ),
            "ExpressionAttributeValues": {
                ":pk": f"USER#{user_filter}",
                ":s": sk_start,
                ":e": sk_end,
            },
            "Limit": limit,
            "ScanIndexForward": False,
        }
    else:
        if not is_org_admin:
            return _error(403, "Org admin access required for unfiltered audit query")
        # Capped no-filter scan, last-resort path. Scan does not honor the
        # SK BETWEEN bounds (no KeyConditionExpression) so the date range is
        # applied as a FilterExpression on the timestamp attribute below.
        limit = min(
            _parse_audit_limit(query_params.get("limit"), AUDIT_EVENTS_NO_FILTER_CAP),
            AUDIT_EVENTS_NO_FILTER_CAP,
        )
        query_kwargs = {
            "Limit": limit,
            "ExpressionAttributeValues": {
                ":s": sk_start,
                ":e": sk_end,
            },
            "ExpressionAttributeNames": {"#ts": "timestamp"},
        }

    # Build optional FilterExpression on action / category. The Scan path
    # also gets a baseline timestamp filter (Query paths already enforce the
    # date range via KeyConditionExpression).
    filters = []
    if not (workspace_id or user_filter):
        filters.append("#ts BETWEEN :s AND :e")
    if action_filter:
        query_kwargs["ExpressionAttributeValues"] = {
            **query_kwargs.get("ExpressionAttributeValues", {}),
            ":action": action_filter,
        }
        filters.append("#a = :action")
        query_kwargs.setdefault("ExpressionAttributeNames", {})["#a"] = "action"
    if category_filter:
        query_kwargs["ExpressionAttributeValues"] = {
            **query_kwargs.get("ExpressionAttributeValues", {}),
            ":category": category_filter,
        }
        filters.append("category = :category")
    if filters:
        query_kwargs["FilterExpression"] = " AND ".join(filters)

    if cursor:
        decoded = _decode_audit_cursor(cursor)
        if decoded:
            query_kwargs["ExclusiveStartKey"] = decoded

    try:
        if workspace_id or user_filter:
            response = audit_table.query(**query_kwargs)
        else:
            response = audit_table.scan(**query_kwargs)

        items = response.get("Items", []) or []
        # Query paths come back in SK order (newest-first via ScanIndexForward
        # = False). Scan returns items in DDB physical order, so sort here so
        # the UI always renders newest-first regardless of branch.
        items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
        events = [_audit_event_from_item(item) for item in items]
        next_cursor = _encode_audit_cursor(response.get("LastEvaluatedKey"))

        return _response(200, {
            "events": events,
            "next_cursor": next_cursor,
            "limit": limit,
            "start_date": start_iso,
            "end_date": end_iso,
        })
    except ClientError as e:
        logger.error(f"[workspace-management] Error in audit events: {e}", exc_info=True)
        return _error(500, "Failed to list audit events")


def get_audit_event(user_email: str, workspace_id: str, request_id: str) -> Dict:
    """Single-event detail fetch for the dashboard drawer.

    The audit SK is `<iso_timestamp>#<request_id>`, so we don't have the
    timestamp from the URL - query the row by PK + ends-with via Query and
    surface the matching item.
    """
    audit_table_name = _get_audit_table()
    if not audit_table_name:
        return _error(500, "Audit table not configured")

    dynamodb = _get_dynamodb()
    ws_table = dynamodb.Table(_get_workspaces_table())
    audit_table = dynamodb.Table(audit_table_name)

    try:
        if not _is_workspace_admin(ws_table, user_email, workspace_id):
            return _error(403, "Workspace admin access required")

        # SK has the request_id as a suffix; we Query by PK and filter.
        response = audit_table.query(
            KeyConditionExpression="PK = :pk",
            FilterExpression="request_id = :rid",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":rid": request_id,
            },
            Limit=1,
        )
        items = response.get("Items", []) or []
        if not items:
            return _error(404, "Audit event not found")

        event = _audit_event_from_item(items[0])
        return _response(200, {"event": event})
    except ClientError as e:
        logger.error(f"[workspace-management] Error in audit event detail: {e}", exc_info=True)
        return _error(500, "Failed to get audit event")


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

    # Capture source_ip / user_agent once per invocation; threaded to mutating
    # endpoints so admin audit rows carry attribution data.
    audit_ctx = extract_request_context(event)

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
            return create_workspace(user_email, request, audit_ctx)

        # Route: GET /workspaces
        if parts == ("workspaces",) and http_method == "GET":
            return list_workspaces(user_email)

        # Route: GET /workspaces/{id}
        if len(parts) == 2 and parts[0] == "workspaces" and http_method == "GET":
            return get_workspace(user_email, parts[1])

        # Route: PATCH /workspaces/{id}
        if len(parts) == 2 and parts[0] == "workspaces" and http_method == "PATCH":
            request = UpdateWorkspaceRequest(**body)
            return update_workspace(user_email, parts[1], request, audit_ctx)

        # Route: DELETE /workspaces/{id}
        if len(parts) == 2 and parts[0] == "workspaces" and http_method == "DELETE":
            return delete_workspace(user_email, parts[1], audit_ctx)

        # Route: POST /workspaces/{id}/members
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "members"
            and http_method == "POST"
        ):
            request = AddMemberRequest(**body)
            return add_member(user_email, parts[1], request, audit_ctx)

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
            return remove_member(user_email, parts[1], member_email, audit_ctx)

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
            return add_repo(user_email, parts[1], request, audit_ctx)

        # Route: DELETE /workspaces/{id}/repos/{name}
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "repos"
            and http_method == "DELETE"
        ):
            repo_name = unquote(parts[3])
            return remove_repo(user_email, parts[1], repo_name, audit_ctx)

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
        # System Prompt Routes
        # ====================================================================

        # Route: GET /workspaces/{id}/system-prompt
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "system-prompt"
            and http_method == "GET"
        ):
            return handle_get_system_prompt(parts[1], user_email)

        # Route: PUT /workspaces/{id}/system-prompt
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "system-prompt"
            and http_method == "PUT"
        ):
            return handle_update_system_prompt(parts[1], user_email, body, audit_ctx)

        # Route: GET /workspaces/{id}/budget
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "budget"
            and http_method == "GET"
        ):
            return get_workspace_budget(user_email, parts[1])

        # Route: PUT /workspaces/{id}/budget
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "budget"
            and http_method == "PUT"
        ):
            return update_workspace_budget(user_email, parts[1], body, audit_ctx)

        # Route: DELETE /workspaces/{id}/system-prompt
        if (
            len(parts) == 3
            and parts[0] == "workspaces"
            and parts[2] == "system-prompt"
            and http_method == "DELETE"
        ):
            return handle_reset_system_prompt(parts[1], user_email, audit_ctx)

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
            return add_org_member(user_email, request, audit_ctx)

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
            return remove_org_member(user_email, member_email, audit_ctx)

        # ====================================================================
        # Chat Workspace Access Routes
        # ====================================================================

        # Route: GET /chat/searchable-workspaces
        if parts == ("chat", "searchable-workspaces") and http_method == "GET":
            return get_searchable_workspaces(user_email)

        # Route: GET /chat/available-prompts
        if parts == ("chat", "available-prompts") and http_method == "GET":
            return get_available_prompts(user_email)

        # ====================================================================
        # MCP Server Management Routes
        # ====================================================================

        # Route: POST /mcp/servers
        if parts == ("mcp", "servers") and http_method == "POST":
            return create_mcp_server(user_email, body, audit_ctx)

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
            return update_mcp_server(user_email, parts[2], body, audit_ctx)

        # Route: DELETE /mcp/servers/{id}
        if (
            len(parts) == 3
            and parts[0] == "mcp"
            and parts[1] == "servers"
            and http_method == "DELETE"
        ):
            return delete_mcp_server(user_email, parts[2], audit_ctx)

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
            return create_api_key(user_email, body, audit_ctx)

        # Route: GET /api-keys
        if parts == ("api-keys",) and http_method == "GET":
            return list_api_keys(user_email)

        # ====================================================================
        # Analytics Routes
        # ====================================================================

        # Route: GET /analytics/budgets
        if parts == ("analytics", "budgets") and http_method == "GET":
            return get_analytics_budgets(user_email)

        # Route: GET /analytics/summary
        if parts == ("analytics", "summary") and http_method == "GET":
            query_params = event.get("queryStringParameters") or {}
            return get_analytics_summary(user_email, query_params)

        # Route: GET /analytics/users
        if parts == ("analytics", "users") and http_method == "GET":
            query_params = event.get("queryStringParameters") or {}
            return get_analytics_users(user_email, query_params)

        # Route: GET /analytics/workspace/{id}
        if (
            len(parts) == 3
            and parts[0] == "analytics"
            and parts[1] == "workspace"
            and http_method == "GET"
        ):
            query_params = event.get("queryStringParameters") or {}
            return get_analytics_workspace(user_email, parts[2], query_params)

        # ====================================================================
        # Audit Routes (chunk 05)
        # ====================================================================

        # Route: GET /audit/summary
        if parts == ("audit", "summary") and http_method == "GET":
            query_params = event.get("queryStringParameters") or {}
            return get_audit_summary(user_email, query_params)

        # Route: GET /audit/workspace/{id}
        if (
            len(parts) == 3
            and parts[0] == "audit"
            and parts[1] == "workspace"
            and http_method == "GET"
        ):
            query_params = event.get("queryStringParameters") or {}
            return get_audit_workspace(user_email, parts[2], query_params)

        # Route: GET /audit/events
        if parts == ("audit", "events") and http_method == "GET":
            query_params = event.get("queryStringParameters") or {}
            return get_audit_events(user_email, query_params)

        # Route: GET /audit/event/{workspace_id}/{request_id}
        if (
            len(parts) == 4
            and parts[0] == "audit"
            and parts[1] == "event"
            and http_method == "GET"
        ):
            return get_audit_event(user_email, parts[2], parts[3])

        # Route: DELETE /api-keys/{key_id}
        if (
            len(parts) == 2
            and parts[0] == "api-keys"
            and http_method == "DELETE"
        ):
            return revoke_api_key(user_email, parts[1], audit_ctx)

        return _error(404, "Not found")

    except ValidationError as e:
        return _error(400, f"Invalid request: {e}")
    except Exception as e:
        logger.error(f"Unhandled error: {http_method} {path}: {e}", exc_info=True)
        return _error(500, f"Internal server error: {type(e).__name__}: {e}")
