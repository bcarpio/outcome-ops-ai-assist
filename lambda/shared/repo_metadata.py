"""
Provider-agnostic repository shape retrieval.

Some check handlers (``TEST_COVERAGE``, ``README_FRESHNESS``) need to
inspect the repository's shape at its default branch — the branch name
itself plus a recursive file listing — rather than the diff of a single
PR. This module unifies the GitHub and Azure DevOps calls behind
``fetch_repo_metadata`` so those handlers stay provider-agnostic.

Kept separate from ``pr_metadata.py`` because:

- The result is repo-scoped, not PR-scoped
- The response shape (default_branch + file_paths) is different
- Handlers that need repo shape usually don't need PR shape and vice
  versa — separating the modules lets callers import only what they
  use

Caller responsibilities mirror ``pr_metadata`` and ``source_fetch``:
tokens are pre-acquired and passed in; this module does not touch SSM
or OAuth refresh.
"""

import json
import logging
from typing import List, NamedTuple, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


_REPO_METADATA_TIMEOUT_SECONDS = 60
_ADO_API_VERSION = "7.0"
_GITHUB_API = "https://api.github.com"
_ADO_API = "https://dev.azure.com"


class RepoMetadataError(Exception):
    """Wraps any repo-metadata retrieval failure."""


class RepoMetadata(NamedTuple):
    """Provider-agnostic repository shape.

    ``file_paths`` is a flat list of blob paths, no leading slash, no
    directory entries. Sorted by returned order (deterministic per-
    provider but no cross-provider ordering guarantee).
    """
    default_branch: str
    file_paths: List[str]


def fetch_repo_metadata(
    provider: str,
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
) -> RepoMetadata:
    """Fetch default branch + recursive file tree from the source provider.

    Args:
        provider: ``"github"``, ``"azure_devops"``, or ``"gitlab"``.
        repository: GitHub ``owner/repo``. Required for github.
        github_token: GitHub PAT. Required for github.
        ado_org: ADO org name. Required for azure_devops.
        ado_project: ADO project name. Required for azure_devops.
        ado_repo: ADO repo name. Required for azure_devops.
        ado_access_token: ADO OAuth access token (pre-refreshed by
            caller). Required for azure_devops.
        gitlab_project_path: GitLab full project path
            (``group/{...subgroups}/project``). Required for gitlab.
        gitlab_access_token: GitLab user-scoped OAuth access token
            (pre-refreshed by caller). Required for gitlab.

    Returns:
        RepoMetadata NamedTuple.

    Raises:
        RepoMetadataError: On any auth, network, or unexpected-shape error.
    """
    if provider == "github":
        if not (repository and github_token):
            raise RepoMetadataError(
                "github fetch_repo_metadata needs repository + github_token",
            )
        return _fetch_repo_from_github(
            repository=repository,
            github_token=github_token,
        )

    if provider == "azure_devops":
        if not (ado_org and ado_project and ado_repo and ado_access_token):
            raise RepoMetadataError(
                "azure_devops fetch_repo_metadata needs ado_org + ado_project "
                "+ ado_repo + ado_access_token",
            )
        return _fetch_repo_from_ado(
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
            ado_access_token=ado_access_token,
        )

    if provider == "gitlab":
        if not (gitlab_project_path and gitlab_access_token):
            raise RepoMetadataError(
                "gitlab fetch_repo_metadata needs gitlab_project_path "
                "+ gitlab_access_token",
            )
        return _fetch_repo_from_gitlab(
            project_path=gitlab_project_path,
            access_token=gitlab_access_token,
        )

    raise RepoMetadataError(f"Unknown provider for fetch_repo_metadata: {provider}")


def _fetch_repo_from_github(
    *,
    repository: str,
    github_token: str,
) -> RepoMetadata:
    """Fetch default branch + recursive tree from GitHub.

    Two REST calls:
      GET /repos/{repo}                          — default_branch
      GET /repos/{repo}/git/trees/{branch}?recursive=1 — tree

    GitHub's trees API returns tree + blob entries; we filter to
    ``type == "blob"`` to match the check handlers' expectation of
    "just the files."

    GitHub caps the recursive-tree response at 100,000 entries and
    sets ``truncated=true`` beyond that. Log a warning but don't error
    — a truncated view is still useful, and no OSS repo the platform
    targets should hit this ceiling.
    """
    repo_body = _github_get(
        f"{_GITHUB_API}/repos/{repository}",
        github_token,
    )
    try:
        default_branch = repo_body["default_branch"]
    except (KeyError, TypeError) as e:
        raise RepoMetadataError(
            f"unexpected GitHub repo shape for {repository}: {e}",
        )

    tree_body = _github_get(
        f"{_GITHUB_API}/repos/{repository}/git/trees/{default_branch}?recursive=1",
        github_token,
    )
    if tree_body.get("truncated"):
        logger.warning(
            "GitHub tree for %s truncated at 100k entries; "
            "TEST_COVERAGE / README_FRESHNESS may miss deep files",
            repository,
        )
    file_paths: List[str] = []
    for entry in tree_body.get("tree", []) or []:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if path:
            file_paths.append(path.lstrip("/"))

    return RepoMetadata(default_branch=default_branch, file_paths=file_paths)


