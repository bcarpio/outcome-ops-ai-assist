# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Bounded source reads for verifying exact code values (ADR-030).

Code-maps deliberately omit literal values (ADR-027) and the symbol
graph stores anchors (file:line) but not values. When an answer depends
on an exact value, the real line has to come from source. This module
owns that read for every consumer:

- the MCP server's ``read_source`` tool (the client model asks for a
  window), and
- chat, which attaches the source window for graph-matched symbols
  to the synthesis context server-side (chat has no tool loop).

Responsibilities: resolve which code host a workspace is connected to
(GitHub App installation, Azure DevOps OAuth connection, or GitLab OAuth
connection; ADR-030 v1.1, ADR-050 Decision 6), mint a short-lived token
for it, resolve the graph's bare repo name to the host's full slug,
fetch the file at a ref, and slice a window around an anchor line.

Workspaces table name comes from ``DYNAMODB_TABLE`` (MCP server) or
``WORKSPACES_TABLE`` (Lambdas), read at call time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_dynamodb = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        )
    return _dynamodb


def _workspaces_table_name() -> str:
    return os.environ.get("DYNAMODB_TABLE") or os.environ.get("WORKSPACES_TABLE", "")


def _workspaces_table():
    return _get_dynamodb().Table(_workspaces_table_name())


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base)."""
    from shared.github_base import github_api_base  # noqa: WPS433
    return github_api_base()


DEFAULT_CONTEXT_LINES = 20
MAX_CONTEXT_LINES = 200
MAX_FILE_BYTES = 1_048_576  # 1 MB whole-file fetch cap

_github_app_config_cache = None


def github_app_config():
    """Fetch GitHub App id + private key from SSM. Cached at module scope so
    the JWT-signing key isn't re-fetched per request."""
    global _github_app_config_cache
    if _github_app_config_cache is not None:
        return _github_app_config_cache
    env = os.environ.get("ENVIRONMENT") or os.environ.get("ENV", "dev")
    app_name = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
    ssm = boto3.client("ssm")
    resp = ssm.get_parameters(
        Names=[
            f"/{env}/{app_name}/github/app-id",
            f"/{env}/{app_name}/github/private-key",
        ],
        WithDecryption=True,
    )
    params = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in resp.get("Parameters", [])}
    cfg = {"app_id": params.get("app-id", ""), "private_key": params.get("private-key", "")}
    if not cfg["app_id"] or not cfg["private_key"]:
        raise RuntimeError("GitHub App id or private key not configured in SSM")
    _github_app_config_cache = cfg
    return cfg


def installation_token(installation_id):
    """Exchange the App JWT for an installation access token."""
    import jwt  # noqa: WPS433 -- defer import; only needed on read_source path
    cfg = github_app_config()
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": cfg["app_id"]}
    jwt_token = jwt.encode(payload, cfg["private_key"], algorithm="RS256")
    url = f"{_github_api()}/app/installations/{installation_id}/access_tokens"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "outcomeops-mcp/read_source",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["token"]


def resolve_installation_id(workspace_id):
    """Find the GitHub App installation id linked to a workspace.

    Workspaces table row: ``PK = WS#<id>``, ``SK begins_with
    GITHUB_INSTALLATION#``. Returns the first row's installation_id or
    None when no installation has been linked yet.
    """
    table = _workspaces_table()
    from boto3.dynamodb.conditions import Key  # noqa: WPS433
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
        & Key("SK").begins_with("GITHUB_INSTALLATION#"),
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    return items[0].get("installation_id")


# ---------------------------------------------------------------------------
# ADR-030 v1.1 -- platform-agnostic dispatch for read_source. GitHub and
# Azure DevOps have symmetric responsibilities (resolve where the code
# lives, mint a token, fetch a file window) but very different plumbing:
#
#   - GitHub: one GITHUB_INSTALLATION# row per workspace; token minted via
#     App JWT -> installation access-token exchange.
#   - Azure DevOps: one AZDO_CONNECTION# row per OAuth flow (org-agnostic)
#     plus one AZDO_REPO#{org/project/repo} row per connected repo. Each
#     AZDO_REPO row carries its OWN connection_id, because a single
#     workspace may span multiple ADO connections and each repo tracks
#     which one to mint from.
#
# The resolver keys on (workspace_id, repo_hint) instead of just
# workspace_id so it can find the right ADO connection when the caller
# already knows the target repo. When repo_hint is None the resolver
# falls back to any GitHub installation on the workspace -- callers must
# still validate that the eventual repo matches the connection.
# ---------------------------------------------------------------------------


AZDO_ENTRA_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
AZDO_ENTRA_SCOPES = (
    f"{AZDO_ENTRA_RESOURCE_ID}/.default offline_access openid profile"
)
AZDO_TOKEN_PK_PREFIX = "AZDO_TOKEN#"

