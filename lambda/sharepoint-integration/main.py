"""
FastAPI app for SharePoint Integration Lambda.
Entry point for uvicorn when running under Lambda Web Adapter.
"""

import json
import os
import sys

# Ensure Lambda task root is in path
sys.path.insert(0, os.environ.get("LAMBDA_TASK_ROOT", "/var/task"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

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
        "queryStringParameters": dict(request.query_params),
        "requestContext": {"http": {"method": method, "path": path}},
    }


def _parse_response(result: dict):
    """Convert a Lambda handler response dict to a FastAPI response."""
    status = result.get("statusCode", 200)

    if status == 302:
        location = result.get("headers", {}).get("Location", "/")
        return RedirectResponse(url=location, status_code=302)

    body = result.get("body", "{}")
    return JSONResponse(content=json.loads(body), status_code=status)


# ============================================================================
# OAuth Routes
# ============================================================================


@app.get("/sharepoint/authorize")
async def sharepoint_authorize(request: Request):
    """Get SharePoint OAuth authorization URL."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/sharepoint/authorize")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/sharepoint/callback")
async def sharepoint_callback(request: Request):
    """SharePoint OAuth callback."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/sharepoint/callback")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/sharepoint/callback")
async def sharepoint_callback_post(request: Request):
    """SharePoint OAuth callback (POST for SigV4 compatibility)."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "POST", "/sharepoint/callback", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Connection Routes
# ============================================================================


@app.get("/workspaces/{workspace_id}/sharepoint/connections")
async def list_connections(workspace_id: str, request: Request):
    """List SharePoint connections for workspace."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/sharepoint/connections")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/sharepoint/connections/{connection_id}/sites")
async def get_sites(workspace_id: str, connection_id: str, request: Request):
    """List available SharePoint sites."""
    from handler import lambda_handler

    event = _build_event(
        request, "GET", f"/workspaces/{workspace_id}/sharepoint/connections/{connection_id}/sites"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.put("/workspaces/{workspace_id}/sharepoint/connections/{connection_id}/sites")
async def update_sites(workspace_id: str, connection_id: str, request: Request):
    """Update selected SharePoint sites."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "PUT", f"/workspaces/{workspace_id}/sharepoint/connections/{connection_id}/sites",
        body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/workspaces/{workspace_id}/sharepoint/connections/{connection_id}/sync")
async def trigger_sync(workspace_id: str, connection_id: str, request: Request):
    """Trigger SharePoint sync for a connection."""
    from handler import lambda_handler

    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/sharepoint/connections/{connection_id}/sync"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/sharepoint/connections/{connection_id}")
async def delete_connection(workspace_id: str, connection_id: str, request: Request):
    """Delete a SharePoint connection."""
    from handler import lambda_handler

    event = _build_event(
        request, "DELETE", f"/workspaces/{workspace_id}/sharepoint/connections/{connection_id}"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)
