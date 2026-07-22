"""
Provider-agnostic pull-request commit-list retrieval.

The LICENSE_COMPLIANCE check's Phase 2 ("AI-generated code detection")
inspects the PR's commit list for markers like `Co-Authored-By: Claude`
in commit messages. GitHub and Azure DevOps expose commits through
different REST shapes; this module unifies them behind
``fetch_pr_commits`` so the check handler stays provider-agnostic.

One notable ADO subtlety: the pull-request-commits endpoint returns
each commit's ``comment`` field TRUNCATED (~80 chars, subject only)
with a ``commentTruncated`` flag. Co-Authored-By trailers usually live
at the END of the message body, so a truncated read misses them. When
``commentTruncated=true`` we refetch the individual commit via
``/commits/{commitId}`` to get the full comment body. That's an N+1
query in principle, but Phase 2 is only interesting when the PR has
some AI-authored commits at all — for a normal 1-3 commit PR the
overhead is negligible.
"""

import json
import logging
from typing import List, NamedTuple, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


_PR_COMMITS_TIMEOUT_SECONDS = 30
_ADO_API_VERSION = "7.0"
# GHES-aware (shared.github_base): api.github.com on SaaS, {base}/api/v3
# on GHES. Env is static per deploy so import-time resolution is fine.
from shared.github_base import github_api_base as _github_api_base

_GITHUB_API = _github_api_base()
_ADO_API = "https://dev.azure.com"


class PRCommitsError(Exception):
    """Wraps any PR-commits retrieval failure. Distinct from
    ``PRMetadataError`` so a caller that tolerates a missing commit
    list (Phase 2 is advisory) but not missing PR metadata can differ."""


class PRCommit(NamedTuple):
    """Provider-agnostic PR commit."""
    sha: str
    message: str
    author: str


def fetch_pr_commits(
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
    # gitlab branch
    gitlab_project_path: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
    gitlab_base_url: Optional[str] = None,
) -> List[PRCommit]:
    """Fetch the ordered list of commits in a PR from the source provider.

    Args:
        provider: ``"github"``, ``"azure_devops"``, or ``"gitlab"``.
        pr_number: PR number (GitLab: the MR ``iid``).
        repository: GitHub ``owner/repo``. Required for github.
        github_token: GitHub PAT. Required for github.
        ado_org / ado_project / ado_repo: ADO context. Required for ADO.
        ado_access_token: ADO OAuth access token (pre-refreshed).
            Required for ADO.
        gitlab_project_path / gitlab_access_token: GitLab context.
            Required for gitlab. GitLab's MR-commits endpoint returns
            the FULL ``message`` field, so no ADO-style truncation
            refetch is needed.

    Returns:
        List of ``PRCommit`` in the order the provider returns them.

    Raises:
        PRCommitsError on any auth, network, or unexpected-shape error.
    """
    if provider == "github":
        if not (repository and github_token):
            raise PRCommitsError(
                "github fetch_pr_commits needs repository + github_token",
            )
        return _fetch_from_github(
            repository=repository,
            pr_number=pr_number,
            github_token=github_token,
        )

    if provider == "azure_devops":
        if not (ado_org and ado_project and ado_repo and ado_access_token):
            raise PRCommitsError(
                "azure_devops fetch_pr_commits needs ado_org + ado_project "
                "+ ado_repo + ado_access_token",
            )
        return _fetch_from_ado(
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
            pr_number=pr_number,
            ado_access_token=ado_access_token,
        )

    if provider == "gitlab":
        if not (gitlab_project_path and gitlab_access_token):
            raise PRCommitsError(
                "gitlab fetch_pr_commits needs gitlab_project_path + "
                "gitlab_access_token",
            )
        return _fetch_from_gitlab(
            project_path=gitlab_project_path,
            mr_iid=pr_number,
            access_token=gitlab_access_token,
            base_url=gitlab_base_url,
        )

    raise PRCommitsError(f"Unknown provider for fetch_pr_commits: {provider}")


