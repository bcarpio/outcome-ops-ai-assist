"""
Provider-agnostic source-content fetcher for the code-map pipeline.

The code-map pipeline (process-repo-summary -> process-batch-summary)
needs to read each tracked file's content to feed into the code-map
LLM prompt and the symbol-graph extractor. The provider determines
WHERE to read from:

- ``github``: live api.github.com / raw.githubusercontent.com path
  (PAT-authenticated). github-sync today does NOT write application-
  repo source to S3; the downstream worker fetches at code-map-
  generation time. Legacy.
- ``azure_devops``: S3 mirror at
  ``workspaces/{workspace_id}/azure_devops/{slug}/{path}``. azure-
  devops-sync wrote the file there during the sync run (per chunk
  05a). No live API hit at code-map-generation time.
- (future) ``gitlab``: S3 mirror at
  ``workspaces/{workspace_id}/gitlab/{slug}/{path}``. gitlab-sync
  follows the ADO pattern -- write to S3 first.

The asymmetry between github (live API) and ADO/GitLab (S3 mirror)
is intentional for v1. github-sync is mature, and refactoring it
to write source to S3 first would be a large change with no
functional benefit. New providers default to S3-mirror because:

- They don't have the GitHub-App-installation-token plumbing
- Their tokens are short-lived OAuth (refresh-rotation cost per call
  is higher than github's installation token)
- The S3 mirror is already required for chat-side full-text retrieval
  (process-documents indexes from there)

When github-sync also moves to writing source to S3 (a separate
plan), the dispatcher below collapses to a single "read from S3"
branch and the provider field becomes a routing-only attribute.
"""

import logging
from typing import Optional
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


# HTTP timeouts used by the SHA-specific PR-check fetch path. Deliberately
# distinct from the snapshot-latest path so a future rate-limit tuning
# does not couple the two use cases.
_PR_FETCH_TIMEOUT_SECONDS = 60
_ADO_ITEMS_API_VERSION = "7.0"


_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3")
    return _s3_client


class SourceFetchError(Exception):
    """Wraps any fetch-side failure so callers can map to retry-vs-skip."""


def fetch_repo_file(
    provider: str,
    file_path: str,
    *,
    # github branch
    repo_project: Optional[str] = None,
    default_branch: Optional[str] = None,
    github_token: Optional[str] = None,
    # azure_devops branch
    workspace_id: Optional[str] = None,
    slug: Optional[str] = None,
    kb_bucket: Optional[str] = None,
) -> str:
    """Return one file's text content.

    Caller responsibilities:
    - ``provider`` MUST be set (no implicit default; the SQS message
      shape includes a ``provider`` field per the chunk-05a contract).
    - Per-provider kwargs MUST be populated:
      - github: ``repo_project`` (owner/repo) + ``default_branch`` +
        ``github_token``
      - azure_devops: ``workspace_id`` + ``slug`` (org/project/repo)
        + ``kb_bucket``
    """
    if provider == "azure_devops":
        if not (workspace_id and slug and kb_bucket):
            raise SourceFetchError(
                "azure_devops fetch needs workspace_id + slug + kb_bucket",
            )
        return _fetch_from_s3_mirror(
            workspace_id=workspace_id,
            provider_dir="azure_devops",
            slug=slug,
            file_path=file_path,
            kb_bucket=kb_bucket,
        )

    if provider == "github":
        if not (repo_project and default_branch and github_token):
            raise SourceFetchError(
                "github fetch needs repo_project + default_branch + github_token",
            )
        return _fetch_from_raw_github(
            repo_project=repo_project,
            default_branch=default_branch,
            file_path=file_path,
            github_token=github_token,
        )

    raise SourceFetchError(f"Unknown provider for source fetch: {provider}")


