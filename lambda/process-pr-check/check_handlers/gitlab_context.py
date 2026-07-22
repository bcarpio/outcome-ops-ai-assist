"""
Handler-local GitLab token helper.

Mirrors ``ado_context.py``: every check handler that reads GitLab MR
source needs the same "get a usable access token for a customer
connection_id" call. Unlike ADO, GitLab rotates the refresh token on
EVERY refresh (ADR-050 v1.1), so this helper MUST route through the
cached single-flight path (``shared.oauth.gitlab.get_valid_access_token``)
-- a parallel raw-refresh here would race the sync workers and poison
the connection with a dead rotated token.

Env contract (wired by terraform/lambda-pr-analysis.tf, same names the
gitlab-sync worker uses):

- ``WORKSPACES_TABLE``       -- GITLAB_TOKEN# rows + the refresh lock.
- ``OAUTH_KMS_KEY_ARN``      -- encrypts the cached access token.
- ``GITLAB_BASE_URL`` / ``GITLAB_CLIENT_ID`` /
  ``GITLAB_CLIENT_SECRET_SSM_PARAM`` -- read inside shared.oauth.gitlab.
"""

import logging
import os

logger = logging.getLogger(__name__)


def get_gitlab_token(connection_id: str, *, force_refresh: bool = False) -> str:
    """Return a usable GitLab access token for a customer connection.

    Raises ``RuntimeError`` when the env contract is missing or the
    connection has no refresh token -- callers surface a
    "reconnect_required" message on that.
    """
    if not connection_id:
        raise ValueError("connection_id required for GitLab token")

    workspaces_table = os.environ.get("WORKSPACES_TABLE", "")
    kms_key_arn = os.environ.get("OAUTH_KMS_KEY_ARN", "")
    if not (workspaces_table and kms_key_arn):
        raise RuntimeError(
            "GitLab OAuth not configured "
            "(WORKSPACES_TABLE / OAUTH_KMS_KEY_ARN env vars missing)"
        )

    from shared.oauth.gitlab import get_valid_access_token  # noqa: E402

    access_token = get_valid_access_token(
        workspaces_table, kms_key_arn, connection_id,
        force_refresh=force_refresh,
    )
    if not access_token:
        raise RuntimeError(
            "Failed to obtain GitLab access token; reconnect_required"
        )
    return access_token


def get_gitlab_base_url(connection_id: str) -> str:
    """The base URL of the GitLab instance this connection targets, resolved
    from the instance id stamped on the connection's CREDENTIALS row. Used to
    build MR-note API calls + human-followable note URLs for the right host."""
    workspaces_table = os.environ.get("WORKSPACES_TABLE", "")
    from shared.oauth.gitlab import instance_for_connection
    return instance_for_connection(workspaces_table, connection_id).base_url
