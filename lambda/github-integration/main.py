# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
FastAPI app for GitHub Integration Lambda.
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

    # Handle redirects
    if status == 302:
        location = result.get("headers", {}).get("Location", "/")
        return RedirectResponse(url=location, status_code=302)

    body = result.get("body", "{}")
    return JSONResponse(content=json.loads(body), status_code=status)


# ============================================================================
# OAuth Routes
# ============================================================================


@app.get("/github/authorize")
async def github_authorize(request: Request):
    """Get GitHub App installation URL."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/github/authorize")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/github/callback")
async def github_callback(request: Request):
    """GitHub App installation callback."""
    from handler import lambda_handler

    event = _build_event(request, "GET", "/github/callback")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/github/callback")
async def github_callback_post(request: Request):
    """GitHub App installation callback (POST for SigV4 compatibility)."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(request, "POST", "/github/callback", body.decode("utf-8"))
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Installation Routes
# ============================================================================


@app.get("/workspaces/{workspace_id}/github/installations")
async def list_installations(workspace_id: str, request: Request):
    """List GitHub installations for workspace."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/github/installations")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.get("/workspaces/{workspace_id}/github/installations/{installation_id}/repos")
async def list_installation_repos(workspace_id: str, installation_id: str, request: Request):
    """List repos for an installation."""
    from handler import lambda_handler

    event = _build_event(
        request, "GET", f"/workspaces/{workspace_id}/github/installations/{installation_id}/repos"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Repo Routes
# ============================================================================


@app.get("/workspaces/{workspace_id}/github/repos")
async def list_repos(workspace_id: str, request: Request):
    """List GitHub repos for workspace."""
    from handler import lambda_handler

    event = _build_event(request, "GET", f"/workspaces/{workspace_id}/github/repos")
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.post("/workspaces/{workspace_id}/github/repos")
async def add_repos(workspace_id: str, request: Request):
    """Add repos to workspace."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/github/repos", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.put("/workspaces/{workspace_id}/github/repos/{repo_name:path}")
async def update_repo(workspace_id: str, repo_name: str, request: Request):
    """Update repo settings."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_event(
        request, "PUT", f"/workspaces/{workspace_id}/github/repos/{repo_name}", body.decode("utf-8")
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


@app.delete("/workspaces/{workspace_id}/github/repos/{repo_name:path}")
async def delete_repo(workspace_id: str, repo_name: str, request: Request):
    """Remove repo from workspace."""
    from handler import lambda_handler

    event = _build_event(
        request, "DELETE", f"/workspaces/{workspace_id}/github/repos/{repo_name}"
    )
    result = lambda_handler(event, None)
    return _parse_response(result)


# ============================================================================
# Sync Routes
# ============================================================================


@app.post("/workspaces/{workspace_id}/github/sync")
async def trigger_sync(workspace_id: str, request: Request):
    """Trigger GitHub sync for workspace repos."""
    from handler import lambda_handler

    body = await request.body()
    body_str = body.decode("utf-8") if body else ""
    event = _build_event(
        request, "POST", f"/workspaces/{workspace_id}/github/sync", body_str
    )
    result = lambda_handler(event, None)
    return _parse_response(result)
