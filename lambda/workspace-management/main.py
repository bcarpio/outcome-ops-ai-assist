"""
FastAPI app for Workspace Management Lambda.
Entry point for uvicorn when running under Lambda Web Adapter.
"""

import json
import os
import sys

# Ensure Lambda task root is in path
sys.path.insert(0, os.environ.get("LAMBDA_TASK_ROOT", "/var/task"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/health")
def health():
    """Health check endpoint for LWA readiness probe."""
    return {"status": "healthy"}


def _build_event(request: Request, method: str, path: str, body: str = "") -> dict:
    """Build a Lambda-style event from a FastAPI request."""
    return {
        "body": body,
        "headers": dict(request.headers),
        "requestContext": {"http": {"method": method, "path": path}},
    }


def _parse_response(result: dict) -> JSONResponse:
    """Convert a Lambda handler response dict to a FastAPI JSONResponse."""
    status = result.get("statusCode", 200)
    body = result.get("body", "{}")
    return JSONResponse(content=json.loads(body), status_code=status)


# ============================================================================
# Workspace Routes
# ============================================================================


@app.post("/workspaces")
async def create_workspace(request: Request):
    """Create a new workspace."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "POST", "/workspaces", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces")
async def list_workspaces(request: Request):
    """List user's workspaces."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/workspaces")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str, request: Request):
    """Get workspace details."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.patch("/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, request: Request):
    """Update workspace."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "PATCH", f"/workspaces/{workspace_id}", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str, request: Request):
    """Delete workspace."""
    from handler import lambda_handler

    event = _build_event(request, "DELETE", f"/workspaces/{workspace_id}")
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Member Routes
# ============================================================================


@app.post("/workspaces/{workspace_id}/members")
async def add_member(workspace_id: str, request: Request):
    """Add member to workspace."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/members", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/members")
async def list_members(workspace_id: str, request: Request):
    """List workspace members."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/members")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.patch("/workspaces/{workspace_id}/members/{member_email}")
async def update_member(workspace_id: str, member_email: str, request: Request):
    """Update member role."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request,
        "PATCH",
        f"/workspaces/{workspace_id}/members/{member_email}",
        body.decode("utf-8"),
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/members/{member_email}")
async def remove_member(workspace_id: str, member_email: str, request: Request):
    """Remove member from workspace."""
    from handler import lambda_handler

    event = _build_event(
        request, "DELETE", f"/workspaces/{workspace_id}/members/{member_email}"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Repo Routes
# ============================================================================


@app.get("/workspaces/{workspace_id}/repos/available")
async def list_available_repos(workspace_id: str, request: Request):
    """List available repos from GitHub."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/repos/available")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/repos")
async def list_repos(workspace_id: str, request: Request):
    """List workspace repos."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/repos")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/workspaces/{workspace_id}/repos")
async def add_repo(workspace_id: str, request: Request):
    """Add repo to workspace."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/repos", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/repos/{repo_name:path}")
async def remove_repo(workspace_id: str, repo_name: str, request: Request):
    """Remove repo from workspace."""
    from handler import lambda_handler

    event = _build_event(
        request, "DELETE", f"/workspaces/{workspace_id}/repos/{repo_name}"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Workspace Sharing Routes
# ============================================================================


@app.get("/workspaces/{workspace_id}/sharing")
async def get_sharing(workspace_id: str, request: Request):
    """Get workspace sharing configuration."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/sharing")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.put("/workspaces/{workspace_id}/sharing")
async def update_sharing(workspace_id: str, request: Request):
    """Update workspace sharing configuration."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "PUT", f"/workspaces/{workspace_id}/sharing", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/sharing/available")
async def list_sharing_available(workspace_id: str, request: Request):
    """List workspaces available for sharing."""
    from handler import lambda_handler

    event = _build_event(
        request, "GET", f"/workspaces/{workspace_id}/sharing/available"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# System Prompt Routes
# ============================================================================


@app.get("/workspaces/{workspace_id}/system-prompt")
async def get_system_prompt(workspace_id: str, request: Request):
    """Get workspace custom system prompt."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/system-prompt")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.put("/workspaces/{workspace_id}/system-prompt")
async def update_system_prompt(workspace_id: str, request: Request):
    """Set workspace custom system prompt."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "PUT", f"/workspaces/{workspace_id}/system-prompt", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/system-prompt")
async def reset_system_prompt(workspace_id: str, request: Request):
    """Reset workspace system prompt to default."""
    from handler import lambda_handler

    event = _build_event(request, "DELETE", f"/workspaces/{workspace_id}/system-prompt")
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# User Search Routes
# ============================================================================


@app.get("/users/search")
async def search_users_route(request: Request):
    """Search for existing users by email."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/users/search")
    event["queryStringParameters"] = dict(request.query_params)
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Org Management Routes
# ============================================================================


@app.get("/org/members")
async def list_org_members(request: Request):
    """List all org members (org admin only)."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/org/members")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/org/members")