_ado_client_secret_cache = None


def ado_client_secret():
    """One-shot SSM read of the ADO Entra ID app's client secret.

    Cached at module scope like the GitHub App config so subsequent
    reads stay in memory across MCP requests within the same Fargate
    task lifetime. The MCP task role must have ``ssm:GetParameter``
    on the parameter named in ``AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM``
    plus ``kms:Decrypt`` on the SecureString's KMS key.
    """
    global _ado_client_secret_cache
    if _ado_client_secret_cache is not None:
        return _ado_client_secret_cache
    param_name = os.environ.get("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", "")
    if not param_name:
        raise RuntimeError(
            "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured; "
            "read_source cannot mint Azure DevOps tokens"
        )
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    _ado_client_secret_cache = resp["Parameter"]["Value"]
    return _ado_client_secret_cache


def ado_access_token(connection_id):
    """Refresh the ADO OAuth access token for a workspace connection.

    Uses the same shared.entra_id_oauth primitive that the analyze-pr
    and process-pr-check handlers use -- the wrapper is inline here
    rather than imported from ``lambda/analyze-pr/`` because that path
    isn't part of the MCP-server Fargate image (only ``shared/`` is).

    Raises ``RuntimeError`` on any missing prereq or refresh failure --
    callers surface a clean tool-facing error to the LLM.
    """
    if not connection_id:
        raise ValueError("connection_id required for ADO token refresh")

    tenant_id = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
    client_id = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
    if not (tenant_id and client_id):
        raise RuntimeError(
            "Azure DevOps OAuth app not configured "
            "(AZURE_DEVOPS_TENANT_ID / AZURE_DEVOPS_CLIENT_ID env vars missing)"
        )

    from shared.entra_id_oauth import refresh_access_token  # noqa: WPS433
    client_secret = ado_client_secret()
    access_token = refresh_access_token(
        workspaces_table=_workspaces_table_name(),
        kms_key_arn=os.environ.get("OAUTH_KMS_KEY_ARN"),
        pk_prefix=AZDO_TOKEN_PK_PREFIX,
        connection_id=connection_id,
        client_id=client_id,
        client_secret=client_secret,
        scopes=AZDO_ENTRA_SCOPES,
        tenant_id=tenant_id,
    )
    if not access_token:
        raise RuntimeError(
            "Failed to refresh Azure DevOps access token; the workspace's "
            "ADO OAuth connection may need to be reauthorized"
        )
    return access_token


def gitlab_access_token(connection_id):
    """Refresh the GitLab OAuth access token for a workspace connection.

    Mirrors ``ado_access_token`` for the GitLab side but is
    thinner: ``shared.oauth.gitlab.get_valid_access_token`` already loads
    the OAuth app's client id (env) + client secret (SSM) and delegates
    KMS-encrypted token storage to ``shared.entra_id_oauth`` keyed by the
    ``GITLAB_TOKEN#`` prefix, so no inline secret read is needed here. It
    also serves a cached token and single-flights refresh (ADR-050 v1.1),
    so a read_source call never collides with a concurrent sync's refresh.

    Raises ``ValueError``/``RuntimeError`` on missing prereqs or refresh
    failure -- callers surface a clean tool-facing error to the LLM.
    """
    if not connection_id:
        raise ValueError("connection_id required for GitLab token refresh")

    from shared.oauth import gitlab as gitlab_oauth  # noqa: WPS433
    # Resolve the instance this connection targets (multi-instance, ADR-050
    # v2.0) so read_source hits the right GitLab host + refreshes against
    # the right OAuth app.
    instance = gitlab_oauth.instance_for_connection(_workspaces_table_name(), connection_id)
    access_token = gitlab_oauth.get_valid_access_token(
        workspaces_table=_workspaces_table_name(),
        kms_key_arn=os.environ.get("OAUTH_KMS_KEY_ARN"),
        connection_id=connection_id,
        instance=instance,
    )
    if not access_token:
        raise RuntimeError(
            "Failed to refresh GitLab access token; the workspace's "
            "GitLab OAuth connection may need to be reauthorized"
        )
    return access_token, instance.base_url


def resolve_full_ado_triplet(workspace_id, bare_repo):
    """Find the full ``org/project/repo`` triplet for a bare repo name.

    Mirrors ``resolve_full_repo_name`` for the ADO side.
    The symbol graph stores ``repo`` as the bare name so the code-maps
    pointer key matches; the ADO items API needs the full triplet.

    Returns the ``org/project/repo`` string, None if no row matches or
    if multiple rows in the workspace share the bare name (ambiguous).
    """
    from boto3.dynamodb.conditions import Key  # noqa: WPS433
    if not bare_repo:
        return None
    if bare_repo.count("/") == 2:
        return bare_repo
    table = _workspaces_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
        & Key("SK").begins_with("AZDO_REPO#"),
    )
    matches = []
    for item in resp.get("Items", []):
        if item.get("repo") == bare_repo:
            matches.append(item)
    if len(matches) != 1:
        return None
    m = matches[0]
    org = m.get("org")
    project = m.get("project")
    repo = m.get("repo")
    if not (org and project and repo):
        return None
    return f"{org}/{project}/{repo}"