def _fetch_from_gitlab(
    *,
    project_path: str,
    mr_iid: int,
    access_token: str,
    base_url: Optional[str] = None,
) -> List[PRCommit]:
    """GET /projects/{enc}/merge_requests/{iid}/commits via the shared
    GitLab client. ``message`` is full (subject + body + trailers), so
    Co-Authored-By detection works without a per-commit refetch."""
    from shared import gitlab_api  # deferred: keeps this module import-light

    try:
        entries = gitlab_api.get_merge_request_commits(
            access_token, project_path, mr_iid, base_url=base_url)
    except Exception as e:
        raise PRCommitsError(
            f"GitLab MR commits fetch failed for {project_path}!{mr_iid}: {e}",
        )

    commits: List[PRCommit] = []
    for entry in entries or []:
        try:
            sha = entry["id"]
            message = entry.get("message") or entry.get("title") or ""
            author = entry.get("author_name") or ""
        except (KeyError, TypeError) as e:
            raise PRCommitsError(
                f"unexpected GitLab commit shape for {project_path}!{mr_iid}: {e}",
            )
        commits.append(PRCommit(sha=sha, message=message, author=author))
    return commits


def _fetch_from_github(
    *,
    repository: str,
    pr_number: int,
    github_token: str,
) -> List[PRCommit]:
    """GET /repos/{repo}/pulls/{n}/commits returns full commit messages
    (no truncation)."""
    url = f"{_GITHUB_API}/repos/{repository}/pulls/{pr_number}/commits"
    body = _github_get(url, github_token)
    commits: List[PRCommit] = []
    for entry in body or []:
        try:
            sha = entry["sha"]
            message = entry["commit"]["message"]
            author = entry["commit"]["author"]["name"]
        except (KeyError, TypeError) as e:
            raise PRCommitsError(
                f"unexpected GitHub commit shape for {repository}#{pr_number}: {e}",
            )
        commits.append(PRCommit(sha=sha, message=message, author=author))
    return commits


def _fetch_from_ado(
    *,
    ado_org: str,
    ado_project: str,
    ado_repo: str,
    pr_number: int,
    ado_access_token: str,
) -> List[PRCommit]:
    """GET .../pullRequests/{n}/commits returns each ``comment`` field
    TRUNCATED at ~80 chars with ``commentTruncated=True``. For truncated
    entries we refetch the individual commit via /commits/{commitId} to
    get the full body — Co-Authored-By trailers live in the tail."""
    list_url = _ado_url(
        f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/"
        f"repositories/{ado_repo}/pullRequests/{pr_number}/commits",
    )
    list_body = _ado_get(list_url, ado_access_token)

    commits: List[PRCommit] = []
    for entry in list_body.get("value", []) or []:
        try:
            sha = entry["commitId"]
            message = entry.get("comment") or ""
            author_block = entry.get("author") or {}
            author = author_block.get("name") or author_block.get("email") or ""
        except (KeyError, TypeError) as e:
            raise PRCommitsError(
                f"unexpected ADO commit shape for "
                f"{ado_org}/{ado_project}/{ado_repo}#{pr_number}: {e}",
            )

        # If truncated, refetch the individual commit to get the full body.
        if entry.get("commentTruncated"):
            try:
                commit_url = _ado_url(
                    f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/"
                    f"repositories/{ado_repo}/commits/{sha}",
                )
                commit_body = _ado_get(commit_url, ado_access_token)
                full_comment = commit_body.get("comment")
                if full_comment:
                    message = full_comment
            except PRCommitsError:
                # Non-fatal -- fall back to the truncated version. The
                # only risk is a missed AI-generated detection.
                logger.warning(
                    "[pr_commits] failed to fetch full commit %s on ADO PR "
                    "%s/%s/%s#%s; falling back to truncated comment",
                    sha[:8], ado_org, ado_project, ado_repo, pr_number,
                )

        commits.append(PRCommit(sha=sha, message=message, author=author))

    return commits


# =============================================================================
# HTTP helpers
# =============================================================================


def _github_get(url: str, token: str):
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_PR_COMMITS_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise PRCommitsError(f"GitHub GET {url} failed: {e}")


def _ado_get(url: str, access_token: str) -> dict:
    request = Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_PR_COMMITS_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise PRCommitsError(f"ADO GET {url} failed: {e}")


def _ado_url(base: str, extra_params: Optional[dict] = None) -> str:
    params = {"api-version": _ADO_API_VERSION}
    if extra_params:
        params.update(extra_params)
    return f"{base}?{urlencode(params)}"