async def add_org_member(request: Request):
    """Add an org member (org admin only)."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "POST", "/org/members", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.patch("/org/members/{member_email}")
async def update_org_member(member_email: str, request: Request):
    """Update org member role (org admin only)."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "PATCH", f"/org/members/{member_email}", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/org/members/{member_email}")
async def remove_org_member(member_email: str, request: Request):
    """Remove org member (org admin only)."""
    from handler import lambda_handler

    event = _build_event(request, "DELETE", f"/org/members/{member_email}")
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Chat Routes
# ============================================================================


@app.get("/chat/searchable-workspaces")
async def get_searchable_workspaces(request: Request):
    """List workspaces the user can search."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/chat/searchable-workspaces")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/chat/available-prompts")
async def get_available_prompts(request: Request):
    """List workspaces with custom system prompts the user can select."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/chat/available-prompts")
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Document Upload Routes
# ============================================================================


@app.post("/workspaces/{workspace_id}/documents/upload")
async def get_upload_url(workspace_id: str, request: Request):
    """Get presigned URL for document upload."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/documents/upload", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/documents")
async def list_documents(workspace_id: str, request: Request):
    """List documents in workspace."""
    from handler import lambda_handler

    # Build path with query string if present
    path = f"/workspaces/{workspace_id}/documents"
    if request.query_params:
        path += f"?{request.query_params}"
    event = _build_event(request, "GET", path)
    # Add query params to event
    event["queryStringParameters"] = dict(request.query_params)
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/documents/{document_path:path}")
async def delete_document(workspace_id: str, document_path: str, request: Request):
    """Delete a document."""
    from handler import lambda_handler

    event = _build_event(
        request, "DELETE", f"/workspaces/{workspace_id}/documents/{document_path}"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# MCP Server Management Routes
# ============================================================================


@app.post("/mcp/servers")
async def create_mcp_server_route(request: Request):
    """Create an MCP server."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "POST", "/mcp/servers", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/mcp/servers")
async def list_mcp_servers_route(request: Request):
    """List all MCP servers."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/mcp/servers")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/mcp/servers/{mcp_id}")
async def get_mcp_server_route(mcp_id: str, request: Request):
    """Get MCP server details."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/mcp/servers/{mcp_id}")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.patch("/mcp/servers/{mcp_id}")
async def update_mcp_server_route(mcp_id: str, request: Request):
    """Update an MCP server."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "PATCH", f"/mcp/servers/{mcp_id}", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/mcp/servers/{mcp_id}")
async def delete_mcp_server_route(mcp_id: str, request: Request):
    """Delete an MCP server."""
    from handler import lambda_handler

    event = _build_event(request, "DELETE", f"/mcp/servers/{mcp_id}")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/mcp/servers/{mcp_id}/refresh")
async def refresh_mcp_tools_route(mcp_id: str, request: Request):
    """Refresh MCP server tools."""
    from handler import lambda_handler

    event = _build_event(request, "POST", f"/mcp/servers/{mcp_id}/refresh")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/mcp/servers/{mcp_id}/workspaces")
async def get_mcp_server_workspaces_route(mcp_id: str, request: Request):
    """Get workspaces assigned to an MCP server."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/mcp/servers/{mcp_id}/workspaces")
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# MCP Workspace Assignment Routes
# ============================================================================


@app.post("/workspaces/{workspace_id}/mcp-servers")
async def assign_mcp_to_workspace_route(workspace_id: str, request: Request):
    """Assign MCP server to workspace."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/mcp-servers", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/mcp-servers")
async def list_workspace_mcp_servers_route(workspace_id: str, request: Request):
    """List MCP servers assigned to workspace."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/mcp-servers")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/mcp-servers/{mcp_id}")
async def remove_mcp_from_workspace_route(workspace_id: str, mcp_id: str, request: Request):
    """Remove MCP server from workspace."""
    from handler import lambda_handler

    event = _build_event(
        request, "DELETE", f"/workspaces/{workspace_id}/mcp-servers/{mcp_id}"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/chat/mcp-servers")
async def get_chat_mcp_servers_route(request: Request):
    """Get MCP servers available for chat."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/chat/mcp-servers")
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# API Key Management Routes
# ============================================================================


@app.post("/api-keys")
async def create_api_key_route(request: Request):
    """Create a personal API key."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "POST", "/api-keys", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/api-keys")
async def list_api_keys_route(request: Request):
    """List user's API keys."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/api-keys")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/api-keys/{key_id}")
async def revoke_api_key_route(key_id: str, request: Request):
    """Revoke an API key."""
    from handler import lambda_handler

    event = _build_event(request, "DELETE", f"/api-keys/{key_id}")
    result = lambda_handler(event, None)
    return _parse_response(result)
