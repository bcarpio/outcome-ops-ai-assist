"""
OutcomeOps MCP Server - Streamable HTTP Transport

Exposes OutcomeOps knowledge base tools to MCP-compatible clients
(Claude Code, VS Code, Cursor, etc.) via the MCP Streamable HTTP protocol.

Authentication: API keys (Bearer token), validated against DynamoDB.
Transport: JSON-RPC 2.0 over HTTP POST.
"""


def handler(event, context):
    """
    Enterprise implementation placeholder.

    This function is part of the proprietary OutcomeOps platform.
    Available via enterprise licensing only.
    See: https://www.outcomeops.ai
    """
    raise NotImplementedError(
        "This is an enterprise component. "
        "Visit https://www.outcomeops.ai for deployment options."
    )