def _fetch_from_s3_mirror(
    *,
    workspace_id: str,
    provider_dir: str,
    slug: str,
    file_path: str,
    kb_bucket: str,
) -> str:
    """Read from the sync-worker-written S3 mirror.

    Key shape matches what azure-devops-sync writes:
    ``workspaces/{workspace_id}/{provider_dir}/{slug}/{file_path}``
    with the file_path's leading slash stripped (ADO paths come in
    leading-slashed; the upload normalizes them).
    """
    clean_path = file_path.lstrip("/")
    s3_key = f"workspaces/{workspace_id}/{provider_dir}/{slug}/{clean_path}"
    try:
        response = _get_s3().get_object(Bucket=kb_bucket, Key=s3_key)
    except Exception as e:
        raise SourceFetchError(
            f"S3 mirror read failed for s3://{kb_bucket}/{s3_key}: {e}",
        )
    body = response["Body"].read()
    # Treat content as text. Binary blobs (skipped by sync's
    # _is_indexable filter) shouldn't reach this path; decode errors
    # would indicate a contract drift between sync filter + downstream
    # consumption. errors='replace' keeps the pipeline moving on
    # marginal cases (UTF-8 with one bad byte) instead of crashing.
    return body.decode("utf-8", errors="replace")


def _fetch_from_raw_github(
    *,
    repo_project: str,
    default_branch: str,
    file_path: str,
    github_token: str,
) -> str:
    encoded_path = quote(file_path, safe="/")
    url = (
        f"https://raw.githubusercontent.com/{repo_project}/"
        f"{default_branch}/{encoded_path}"
    )
    headers = {
        "Authorization": f"Bearer {github_token}",
        "User-Agent": "outcome-ops-process-batch-summary",
    }
    request = Request(url, headers=headers)
    try:
        with urlopen(request) as response:
            return response.read().decode("utf-8")
    except Exception as e:
        raise SourceFetchError(
            f"raw.githubusercontent.com fetch failed for {repo_project}/{file_path}: {e}",
        )


# =============================================================================
# SHA-specific fetch (PR-check path)
# =============================================================================
#
# The functions below fetch a single file's bytes at a SPECIFIC commit SHA
# rather than the "latest synced snapshot" the code-map pipeline uses above.
# PR-check needs SHA-specific reads because a PR's head commit may pre-date
# any sync run (or be a branch the sync worker will never materialize into
# S3). Consumers (`lambda/process-pr-check/check_handlers/*`) call
# ``fetch_file_at_sha`` with a caller-provided access token so this shared
# module stays free of SSM/OAuth-refresh concerns.


def fetch_file_at_sha(
    provider: str,
    file_path: str,
    sha: str,
    *,
    # github branch
    repository: Optional[str] = None,
    github_token: Optional[str] = None,
    # azure_devops branch
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    ado_version_type: str = "commit",
) -> bytes:
    """Return one file's raw bytes at a specific ref position.

    Args:
        provider: ``"github"`` or ``"azure_devops"``.
        file_path: Repo-relative path (e.g. ``"src/foo.py"``). Both
            providers tolerate a leading slash but the callers should
            normalize to no-leading-slash.
        sha: Ref value. When ``ado_version_type="commit"`` (the
            default), this is a full commit SHA. When
            ``ado_version_type="branch"``, this is a branch NAME (e.g.
            ``"main"``). GitHub's raw endpoint accepts either
            uniformly in the ref slot, so the same value works both
            ways there.
        repository: GitHub ``owner/repo`` string. Required for
            ``provider="github"``.
        github_token: GitHub PAT. Required for ``provider="github"``.
        ado_org: ADO org name. Required for ``provider="azure_devops"``.
        ado_project: ADO project name. Required for ``provider="azure_devops"``.
        ado_repo: ADO repo name. Required for ``provider="azure_devops"``.
        ado_access_token: ADO OAuth access token (already refreshed by
            the caller via ``azure_devops_token_manager``). Required for
            ``provider="azure_devops"``.
        ado_version_type: ADO ``versionDescriptor.versionType`` value.
            ``"commit"`` (default) or ``"branch"``. Ignored on
            ``provider="github"``.

    Returns:
        Raw file bytes. Text-file callers should ``.decode("utf-8", "replace")``.

    Raises:
        SourceFetchError: On any auth, network, or 4xx/5xx response.
    """
    if provider == "github":
        if not (repository and github_token):
            raise SourceFetchError(
                "github fetch_file_at_sha needs repository + github_token",
            )
        return _fetch_from_raw_github_at_sha(
            repository=repository,
            file_path=file_path,
            sha=sha,
            github_token=github_token,
        )

    if provider == "azure_devops":
        if not (ado_org and ado_project and ado_repo and ado_access_token):
            raise SourceFetchError(
                "azure_devops fetch_file_at_sha needs ado_org + ado_project + "
                "ado_repo + ado_access_token",
            )
        if ado_version_type not in ("commit", "branch"):
            raise SourceFetchError(
                f"ado_version_type must be 'commit' or 'branch', got {ado_version_type!r}",
            )
        return _fetch_from_ado_at_sha(
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
            file_path=file_path,
            sha=sha,
            ado_access_token=ado_access_token,
            version_type=ado_version_type,
        )

    raise SourceFetchError(
        f"Unknown provider for fetch_file_at_sha: {provider}",
    )


