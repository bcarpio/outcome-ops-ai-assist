"""
Provider-agnostic pull-request metadata retrieval.

The PR-check pipeline (``lambda/process-pr-check/``) needs to know a
PR's head/base commit SHAs, the list of changed files, and a few
identifiers (author, title, target branch) to feed into the check
handlers. GitHub and Azure DevOps expose these through different REST
shapes; this module unifies them behind ``fetch_pr_metadata`` so the
check handlers stay provider-agnostic.

Caller responsibilities:

- Token acquisition. This module does NOT touch SSM, DDB, or OAuth
  refresh. The caller acquires the access token (GitHub PAT or ADO
  OAuth access token via ``azure_devops_token_manager.refresh_access_token``)
  and passes it in.
- Provider selection. The caller inspects the SQS message's
  ``provider`` field and calls with the matching kwargs.

The two providers' response shapes and endpoint paths are documented
in the branch functions below.
"""

import json
import logging
from typing import List, NamedTuple, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


_PR_METADATA_TIMEOUT_SECONDS = 60
_ADO_API_VERSION = "7.0"
_GITHUB_API = "https://api.github.com"
_ADO_API = "https://dev.azure.com"


class PRMetadataError(Exception):
    """Wraps any PR-metadata retrieval failure so callers can map to
    retry-vs-skip. Distinct from ``SourceFetchError`` so a caller that
    can tolerate a missing file but not a missing PR can differentiate."""


class PRMetadata(NamedTuple):
    """Provider-agnostic pull-request metadata.

    Every check handler consumes some subset of these fields. Fields
    that don't map cleanly across providers (labels, reviewers, draft
    status, etc.) are deliberately excluded — this NamedTuple is the
    stable common surface, not a general-purpose PR object.
    """
    head_sha: str
    base_sha: str
    base_branch: str
    changed_files: List[str]
    author: str
    title: str


def fetch_pr_metadata(
    provider: str,
    pr_number: int,
    *,
    # github branch
    repository: Optional[str] = None,
    github_token: Optional[str] = None,
    # azure_devops branch
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
) -> PRMetadata:
    """Fetch a PR's metadata + changed-file list from the source provider.

    Args:
        provider: ``"github"`` or ``"azure_devops"``.
        pr_number: The PR number as it appears in the customer's UI.
        repository: GitHub ``owner/repo``. Required for github.
        github_token: GitHub PAT. Required for github.
        ado_org: ADO org name. Required for azure_devops.
        ado_project: ADO project name. Required for azure_devops.
        ado_repo: ADO repo name. Required for azure_devops.
        ado_access_token: ADO OAuth access token, pre-refreshed by
            caller. Required for azure_devops.

    Returns:
        PRMetadata NamedTuple.

    Raises:
        PRMetadataError: On any auth, network, or unexpected-shape error.
    """
    if provider == "github":
        if not (repository and github_token):
            raise PRMetadataError(
                "github fetch_pr_metadata needs repository + github_token",
            )
        return _fetch_pr_from_github(
            repository=repository,
            pr_number=pr_number,
            github_token=github_token,
        )

    if provider == "azure_devops":
        if not (ado_org and ado_project and ado_repo and ado_access_token):
            raise PRMetadataError(
                "azure_devops fetch_pr_metadata needs ado_org + ado_project "
                "+ ado_repo + ado_access_token",
            )
        return _fetch_pr_from_ado(
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
            pr_number=pr_number,
            ado_access_token=ado_access_token,
        )

    raise PRMetadataError(f"Unknown provider for fetch_pr_metadata: {provider}")


def _fetch_pr_from_github(
    *,
    repository: str,
    pr_number: int,
    github_token: str,
) -> PRMetadata:
    """Fetch PR metadata + changed files from GitHub.

    Two REST calls:
      GET /repos/{repo}/pulls/{n}         — head/base SHAs, author, title, target branch
      GET /repos/{repo}/pulls/{n}/files   — changed-file list (paginated)

    GitHub's ``pulls/{n}/files`` paginates at 30 items by default; we
    request 100 per page and follow ``Link: <...>; rel="next"`` until
    exhaustion. Real PRs rarely exceed one page but the loop is cheap
    and correctness-preserving for the tail.
    """
    pr_url = f"{_GITHUB_API}/repos/{repository}/pulls/{pr_number}"
    pr_body = _github_get(pr_url, github_token)

    try:
        head_sha = pr_body["head"]["sha"]
        base_sha = pr_body["base"]["sha"]
        base_branch = pr_body["base"]["ref"]
        author = pr_body["user"]["login"]
        title = pr_body.get("title") or ""
    except (KeyError, TypeError) as e:
        raise PRMetadataError(
            f"unexpected GitHub PR shape for {repository}#{pr_number}: {e}",
        )

    files_url = f"{_GITHUB_API}/repos/{repository}/pulls/{pr_number}/files?per_page=100"
    changed_files: List[str] = []
    while files_url:
        page, next_url = _github_get_with_next(files_url, github_token)
        for entry in page or []:
            filename = entry.get("filename")
            if filename:
                changed_files.append(filename)
        files_url = next_url

    return PRMetadata(
        head_sha=head_sha,
        base_sha=base_sha,
        base_branch=base_branch,
        changed_files=changed_files,
        author=author,
        title=title,
    )