def _fetch_repo_from_ado(
    *,
    ado_org: str,
    ado_project: str,
    ado_repo: str,
    ado_access_token: str,
) -> RepoMetadata:
    """Fetch default branch + recursive tree from Azure DevOps.

    Two REST calls:
      GET .../git/repositories/{repo}                        — defaultBranch
      GET .../items?recursionLevel=Full&scopePath=/&version=... — tree

    ADO returns ``defaultBranch`` as ``refs/heads/{name}``; strip the
    prefix to match GitHub's ``default_branch`` shape.

    ADO's items endpoint returns folder AND blob entries; we filter to
    ``gitObjectType == "blob"``. Paths come leading-slashed
    (``/src/foo.py``); we normalize to no-leading-slash to match GitHub.
    """
    repo_url = _ado_url(
        f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/repositories/{ado_repo}",
    )
    repo_body = _ado_get(repo_url, ado_access_token)
    raw_default = repo_body.get("defaultBranch")
    if not raw_default:
        raise RepoMetadataError(
            f"ADO repo {ado_org}/{ado_project}/{ado_repo} has no defaultBranch",
        )
    default_branch = (
        raw_default[len("refs/heads/"):]
        if raw_default.startswith("refs/heads/")
        else raw_default
    )

    items_url = _ado_url(
        f"{_ADO_API}/{ado_org}/{ado_project}/_apis/git/repositories/{ado_repo}/items",
        extra_params={
            "scopePath": "/",
            "recursionLevel": "Full",
            "versionDescriptor.version": default_branch,
            "versionDescriptor.versionType": "branch",
        },
    )
    items_body = _ado_get(items_url, ado_access_token)
    file_paths: List[str] = []
    for entry in items_body.get("value", []) or []:
        if entry.get("gitObjectType") != "blob":
            continue
        path = entry.get("path")
        if path:
            file_paths.append(path.lstrip("/"))

    return RepoMetadata(default_branch=default_branch, file_paths=file_paths)


def _fetch_repo_from_gitlab(
    *,
    project_path: str,
    access_token: str,
) -> RepoMetadata:
    """Fetch default branch + recursive tree from GitLab.

    Two GitLab API v4 calls via the shared client:
      GET /projects/{enc}                                  -- default_branch
      GET /projects/{enc}/repository/tree?recursive=true   -- tree

    ``gitlab_api.get_tree`` already filters to blob (file) entries and
    returns bare paths (no leading slash), so the result normalizes to
    the same shape as the GitHub/ADO branches. The project is addressed
    by its URL-encoded full path (nested subgroups work).
    """
    from shared import gitlab_api  # noqa: WPS433 -- defer import
    try:
        default_branch = gitlab_api.get_default_branch(access_token, project_path)
    except Exception as e:
        raise RepoMetadataError(
            f"GitLab default-branch fetch failed for {project_path}: {e}",
        )
    if not default_branch:
        raise RepoMetadataError(
            f"GitLab project {project_path} has no default_branch",
        )
    try:
        tree = gitlab_api.get_tree(access_token, project_path, default_branch)
    except Exception as e:
        raise RepoMetadataError(
            f"GitLab tree fetch failed for {project_path}: {e}",
        )
    file_paths = [p.lstrip("/") for p in tree if p]
    return RepoMetadata(default_branch=default_branch, file_paths=file_paths)


# =============================================================================
# HTTP helpers (mirror pr_metadata; kept module-local for clarity)
# =============================================================================


def _github_get(url: str, token: str) -> dict:
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_REPO_METADATA_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise RepoMetadataError(f"GitHub GET {url} failed: {e}")


def _ado_get(url: str, access_token: str) -> dict:
    request = Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "outcome-ops-process-pr-check",
    })
    try:
        with urlopen(request, timeout=_REPO_METADATA_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        raise RepoMetadataError(f"ADO GET {url} failed: {e}")


def _ado_url(base: str, extra_params: Optional[dict] = None) -> str:
    params = {"api-version": _ADO_API_VERSION}
    if extra_params:
        params.update(extra_params)
    return f"{base}?{urlencode(params)}"
