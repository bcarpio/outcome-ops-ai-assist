"""
FastAPI app for the GitLab Integration Lambda.

Entry point for uvicorn under Lambda Web Adapter. Each FastAPI route
forwards to ``handler.lambda_handler`` so the Lambda response shape
remains identical regardless of how the function is invoked.

Per CLAUDE.md's four-place rule, adding a new endpoint requires
matching changes in:
- ``handler.py`` (route + handler function)
- this file (FastAPI route forwarding to ``lambda_handler``)
- ``ui/server/index.js`` (Express proxy)
- ``ui/src/lib/api.ts`` (TypeScript client)

Step 4 (api.ts client) lands in the UI chunk.
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


@app.get("/gitlab/authorize")
async def gitlab_authorize(request: Request):
    from handler import lambda_handler
    event = _build_event(request, "GET", "/gitlab/authorize")
    return _parse_response(lambda_handler(event, None))


@app.get("/gitlab/callback")
async def gitlab_callback_get(request: Request):
    from handler import lambda_handler
    event = _build_event(request, "GET", "/gitlab/callback")
    return _parse_response(lambda_handler(event, None))


@app.post("/gitlab/callback")
async def gitlab_callback_post(request: Request):
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(request, "POST", "/gitlab/callback", body.decode("utf-8"))
    return _parse_response(lambda_handler(event, None))


# ---------------------------------------------------------------------------
# Connections + discovery
# ---------------------------------------------------------------------------


@app.get("/workspaces/{workspace_id}/gitlab/connections")
async def list_connections(workspace_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "GET", f"/workspaces/{workspace_id}/gitlab/connections",
    )
    return _parse_response(lambda_handler(event, None))


@app.delete("/workspaces/{workspace_id}/gitlab/connections/{connection_id}")
async def delete_connection(workspace_id: str, connection_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "DELETE",
        f"/workspaces/{workspace_id}/gitlab/connections/{connection_id}",
    )
    return _parse_response(lambda_handler(event, None))


@app.get("/workspaces/{workspace_id}/gitlab/connections/{connection_id}/groups")
async def list_groups(workspace_id: str, connection_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "GET",
        f"/workspaces/{workspace_id}/gitlab/connections/{connection_id}/groups",
    )
    return _parse_response(lambda_handler(event, None))


@app.get(
    "/workspaces/{workspace_id}/gitlab/connections/{connection_id}"
    "/groups/{group_id}/projects"
)
async def list_group_projects(
    workspace_id: str, connection_id: str, group_id: str, request: Request
):
    from handler import lambda_handler
    event = _build_event(
        request, "GET",
        f"/workspaces/{workspace_id}/gitlab/connections/{connection_id}"
        f"/groups/{group_id}/projects",
    )
    return _parse_response(lambda_handler(event, None))


# ---------------------------------------------------------------------------
# Workspace repo set
# ---------------------------------------------------------------------------


@app.post("/workspaces/{workspace_id}/gitlab/repos")
async def add_repos(workspace_id: str, request: Request):
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(
        request, "POST",
        f"/workspaces/{workspace_id}/gitlab/repos",
        body.decode("utf-8"),
    )
    return _parse_response(lambda_handler(event, None))


@app.get("/workspaces/{workspace_id}/gitlab/repos")
async def list_repos(workspace_id: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "GET", f"/workspaces/{workspace_id}/gitlab/repos",
    )
    return _parse_response(lambda_handler(event, None))


@app.put("/workspaces/{workspace_id}/gitlab/repos/{slug:path}")
async def update_repo(workspace_id: str, slug: str, request: Request):
    """Slug is ``group/.../project`` -- the ``:path`` converter keeps its
    embedded slashes intact rather than treating them as additional path
    segments. GitLab nested subgroups mean the slug can have >2 segments."""
    from handler import lambda_handler
    body = await request.body()
    event = _build_event(
        request, "PUT",
        f"/workspaces/{workspace_id}/gitlab/repos/{slug}",
        body.decode("utf-8"),
    )
    return _parse_response(lambda_handler(event, None))


@app.delete("/workspaces/{workspace_id}/gitlab/repos/{slug:path}")
async def delete_repo(workspace_id: str, slug: str, request: Request):
    from handler import lambda_handler
    event = _build_event(
        request, "DELETE",
        f"/workspaces/{workspace_id}/gitlab/repos/{slug}",
    )
    return _parse_response(lambda_handler(event, None))
