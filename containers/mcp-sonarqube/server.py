"""Minimal SonarQube Cloud MCP server (OutcomeOps-owned).

Replaces the third-party `mcp/sonarqube` image whose Apache HttpClient async I/O
reactor shuts down under concurrent load (IOReactorShutdownException) and never
recovers. This is a tiny stateless MCP Streamable-HTTP server: each tool call
makes a fresh per-request HTTPS call to SonarCloud's REST API (sonarcloud.py) --
no shared async reactor, no connection pool, structurally immune to that bug.

It speaks the same MCP contract as the retired container (POST /mcp, GET /
health), so the OutcomeOps gateway and the chat reach it through the existing
catalog+proxy path unchanged. It is also the reference template for future
container-only MCP servers a customer wants to add: build image -> ECS ->
catalog -> both surfaces proxy.

Auth: none at this layer -- the container is only reachable in-VPC via the ALB /
Cloud Map security groups. The SonarCloud token is read from SSM (env
SONARQUBE_TOKEN_SSM_PATH) at request time, never exposed to callers.
"""

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sonarcloud

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sonarqube-mcp")

PROTOCOL_VERSION = "2025-03-26"
PORT = int(os.environ.get("SONARQUBE_HTTP_PORT", "8080"))


def _jsonrpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _jsonrpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(fmt % args)

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._send(200, {"status": "healthy", "service": "sonarqube-mcp"})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # Every POST is an MCP JSON-RPC request. We accept any path so both the
        # gateway (Cloud Map, /mcp) and the chat (via the ALB rule path, e.g.
        # /mcp/sonarqube) reach us without a path-rewrite dance.
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            self._send(400, _jsonrpc_error(None, -32600, "Empty body"))
            return
        try:
            req = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send(400, _jsonrpc_error(None, -32600, "Invalid JSON"))
            return

        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "initialize":
            self._send(200, _jsonrpc_result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "SonarQube", "version": "1.0.0"},
            }))
        elif method == "notifications/initialized" or rid is None:
            # notification -> no response body expected
            self.send_response(202)
            self.end_headers()
        elif method == "tools/list":
            self._send(200, _jsonrpc_result(rid, {"tools": sonarcloud.TOOL_DEFS}))
        elif method == "tools/call":
            self._handle_tool_call(rid, params)
        else:
            self._send(200, _jsonrpc_error(rid, -32601, f"Method not found: {method}"))

    def _handle_tool_call(self, rid, params):
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        fn = sonarcloud.TOOL_MAP.get(name)
        if not fn:
            self._send(200, _jsonrpc_error(rid, -32602, f"Unknown tool: {name}"))
            return
        try:
            result = fn(args)
        except Exception as e:  # never crash the server on a tool error
            logger.exception("tool %s failed", name)
            result = {"error": f"{type(e).__name__}: {e}"}
        if "error" in result:
            content = [{"type": "text", "text": f"Error: {result['error']}"}]
            is_error = True
        else:
            content = [{"type": "text", "text": result["text"]}]
            is_error = False
        self._send(200, _jsonrpc_result(rid, {"content": content, "isError": is_error}))


def main():
    logger.info("SonarQube MCP server (OutcomeOps) listening on :%d (org=%s, url=%s)",
                PORT, sonarcloud._org(), sonarcloud._base_url())
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