def resolve_full_gitlab_slug(workspace_id, bare_repo):
    """Find the full ``group/{...subgroups}/project`` slug for a bare name.

    Mirrors ``resolve_full_ado_triplet`` for the GitLab
    side. The symbol graph stores ``repo`` as the bare project name (the
    last slug segment) so the code-maps pointer key matches; the GitLab
    API needs the full slug. Because GitLab slugs have a variable number
    of slashes (nested subgroups), we cannot pass-through on a fixed
    slash count -- instead a value that already contains a ``/`` is
    treated as a full slug, and a bare name is resolved by scanning the
    workspace's ``GITLAB_REPO#`` rows for the one whose slug tail matches.

    Returns the full slug, or None when no row matches or multiple rows
    share the bare tail (ambiguous -- fail loudly rather than fetch from
    the wrong project).
    """
    from boto3.dynamodb.conditions import Key  # noqa: WPS433
    if not bare_repo:
        return None
    if "/" in bare_repo:
        return bare_repo
    table = _workspaces_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
        & Key("SK").begins_with("GITLAB_REPO#"),
    )
    matches = []
    for item in resp.get("Items", []):
        slug = item.get("slug") or item.get("SK", "").split("GITLAB_REPO#", 1)[-1]
        if slug and slug.rsplit("/", 1)[-1] == bare_repo:
            matches.append(slug)
    if len(matches) != 1:
        return None
    return matches[0]


def resolve_platform(workspace_id, repo_hint=None, provider=None):
    """Return the workspace's connected code-hosting platform.

    Result shape:
      {"platform": "github", "installation_id": "..."}
      {"platform": "azure_devops",
       "connection_id": "...", "org": "...", "project": "...", "repo": "..."}
      {"platform": "gitlab", "connection_id": "...", "slug": "..."}
      None  # no connection linked

    ``repo_hint`` is either a full slug/owner-name string (``owner/name``,
    ``org/project/repo``, or the variable-depth GitLab
    ``group/{...subgroups}/project``) or None. When present, the ADO and
    GitLab branches do a direct ``get_item`` on ``AZDO_REPO#{repo_hint}``
    / ``GITLAB_REPO#{repo_hint}``. That's the fast path used by the
    explicit-anchor mode (caller has the repo already). For the
    ``at_graph_sha`` path the caller resolves the full slug via
    ``resolve_full_ado_triplet`` /
    ``resolve_full_gitlab_slug`` first, then calls this
    with the full slug.

    ADR-050 Decision 6: the platform is resolved by DDB lookup of the
    enrolled ``*_REPO#`` rows, NOT by slash count. GitLab supports nested
    subgroups, so its slug has a VARIABLE number of slashes (>=2) and
    would collide with ADO's fixed 3-segment shape -- slash-count
    inference is ambiguous. Resolution order (when no explicit
    ``provider`` hint is supplied):
      1. GitHub short-circuit -- if any GITHUB_INSTALLATION# row exists.
      2. ADO fallback -- an AZDO_REPO# row matching the hint (or any
         AZDO_REPO# row on the workspace).
      3. GitLab fallback -- a GITLAB_REPO# row matching the hint (or any
         GITLAB_REPO# row on the workspace).
      4. None otherwise.

    ``provider`` (``"github"`` / ``"azure_devops"`` / ``"gitlab"``) is an
    optional caller-supplied hint that pins resolution to that platform,
    bypassing the ordered fallback -- honored per ADR-050 Decision 6 for
    callers that already know the source host.

    Fail-soft on DDB error: returns None so the caller emits the same
    clear "no code-hosting integration linked" error rather than
    routing to a partial-state fetch path.
    """
    from boto3.dynamodb.conditions import Key  # noqa: WPS433

    try:
        table = _workspaces_table()

        if provider in (None, "github"):
            gh_resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
                & Key("SK").begins_with("GITHUB_INSTALLATION#"),
                Limit=1,
            )
            gh_items = gh_resp.get("Items", [])
            if gh_items:
                installation_id = gh_items[0].get("installation_id")
                if installation_id:
                    return {
                        "platform": "github",
                        "installation_id": installation_id,
                    }
            if provider == "github":
                return None

        if provider in (None, "azure_devops"):
            # ADO branch. Direct-key path when repo_hint is a full triplet.
            if repo_hint and repo_hint.count("/") == 2:
                repo_resp = table.get_item(
                    Key={
                        "PK": f"WS#{workspace_id}",
                        "SK": f"AZDO_REPO#{repo_hint}",
                    }
                )
                item = repo_resp.get("Item")
                if item:
                    ado = _ado_platform_result(item)
                    if ado:
                        return ado

            # Scan/query fallback: no direct hint, or the direct-key miss.
            # Query all AZDO_REPO# rows on the workspace; return the first
            # one that has a connection_id. (No hint = caller only wants to
            # know the workspace has SOME ADO connection; router will reject
            # a repo-shape mismatch later.)
            ado_resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
                & Key("SK").begins_with("AZDO_REPO#"),
                Limit=1,
            )
            ado_items = ado_resp.get("Items", [])
            if ado_items:
                ado = _ado_platform_result(ado_items[0])
                if ado:
                    return ado
            if provider == "azure_devops":
                return None

        if provider in (None, "gitlab"):
            # GitLab branch (ADR-050). Direct-key path whenever a hint is
            # present -- unlike ADO we can't gate on slash count (nested
            # subgroups make it ambiguous), so we simply attempt the
            # direct get and fall through to a scan on a miss.
            if repo_hint:
                gl_resp = table.get_item(
                    Key={
                        "PK": f"WS#{workspace_id}",
                        "SK": f"GITLAB_REPO#{repo_hint}",
                    }
                )
                item = gl_resp.get("Item")
                if item:
                    gl = _gitlab_platform_result(item)
                    if gl:
                        return gl

            gl_resp = table.query(
                KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
                & Key("SK").begins_with("GITLAB_REPO#"),
                Limit=1,
            )
            gl_items = gl_resp.get("Items", [])
            if gl_items:
                gl = _gitlab_platform_result(gl_items[0])
                if gl:
                    return gl
            if provider == "gitlab":
                return None

        return None
    except ClientError:
        return None


