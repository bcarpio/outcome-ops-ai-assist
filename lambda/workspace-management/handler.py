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

Security: Invoked via IAM-authenticated Lambda Function URL from Fargate.
"""

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator

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

    email: EmailStr
    role: str = Field(default="member")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

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

    email: EmailStr
    org_role: str = Field(default="member")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

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
# Workspace Operations
# ============================================================================


def create_workspace(user_email: str, request: CreateWorkspaceRequest) -> Dict:
    """Create a new workspace and add creator as admin."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    # Ensure user has org membership record
    _ensure_org_member(table, user_email)

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


def _ensure_org_member(table, user_email: str) -> None:
    """Ensure user has an org membership record. Creates one if missing."""
    existing = _get_org_membership(table, user_email)
    if existing:
        return

    # Create org membership as regular member
    now = datetime.now(timezone.utc).isoformat()
    table.put_item(
        Item={
            "PK": f"USER#{user_email}",
            "SK": "ORG_MEMBER",
            "GSI1PK": "ORG#default",
            "GSI1SK": f"USER#{user_email}",
            "user_email": user_email,
            "org_role": "member",
            "joined_at": now,
        }
    )
    logger.info(f"Created org membership for {user_email}")


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

        # Ensure user has org membership record
        _ensure_org_member(table, request.email)

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
# Org Management Operations
# ============================================================================


def list_org_members(user_email: str) -> Dict:
    """List all org members (org admin only)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        # Must be org admin
        if not _is_org_admin(table, user_email):
            return _error(403, "Only org admins can list org members")

        # Query using GSI1 for ORG#default
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :gsi1pk",
            ExpressionAttributeValues={":gsi1pk": "ORG#default"},
        )

        members = response.get("Items", [])

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

        # Check member exists
        existing = _get_org_membership(table, member_email)
        if not existing:
            return _error(404, "Org member not found")

        # Remove org membership
        table.delete_item(Key={"PK": f"USER#{member_email}", "SK": "ORG_MEMBER"})

        # Also remove all workspace memberships for this user
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
        "FilterExpression": "SK = :sk",
        "ExpressionAttributeValues": {":sk": "METADATA"},
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

        return _error(404, "Not found")

    except ValidationError as e:
        return _error(400, f"Invalid request: {e}")
