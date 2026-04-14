"""
GitHub App Integration Lambda for OutcomeOps AI Assist.

Handles GitHub App OAuth flow and connection management.
Supports per-workspace repo selection with code-map generation.

API Endpoints:
    GET  /github/authorize?workspace_id={id}                    - Get GitHub App install URL
    GET  /github/callback                                       - Installation callback
    GET  /workspaces/{id}/github/installations                  - List installations
    GET  /workspaces/{id}/github/install...
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