def _ado_platform_result(item):
    """Build the ADO branch's platform_info dict from an AZDO_REPO# item."""
    connection_id = item.get("connection_id")
    org = item.get("org")
    project = item.get("project")
    repo = item.get("repo")
    if not (connection_id and org and project and repo):
        return None
    return {
        "platform": "azure_devops",
        "connection_id": connection_id,
        "org": org,
        "project": project,
        "repo": repo,
    }


def _gitlab_platform_result(item):
    """Build the GitLab branch's platform_info dict from a GITLAB_REPO# item.

    Each GITLAB_REPO# row carries its own ``connection_id`` (a workspace
    can span multiple GitLab connections) and the URL-decoded ``slug``
    (``group/{...subgroups}/project``). Falls back to deriving the slug
    from the SK when the attribute is absent.
    """
    connection_id = item.get("connection_id")
    slug = item.get("slug") or item.get("SK", "").split("GITLAB_REPO#", 1)[-1]
    if not (connection_id and slug):
        return None
    return {
        "platform": "gitlab",
        "connection_id": connection_id,
        "slug": slug,
    }


def bare_name(symbol_id):
    """Strip a canonical symbol id down to its bare tail.

    Mirrors the tail logic in symbol_graph_writer._derive_bare_name so
    relocation searches use the same name a user would type. Falls back
    to the raw id if no separator matches (shouldn't happen on canonical
    inputs, but defensive)."""
    if not symbol_id:
        return ""
    s = symbol_id
    # Strip the language prefix before the first single colon (after a
    # double-colon we treat them as namespace separators).
    if ":" in s and "::" not in s.split(":", 1)[0]:
        s = s.split(":", 1)[1]
    for sep in ("::", ".", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def relocate(window, bare_name):
    """Search a read window for a bare symbol name. Returns the line
    number of the first match or None when the symbol isn't in the
    window. Word-boundary match so unrelated identifiers like
    ``my_require_auth`` don't trigger on ``require_auth``."""

    if not bare_name:
        return None
    pat = re.compile(rf"\b{re.escape(bare_name)}\b")
    for entry in window:
        if pat.search(entry["text"]):
            return entry["line"]
    return None


def resolve_full_repo_name(workspace_id, bare_repo):
    """Find the owner-prefixed repo name ('owner/<bare>') for a bare repo
    name within a workspace.

    The symbol graph stores ``repo`` as the bare name (no owner) so the
    code-maps pointer key matches. The GitHub Contents API needs the
    owner-prefixed form. Resolve by scanning the workspace's
    ``GITHUB_REPO#`` rows for one whose tail equals the bare name.
    Returns the owner/bare string, or None when no matching row exists
    (e.g. the workspace never added the repo)."""
    from boto3.dynamodb.conditions import Key  # noqa: WPS433
    if not bare_repo or "/" in bare_repo:
        return bare_repo or None
    table = _workspaces_table()
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(f"WS#{workspace_id}")
        & Key("SK").begins_with("GITHUB_REPO#"),
    )
    for item in resp.get("Items", []):
        owner_repo = item.get("SK", "").split("GITHUB_REPO#", 1)[-1]
        if "/" in owner_repo and owner_repo.rsplit("/", 1)[-1] == bare_repo:
            return owner_repo
    return None


