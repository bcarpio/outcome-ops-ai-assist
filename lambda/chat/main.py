"""
FastAPI app for Lambda Web Adapter streaming.
Entry point for uvicorn when running under LWA.
CORS is handled by Lambda Function URL config, not here.
"""
import json
import os
import sys

# Ensure Lambda task root is in path
sys.path.insert(0, os.environ.get("LAMBDA_TASK_ROOT", "/var/task"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()


def _extract_browser_ctx(headers: dict) -> tuple[str, str]:
    """Pull the browser's source IP / user-agent forwarded by the Express
    proxy as ``x-real-source-ip`` / ``x-real-user-agent``, falling back to
    ``x-forwarded-for`` (LWA-injected from the Function URL's
    requestContext.http.sourceIp) and the standard ``user-agent`` header.
    Without this lookup the audit row records source_ip="" and user_agent
    is whatever Node's fetch defaults to ("node")."""
    real_ip = headers.get("x-real-source-ip", "").strip()
    if not real_ip:
        xff = headers.get("x-forwarded-for", "")
        real_ip = xff.split(",")[0].strip() if xff else ""
    real_ua = (
        headers.get("x-real-user-agent", "").strip()
        or headers.get("user-agent", "")
    )
    return real_ip, real_ua


@app.get("/health")
def health():
    """Health check endpoint for LWA readiness probe."""
    return {"status": "healthy"}


@app.post("/")
async def chat_stream(request: Request):
    """Stream chat responses using NDJSON format."""
    from handler import handle_chat_stream

    body = await request.body()
    body_str = body.decode("utf-8")

    headers = dict(request.headers)
    source_ip, user_agent = _extract_browser_ctx(headers)
    event = {
        "body": body_str,
        "headers": headers,
        "requestContext": {
            "http": {
                "method": "POST",
                "path": "/",
                "sourceIp": source_ip,
                "userAgent": user_agent,
            },
        },
    }

    def generate():
        try:
            for chunk in handle_chat_stream(event):
                yield chunk
        except Exception as e:
            print(f"[STREAM] Error: {e}")
            import traceback
            traceback.print_exc()
            yield json.dumps({"type": "error", "error": str(e)}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ============================================================================
# REST Endpoints for Conversation Management
# ============================================================================


def _build_rest_event(request: Request, method: str, path: str) -> dict:
    """Build a Lambda-style event from a FastAPI request."""
    headers = dict(request.headers)
    source_ip, user_agent = _extract_browser_ctx(headers)
    return {
        "headers": headers,
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
                "sourceIp": source_ip,
                "userAgent": user_agent,
            },
        },
    }


def _parse_lambda_response(result: dict) -> JSONResponse:
    """Convert a Lambda handler response dict to a FastAPI JSONResponse."""
    status = result.get("statusCode", 200)
    body = result.get("body", "{}")
    return JSONResponse(content=json.loads(body), status_code=status)


@app.get("/conversations")
async def list_conversations(request: Request, limit: int = 20, cursor: str = None):
    """List conversations for the authenticated user, with pagination."""
    from handler import lambda_handler

    event = _build_rest_event(request, "GET", "/conversations")
    event["queryStringParameters"] = {"limit": str(limit)}
    if cursor:
        event["queryStringParameters"]["cursor"] = cursor
    result = lambda_handler(event, None)
    return _parse_lambda_response(result)


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    """Get a conversation with its messages."""
    from handler import lambda_handler

    event = _build_rest_event(request, "GET", f"/conversations/{conversation_id}")
    result = lambda_handler(event, None)
    return _parse_lambda_response(result)


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request):
    """Delete a conversation."""
    from handler import lambda_handler

    event = _build_rest_event(request, "DELETE", f"/conversations/{conversation_id}")
    result = lambda_handler(event, None)
    return _parse_lambda_response(result)


# ============================================================================
# Conversation Sharing Endpoints
# ============================================================================


@app.put("/conversations/{conversation_id}/share")
async def share_conversation(conversation_id: str, request: Request):
    """Set sharing for a conversation (replaces current sharing)."""
    from handler import lambda_handler

    body = await request.body()
    event = _build_rest_event(
        request, "PUT", f"/conversations/{conversation_id}/share"
    )
    event["body"] = body.decode("utf-8")
    result = lambda_handler(event, None)
    return _parse_lambda_response(result)


@app.get("/shared")
async def list_shared_conversations(
    request: Request,
    workspace_id: str = None,
    limit: int = 20,
    cursor: str = None,
):
    """List shared conversations visible to the user."""
    from handler import lambda_handler

    event = _build_rest_event(request, "GET", "/shared")
    event["queryStringParameters"] = {"limit": str(limit)}
    if workspace_id:
        event["queryStringParameters"]["workspace_id"] = workspace_id
    if cursor:
        event["queryStringParameters"]["cursor"] = cursor
    result = lambda_handler(event, None)
    return _parse_lambda_response(result)
