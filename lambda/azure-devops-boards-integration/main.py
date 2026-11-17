# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
FastAPI app for the Azure DevOps Boards Integration Lambda.

Entry point for uvicorn under Lambda Web Adapter. Each FastAPI route
forwards to ``handler.lambda_handler`` so the Lambda response shape
remains identical regardless of how the function is invoked.

Per CLAUDE.md's four-place rule, adding a new endpoint requires
matching changes in:
- ``handler.py`` (route + handler function)
- this file (FastAPI route forwarding to ``lambda_handler``)
- ``ui/server/index.js`` (Express proxy)
- ``ui/src/lib/api.ts`` (TypeScript client)

Steps 3 + 4 land in chunk 07.
"""

import json
import os
import sys

# Ensure Lambda task root is on the path so ``from handler import ...``
# works under LWA.
sys.path.insert(0, os.environ.get("LAMBDA_TASK_ROOT", "/var/task"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "healthy"}


def _build_event(request: Request, method: str, path: str, body: str = "") -> dict:
    return {
        "body": body,
        "headers": dict(request.headers),
        "queryStringParameters": dict(request.query_params),
        "requestContext": {"http": {"method": method, "path": path}},
    }


def _parse_response(result: dict):
    status = result.get("statusCode", 200)
    if status == 302:
        location = result.get("headers", {}).get("Location", "/")
        return RedirectResponse(url=location, status_code=302)
    body = result.get("body", "{}")
    return JSONResponse(content=json.loads(body), status_code=status)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


@app.get("/azure-devops-boards/authorize")
async def boards_authorize(request: Request):
    from handler import lambda_handler
    event = _build_event(request, "GET", "/azure-devops-boards/authorize")
    return _parse_response(lambda_handler(event, None))


@app.get("/azure-devops-boards/callback")
async def boards_callback_get(request: Request):
    from handler import lambda_handler
    event = _build_event(request, "GET", "/azure-devops-boards/callback")
    return _parse_response(lambda_handler(event, None))


@app.post("/azure-devops-boards/callback")
async def boards_callback_post(request: Request):
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(
        request, "POST", "/azure-devops-boards/callback", body.decode("utf-8"),
    )
    return _parse_response(lambda_handler(event, None))


# ---------------------------------------------------------------------------
# Connections + discovery
# ---------------------------------------------------------------------------


@app.get("/workspaces/{workspace_id}/azure-devops-boards/connections")
async def list_connections(workspace_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "GET",
        f"/workspaces/{workspace_id}/azure-devops-boards/connections",
    )
    return _parse_response(lambda_handler(event, None))


@app.delete(
    "/workspaces/{workspace_id}/azure-devops-boards/connections/{connection_id}"
)
async def delete_connection(workspace_id: str, connection_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "DELETE",
        f"/workspaces/{workspace_id}/azure-devops-boards/connections/{connection_id}",
    )
    return _parse_response(lambda_handler(event, None))


@app.get(
    "/workspaces/{workspace_id}/azure-devops-boards/connections/{connection_id}/orgs"
)
async def list_orgs(workspace_id: str, connection_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "GET",
        f"/workspaces/{workspace_id}/azure-devops-boards/connections/{connection_id}/orgs",
    )
    return _parse_response(lambda_handler(event, None))


@app.get(
    "/workspaces/{workspace_id}/azure-devops-boards/connections/{connection_id}"
    "/orgs/{org}/projects"
)
async def list_org_projects(
    workspace_id: str, connection_id: str, org: str, request: Request,
):
    from handler import lambda_handler
    event = _build_event(
        request, "GET",
        f"/workspaces/{workspace_id}/azure-devops-boards/connections/{connection_id}"
        f"/orgs/{org}/projects",
    )
    return _parse_response(lambda_handler(event, None))


# ---------------------------------------------------------------------------
# Workspace project set
# ---------------------------------------------------------------------------


@app.post("/workspaces/{workspace_id}/azure-devops-boards/projects")
async def add_projects(workspace_id: str, request: Request):
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(
        request, "POST",
        f"/workspaces/{workspace_id}/azure-devops-boards/projects",
        body.decode("utf-8"),
    )
    return _parse_response(lambda_handler(event, None))


@app.get("/workspaces/{workspace_id}/azure-devops-boards/projects")
async def list_projects(workspace_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "GET",
        f"/workspaces/{workspace_id}/azure-devops-boards/projects",
    )
    return _parse_response(lambda_handler(event, None))


@app.put(
    "/workspaces/{workspace_id}/azure-devops-boards/projects/{slug:path}"
)
async def update_project(workspace_id: str, slug: str, request: Request):
    """Slug is ``org/project`` -- the ``:path`` converter preserves its
    embedded slash rather than splitting it into two path segments."""
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(
        request, "PUT",
        f"/workspaces/{workspace_id}/azure-devops-boards/projects/{slug}",
        body.decode("utf-8"),
    )
    return _parse_response(lambda_handler(event, None))


@app.delete(
    "/workspaces/{workspace_id}/azure-devops-boards/projects/{slug:path}"
)
async def delete_project(workspace_id: str, slug: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "DELETE",
        f"/workspaces/{workspace_id}/azure-devops-boards/projects/{slug}",
    )
    return _parse_response(lambda_handler(event, None))


@app.post("/workspaces/{workspace_id}/azure-devops-boards/sync")
async def trigger_sync(workspace_id: str, request: Request):
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(
        request, "POST",
        f"/workspaces/{workspace_id}/azure-devops-boards/sync",
        body.decode("utf-8"),
    )
    return _parse_response(lambda_handler(event, None))