def platform_from_repo(repo):
    """Auto-detect code-hosting platform from the ``repo`` param shape.

    - ``owner/name`` (1 slash) -> ``github``
    - ``org/project/repo`` (2 slashes) -> ``azure_devops``
    - anything else -> ``None`` (caller returns a validation error)

    ADO project names in practice never contain ``/`` -- ADO's own web
    routing (``dev.azure.com/{org}/{project}/_git/{repo}``) enforces
    path-safe segments -- so a 2-slash triplet is unambiguous.
    """
    if not repo:
        return None
    parts = repo.split("/")
    if len(parts) == 2 and all(parts):
        return "github"
    if len(parts) == 3 and all(parts):
        return "azure_devops"
    return None


def resolve_via_graph(workspace_id, symbol, platform="github"):
    """Resolve a canonical symbol id to ``(repo, file_path, line, graph_sha)``
    using the symbol graph + the code-maps SHA-truth pointer.

    Returns one of:
    - dict with the resolved coordinates on success.
    - ``{"error": "missing"}`` when no definition is found.
    - ``{"error": "ambiguous", "sites": [...]}`` when multiple definitions
      exist (caller must disambiguate with explicit file_path/line).

    Pinning ``ref`` to the pointer's ``graph_sha`` eliminates the
    file:line skew that exists between the graph's pinned SHA and the
    repo's HEAD.

    The symbol graph stores ``repo`` as the bare name so the pointer
    key matches across platforms. The final fetcher needs the
    platform-specific full form: ``owner/name`` for GitHub or
    ``org/project/repo`` for ADO. ``platform`` selects which resolver
    to use.
    """
    from boto3.dynamodb.conditions import Key  # noqa: WPS433
    from shared import symbol_graph_reader  # type: ignore  # noqa: WPS433

    try:
        result = symbol_graph_reader.definitions(
            workspace_id,
            symbol,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE") or None,
            code_maps_table_name=os.environ.get("CODE_MAPS_TABLE") or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable:
        return {"error": "graph_unavailable"}

    sites = result.sites
    if not sites:
        return {"error": "missing"}
    if len(sites) > 1:
        return {
            "error": "ambiguous",
            "sites": [
                {"repo": s.repo, "file_path": s.file_path, "line": s.line}
                for s in sites
            ],
        }
    site = sites[0]

    repo_full, error = full_repo_for_platform(workspace_id, site.repo, platform)
    if error:
        return {"error": error}
    graph_sha = graph_sha_for_repo(workspace_id, site.repo)

    return {
        "repo": repo_full,
        "file_path": site.file_path,
        "line": site.line,
        "graph_sha": graph_sha,
    }


def full_repo_for_platform(workspace_id, site_repo, platform):
    """Resolve the graph's (usually bare) repo name to the host's full slug.

    The symbol graph stores ``repo`` as the bare name so the code-maps
    pointer key matches across platforms. The fetchers need
    ``owner/name`` (GitHub), ``org/project/repo`` (Azure DevOps) or
    ``group/.../project`` (GitLab). Returns ``(repo_full, None)`` or
    ``(None, error_message)``.
    """
    bare = (site_repo or "").rsplit("/", 1)[-1]
    if platform == "azure_devops":
        if site_repo.count("/") == 2:
            return site_repo, None
        looked_up = resolve_full_ado_triplet(workspace_id, bare)
        if not looked_up:
            return None, (
                f"workspace has no Azure DevOps repo row matching "
                f"bare name {site_repo!r}; cannot derive "
                "org/project for the ADO fetch"
            )
        return looked_up, None
    if platform == "gitlab":
        if "/" in site_repo:
            return site_repo, None
        looked_up = resolve_full_gitlab_slug(workspace_id, bare)
        if not looked_up:
            return None, (
                f"workspace has no GitLab repo row matching bare "
                f"name {site_repo!r}; cannot derive the "
                "group/.../project slug for the GitLab fetch"
            )
        return looked_up, None
    if "/" in site_repo:
        return site_repo, None
    looked_up = resolve_full_repo_name(workspace_id, site_repo)
    if not looked_up:
        return None, (
            f"workspace has no GitHub repo row matching bare "
            f"name {site_repo!r}; cannot derive owner for the "
            "GitHub fetch"
        )
    return looked_up, None


def graph_sha_for_repo(workspace_id, site_repo):
    """Commit SHA the workspace's symbol graph was built from, or None.

    Read from the code-maps SHA-truth pointer, keyed on the BARE repo
    name. Pinning a read to this SHA keeps graph line numbers and file
    content in step (no skew against HEAD).
    """
    code_maps_table = os.environ.get("CODE_MAPS_TABLE")
    if not code_maps_table:
        return None
    bare = (site_repo or "").rsplit("/", 1)[-1]
    try:
        table = _get_dynamodb().Table(code_maps_table)
        resp = table.get_item(
            Key={"PK": f"SYMBOL_GRAPH#{workspace_id}#{bare}", "SK": "POINTER"}
        )
        return resp.get("Item", {}).get("graph_sha")
    except ClientError as exc:
        logger.warning("read_source graph_sha lookup failed: %s", exc)
        return None


def fetch_file_ado(org, project, repo, file_path, token, ref=None):
    """Fetch the raw bytes of a file from Azure DevOps.

    Uses the ``/_apis/git/repositories/{repo}/items`` endpoint at
    ``dev.azure.com`` with ``api-version=7.1`` and ``$format=text``.
    When ``ref`` is provided it's sent as a commit SHA via
    ``versionDescriptor.version=<sha>`` +
    ``versionDescriptor.versionType=commit`` -- matching the GitHub
    path where ``ref`` is likewise a commit SHA in the
    ``at_graph_sha`` flow.

    Caps the read at ``MAX_FILE_BYTES``; over-cap raises
    ``ValueError`` so the caller surfaces the limit rather than
    return a silently-truncated window.

    Pre-flight guard: if ADO returns a JSON envelope (starts with
    ``{"``) instead of raw text, the URL builder or ``$format`` param
    dropped somewhere -- raise a distinct error rather than let
    ``splitlines()`` treat the envelope as one giant line.
    """
    params = {
        "path": file_path,
        "api-version": "7.1",
        "$format": "text",
    }
    if ref:
        params["versionDescriptor.version"] = ref
        params["versionDescriptor.versionType"] = "commit"
    query = urllib.parse.urlencode(params)
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org, safe='')}"
        f"/{urllib.parse.quote(project, safe='')}"
        f"/_apis/git/repositories/{urllib.parse.quote(repo, safe='')}/items?{query}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/plain",
            "User-Agent": "outcomeops-mcp/read_source",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(
            f"file {file_path} exceeds {MAX_FILE_BYTES}-byte "
            "read-source cap"
        )
    if raw.startswith(b'{"'):
        raise ValueError(
            "Azure DevOps returned a JSON envelope instead of raw file "
            "content; the $format=text query param may have been dropped"
        )
    return raw