def _fetch_from_raw_github_at_sha(
    *,
    repository: str,
    file_path: str,
    sha: str,
    github_token: str,
) -> bytes:
    """Fetch a file at a specific SHA from raw.githubusercontent.com.

    raw.githubusercontent.com accepts a commit SHA in place of a branch
    ref, so the URL shape is identical to the branch-tip path used by
    ``_fetch_from_raw_github``. Returns raw bytes rather than text so
    binary files (LICENSE_COMPLIANCE walks .lock/.jar/.zip in some
    ecosystems) round-trip losslessly.
    """
    encoded_path = quote(file_path.lstrip("/"), safe="/")
    url = (
        f"https://raw.githubusercontent.com/{repository}/"
        f"{sha}/{encoded_path}"
    )
    headers = {
        "Authorization": f"Bearer {github_token}",
        "User-Agent": "outcome-ops-process-pr-check",
    }
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=_PR_FETCH_TIMEOUT_SECONDS) as response:
            return response.read()
    except Exception as e:
        raise SourceFetchError(
            f"raw.githubusercontent.com fetch@{sha[:8]} failed for "
            f"{repository}/{file_path}: {e}",
        )


def _fetch_from_ado_at_sha(
    *,
    ado_org: str,
    ado_project: str,
    ado_repo: str,
    file_path: str,
    sha: str,
    ado_access_token: str,
    version_type: str = "commit",
) -> bytes:
    """Fetch a file at a specific ref position from Azure DevOps.

    Uses the ADO ``items`` endpoint with
    ``versionDescriptor.versionType={version_type}``. Two flags are
    load-bearing (matches the pattern in
    ``lambda/process-pr-check/azure_devops_api.get_file_content`` and was
    live-burned on 2026-06-05 during first sync):

    - ``download=true`` tells ADO to return blob content vs the JSON object
      descriptor.
    - ``Accept: */*`` overrides the default ``application/json`` so the
      blob bytes come through unchanged.

    ``version_type`` is ``"commit"`` by default (chunk 01 behavior).
    Chunk 04 added ``"branch"`` for reads at the branch tip.
    """
    query = urlencode({
        "path": "/" + file_path.lstrip("/"),
        "download": "true",
        "versionDescriptor.version": sha,
        "versionDescriptor.versionType": version_type,
        "api-version": _ADO_ITEMS_API_VERSION,
    })
    url = (
        f"https://dev.azure.com/{ado_org}/{ado_project}/_apis/git/"
        f"repositories/{ado_repo}/items?{query}"
    )
    headers = {
        "Authorization": f"Bearer {ado_access_token}",
        "Accept": "*/*",
        "User-Agent": "outcome-ops-process-pr-check",
    }
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=_PR_FETCH_TIMEOUT_SECONDS) as response:
            return response.read()
    except Exception as e:
        raise SourceFetchError(
            f"ADO items fetch@{sha[:8]} ({version_type}) failed for "
            f"{ado_org}/{ado_project}/{ado_repo}/{file_path}: {e}",
        )
