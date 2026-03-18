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

    event = {
        "body": body_str,
        "headers": dict(request.headers),
        "requestContext": {"http": {"method": "POST", "path": "/"}},
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
    return {
        "headers": dict(request.headers),
        "requestContext": {"http": {"method": method, "path": path}},
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