def fetch_file_gitlab(slug, file_path, token, ref=None, base_url=None):
    """Fetch the raw bytes of a file from GitLab.

    Delegates to the shared GitLab API v4 client
    (``GET /projects/{enc}/repository/files/{enc}/raw?ref=<ref>``); the
    project is addressed by its URL-encoded full ``slug``
    (``group/{...subgroups}/project``) so nested subgroups work.

    GitLab's raw endpoint requires a ``ref``. When the caller doesn't
    pin one (the HEAD-of-default-branch case, matching the ADO fetcher's
    ref-omitted path), we resolve the project's default branch first and
    read at its tip. When ``ref`` IS supplied it's a commit SHA (the
    ``at_graph_sha`` flow) passed straight through -- GitLab accepts a
    SHA, branch, or tag uniformly in that slot.

    Caps the read at ``MAX_FILE_BYTES``; over-cap raises
    ``ValueError`` so the caller surfaces the limit rather than return a
    silently-truncated window (matching the github/ADO fetchers). The
    window slice around the anchor line happens in ``handle_read_source``
    after this returns.
    """
    from shared import gitlab_api  # noqa: WPS433 -- defer import
    effective_ref = ref or gitlab_api.get_default_branch(token, slug, base_url=base_url)
    if not effective_ref:
        raise ValueError(
            f"could not resolve a ref for GitLab project {slug!r} "
            "(no ref supplied and no default branch found)"
        )
    raw = gitlab_api.get_file_content(token, slug, file_path, effective_ref, base_url=base_url)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(
            f"file {file_path} exceeds {MAX_FILE_BYTES}-byte "
            "read-source cap"
        )
    return raw


def fetch_file_github(repo, file_path, token, ref=None):
    """Fetch the raw bytes of a file via the GitHub Contents API.

    Caps the read at ``MAX_FILE_BYTES``; files exceeding the
    cap raise ValueError so the caller can surface the limit rather than
    return a silently-truncated window.
    """
    quoted = urllib.parse.quote(file_path, safe="/")
    url = f"{_github_api()}/repos/{repo}/contents/{quoted}"
    if ref:
        url += f"?ref={urllib.parse.quote(ref, safe='')}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "outcomeops-mcp/read_source",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(
            f"file {file_path} exceeds {MAX_FILE_BYTES}-byte read-source cap"
        )
    return raw


