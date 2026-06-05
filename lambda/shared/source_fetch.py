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
from urllib.parse import quote
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


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