def _fetch_pr_from_ado(
    *,
    ado_org: str,
    ado_project: str,
    ado_repo: str,
    pr_number: int,
    ado_access_token: str,
) -> PRMetadata:
    """Fetch PR metadata + changed files from Azure DevOps.

    Two REST calls:
      GET .../pullrequests/{n}                          — head/base SHAs, author, title, target branch
      GET .../pullrequests/{n}/iterations/{last}/changes — changed-file list

    Notable shape deltas from GitHub:

    - Head SHA is ``lastMergeSourceCommit.commitId``; base SHA is
      ``lastMergeTargetCommit.commitId``. ADO exposes these as merge-
      preview commits; for PR-check purposes they're the correct head
      and base for a snapshot analysis.
    - ``targetRefName`` comes as ``refs/heads/{branch}``; strip the
      prefix to match GitHub's ``base.ref``.
    - Author is ``createdBy.uniqueName`` (the email-like Entra ID
      identifier).
    - Changed files live under the iterations/{id}/changes endpoint.
      We use the last iteration index (from ``codeReviewId`` shape isn't
      reliable across ADO versions; we take the max iteration).
    - Change entries wrap the actual path in
      ``changeEntries[i].item.path``. Filter to those with
      ``item.gitObjectType == "blob"`` to skip folder-level entries.
    """
    pr_url = _ado_url(
        f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/"
        f"repositories/{ado_repo}/pullrequests/{pr_number}",
    )
    pr_body = _ado_get(pr_url, ado_access_token)

    try:
        head_sha = pr_body["lastMergeSourceCommit"]["commitId"]
        base_sha = pr_body["lastMergeTargetCommit"]["commitId"]
        raw_target = pr_body["targetRefName"]
        base_branch = raw_target[len("refs/heads/"):] if raw_target.startswith("refs/heads/") else raw_target
        author = pr_body.get("createdBy", {}).get("uniqueName") or pr_body.get("createdBy", {}).get("displayName") or ""
        title = pr_body.get("title") or ""
    except (KeyError, TypeError) as e:
        raise PRMetadataError(
            f"unexpected ADO PR shape for {ado_org}/{ado_project}/{ado_repo}#{pr_number}: {e}",
        )

    iterations_url = _ado_url(
        f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/"
        f"repositories/{ado_repo}/pullrequests/{pr_number}/iterations",
    )
    iterations_body = _ado_get(iterations_url, ado_access_token)
    iterations = iterations_body.get("value", []) or []
    if not iterations:
        return PRMetadata(
            head_sha=head_sha,
            base_sha=base_sha,
            base_branch=base_branch,
            changed_files=[],
            author=author,
            title=title,
        )
    last_iteration_id = max(int(i.get("id", 0)) for i in iterations)

    changes_url = _ado_url(
        f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/"
        f"repositories/{ado_repo}/pullrequests/{pr_number}/iterations/"
        f"{last_iteration_id}/changes",
    )
    changes_body = _ado_get(changes_url, ado_access_token)

    changed_files: List[str] = []
    for entry in changes_body.get("changeEntries", []) or []:
        item = entry.get("item") or {}
        if item.get("gitObjectType") != "blob":
            continue
        raw_path = item.get("path")
        if raw_path:
            changed_files.append(raw_path.lstrip("/"))

    return PRMetadata(
        head_sha=head_sha,
        base_sha=base_sha,
        base_branch=base_branch,
        changed_files=changed_files,
        author=author,
        title=title,
    )


# =============================================================================
# HTTP helpers
# =============================================================================


def _github_get(url: str, token: str) -> dict:
    """Single-page GitHub GET returning parsed JSON."""
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_PR_METADATA_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise PRMetadataError(f"GitHub GET {url} failed: {e}")


def _github_get_with_next(url: str, token: str):
    """GitHub GET returning (body, next_url) — next_url from Link header
    or ``None`` if this was the last page."""
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_PR_METADATA_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
            link_header = response.headers.get("Link") or ""
    except Exception as e:
        raise PRMetadataError(f"GitHub GET {url} failed: {e}")

    next_url = _parse_github_link_next(link_header)
    return body, next_url


def _parse_github_link_next(link_header: str) -> Optional[str]:
    """Extract the rel=next URL from a GitHub Link header, or None."""
    if not link_header:
        return None
    for segment in link_header.split(","):
        segment = segment.strip()
        if 'rel="next"' not in segment:
            continue
        left = segment.find("<")
        right = segment.find(">")
        if left == -1 or right == -1 or right <= left:
            return None
        return segment[left + 1:right]
    return None


def _ado_get(url: str, access_token: str) -> dict:
    """Single-page ADO GET returning parsed JSON."""
    request = Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_PR_METADATA_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise PRMetadataError(f"ADO GET {url} failed: {e}")


def _ado_url(base: str, extra_params: Optional[dict] = None) -> str:
    """Append the api-version query param (and any extras) to an ADO URL."""
    params = {"api-version": _ADO_API_VERSION}
    if extra_params:
        params.update(extra_params)
    return f"{base}?{urlencode(params)}"