# ---------------------------------------------------------------------------
# Window read: the one entry point both consumers share
# ---------------------------------------------------------------------------


def _host_label(platform: str) -> str:
    return {"github": "GitHub", "gitlab": "GitLab"}.get(platform, "Azure DevOps")


def fetch_raw(platform_info: Dict[str, Any], repo: str, file_path: str,
              ref: Optional[str] = None) -> bytes:
    """Mint a token for ``platform_info`` and fetch one file's bytes.

    ``repo`` is the host's full slug (``owner/name``, ``org/project/repo``
    or ``group/.../project``). Raises the fetcher's own errors
    (``urllib.error.*``, ``ValueError``, ``RuntimeError``); ``read_window``
    turns them into tool-facing error dicts.
    """
    platform = platform_info["platform"]
    if platform == "github":
        token = installation_token(platform_info["installation_id"])
        return fetch_file_github(repo, file_path, token, ref=ref)
    if platform == "gitlab":
        token, gl_base = gitlab_access_token(platform_info["connection_id"])
        return fetch_file_gitlab(repo, file_path, token, ref=ref, base_url=gl_base)
    org, project, repo_bare = repo.split("/")
    token = ado_access_token(platform_info["connection_id"])
    return fetch_file_ado(org, project, repo_bare, file_path, token, ref=ref)


def slice_window(text: str, line: int, context_lines: int) -> Dict[str, Any]:
    """Cut a ``2*context_lines+1`` window around ``line`` (1-indexed)."""
    lines = text.splitlines()
    total = len(lines)
    start = max(1, line - context_lines)
    end = min(total, line + context_lines)
    window = [{"line": i, "text": lines[i - 1]} for i in range(start, end + 1)]
    return {
        "lines": window,
        "total_lines": total,
        "window_start": start,
        "window_end": end,
        "clipped_to_file_bounds": start > (line - context_lines)
                                  or end < (line + context_lines),
    }


def read_window(platform_info: Dict[str, Any], repo: str, file_path: str,
                line: int, *, ref: Optional[str] = None,
                context_lines: int = DEFAULT_CONTEXT_LINES,
                symbol: str = "") -> Dict[str, Any]:
    """Fetch ``file_path`` and return a window around ``line``.

    On success returns the window dict plus ``repo`` / ``file_path`` /
    ``ref`` / ``anchor_line`` / ``relocated_to_line`` / ``relocated`` /
    ``platform``. When ``symbol`` is given, the window is searched for
    its bare name so a stale anchor self-corrects (``relocated_to_line``).
    On any failure returns ``{"error": "<message>"}``; never raises.
    """
    platform = platform_info.get("platform", "")
    host = _host_label(platform)
    try:
        raw = fetch_raw(platform_info, repo, file_path, ref=ref)
    except urllib.error.HTTPError as exc:
        return {"error": f"{host} returned {exc.code} for {repo}:{file_path}"}
    except urllib.error.URLError as exc:
        return {"error": f"{host} fetch failed for {repo}:{file_path}: {exc.reason}"}
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error("read_source unexpected error: %s", exc, exc_info=True)
        return {"error": "read_source failed (see logs)"}

    out = slice_window(raw.decode("utf-8", errors="replace"), line, context_lines)
    relocated_to_line = relocate(out["lines"], bare_name(symbol)) if symbol else None
    out.update({
        "repo": repo,
        "file_path": file_path,
        "ref": ref,
        "anchor_line": line,
        "symbol": symbol or None,
        "relocated_to_line": relocated_to_line,
        "relocated": relocated_to_line is not None and relocated_to_line != line,
        "platform": platform,
    })
    return out


# ---------------------------------------------------------------------------
# Definition windows for synthesis context (chat, ADR-030 v1.2)
# ---------------------------------------------------------------------------

DEFAULT_DEFINITION_LINES = 80
DEFAULT_MAX_DEFINITIONS = 3
DEFAULT_MAX_BLOCK_CHARS = 24_000
# Lines shown above the definition line, so a decorator or a comment
# attached to the definition is not cut off.
_LEADING_LINES = 2


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def source_lines_config_from_env() -> Dict[str, Any]:
    """Chat source-lines settings, bounded so a typo cannot flood context.

    ``CHAT_SOURCE_LINES_ENABLED`` (default true) is the kill switch;
    ``CHAT_SOURCE_LINES_PER_SYMBOL`` (default 80, 10-300) and
    ``CHAT_SOURCE_MAX_SYMBOLS`` (default 3, 1-10) size the block.
    """
    enabled = os.environ.get("CHAT_SOURCE_LINES_ENABLED", "true").strip().lower()
    return {
        "enabled": enabled not in ("false", "0", "no", "off"),
        "max_lines": _int_env("CHAT_SOURCE_LINES_PER_SYMBOL", DEFAULT_DEFINITION_LINES, 10, 300),
        "max_definitions": _int_env("CHAT_SOURCE_MAX_SYMBOLS", DEFAULT_MAX_DEFINITIONS, 1, 10),
    }


def read_definition(definition: Dict[str, Any], *,
                    max_lines: int = DEFAULT_DEFINITION_LINES) -> Dict[str, Any]:
    """Read the real lines of one graph definition site.

    ``definition`` carries ``workspace_id``, ``symbol``, ``repo`` (as the
    graph stores it), ``file_path`` and ``line`` (the definition line).
    The read is pinned to the symbol graph's commit, so the graph's line
    number matches the content. Returns ``symbol``, ``repo``,
    ``file_path``, ``ref``, ``lines`` (``[{"line", "text"}]``) and
    ``truncated``; or ``{"symbol", "error"}``. Never raises.
    """
    symbol = definition.get("symbol", "")
    workspace_id = definition.get("workspace_id", "")
    site_repo = definition.get("repo", "")
    file_path = definition.get("file_path", "")
    try:
        line = int(definition.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    if not (workspace_id and site_repo and file_path and line >= 1):
        return {"symbol": symbol, "error": "definition site is incomplete"}

    platform_info = resolve_platform(workspace_id)
    if platform_info is None:
        return {"symbol": symbol, "error": "no code-hosting connection on the workspace"}
    repo_full, error = full_repo_for_platform(
        workspace_id, site_repo, platform_info["platform"],
    )
    if error:
        return {"symbol": symbol, "error": error}
    ref = graph_sha_for_repo(workspace_id, site_repo)

    # A window centred on the line wide enough to cover the body; the
    # head of it is dropped below so the definition leads.
    window = read_window(
        platform_info, repo_full, file_path, line,
        ref=ref, context_lines=min(max_lines, MAX_CONTEXT_LINES), symbol=symbol,
    )
    if "error" in window:
        return {"symbol": symbol, "error": window["error"]}

    # Self-correct a stale anchor: start at the symbol's actual line in
    # the window when the graph's line has drifted.
    anchor = window.get("relocated_to_line") or line
    first = max(1, anchor - _LEADING_LINES)
    last = anchor + max_lines - 1
    lines = [e for e in window["lines"] if first <= e["line"] <= last]
    return {
        "symbol": symbol,
        "repo": repo_full,
        "file_path": file_path,
        "ref": ref,
        "lines": lines,
        "truncated": bool(lines) and lines[-1]["line"] < window["total_lines"]
                     and lines[-1]["line"] >= last,
    }


def read_definitions(definitions: List[Dict[str, Any]], *,
                     max_definitions: int = DEFAULT_MAX_DEFINITIONS,
                     max_lines: int = DEFAULT_DEFINITION_LINES) -> List[Dict[str, Any]]:
    """Read up to ``max_definitions`` distinct definition sites, in order."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for d in definitions or []:
        key = (d.get("workspace_id"), d.get("repo"), d.get("file_path"), d.get("line"))
        if key in seen:
            continue
        seen.add(key)
        out.append(read_definition(d, max_lines=max_lines))
        if len(out) >= max_definitions:
            break
    return out


def build_source_lines_block(windows: List[Dict[str, Any]], *,
                             max_chars: int = DEFAULT_MAX_BLOCK_CHARS) -> str:
    """Render definition windows as a context block for synthesis.

    Each window is labelled with its file, line span and commit so the
    model can cite it. Windows that could not be read are listed with the
    reason, so the model says the value is unverified instead of guessing.
    Returns ``""`` when there is nothing to show.
    """
    if not windows:
        return ""
    parts = [
        "SOURCE LINES (read from the repository at the symbol graph's commit; "
        "this is the actual code. It is authoritative over code-map summaries "
        "and documentation for exact values: error codes, messages, status "
        "codes, field names, constants. Quote from it and cite file:line.)"
    ]
    used = len(parts[0])
    for w in windows:
        if "error" in w:
            entry = f"\n\n### {w.get('symbol') or '(unknown symbol)'}\nSource could not be read: {w['error']}"
        else:
            lines = w.get("lines") or []
            if not lines:
                continue
            ref = (w.get("ref") or "")[:12] or "default branch"
            body = "\n".join(f"{e['line']:>5} | {e['text']}" for e in lines)
            more = "\n(window ends here; the definition continues)" if w.get("truncated") else ""
            entry = (
                f"\n\n### {w.get('symbol')} -- {w['repo']}:{w['file_path']}:"
                f"{lines[0]['line']}-{lines[-1]['line']} @ {ref}\n```\n{body}\n```{more}"
            )
        if used + len(entry) > max_chars:
            parts.append("\n\n(further source windows omitted to stay within the context budget)")
            break
        parts.append(entry)
        used += len(entry)
    if len(parts) == 1:
        return ""
    return "".join(parts)
