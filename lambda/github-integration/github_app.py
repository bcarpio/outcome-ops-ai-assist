"""
GitHub App authentication utilities.

GitHub Apps use a different auth model than OAuth 2.0:
1. Generate JWT signed with App's private key
2. Exchange JWT for installation access token
3. Use installation token for API calls (expires in 1 hour)

No refresh tokens - regenerate installation token as needed.
"""

import json
import logging
import os
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

import boto3
import jwt
from botocore.exceptions import ClientError

logger = logging.getLogger()

# Environment
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Cached clients and config
_ssm_client = None
_github_config = None

# GitHub API endpoints
GITHUB_API_BASE = "https://api.github.com"


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def get_github_config() -> Dict[str, str]:
    """Get GitHub App configuration from SSM."""
    global _github_config
    if _github_config is not None:
        return _github_config

    ssm = _get_ssm_client()
    param_prefix = f"/{ENV}/{APP_NAME}/github"

    try:
        response = ssm.get_parameters(
            Names=[
                f"{param_prefix}/app-id",
                f"{param_prefix}/private-key",
                f"{param_prefix}/client-id",
                f"{param_prefix}/client-secret",
                f"{param_prefix}/callback-url",
                f"{param_prefix}/app-slug",
            ],
            WithDecryption=True,
        )

        params = {p["Name"].split("/")[-1]: p["Value"] for p in response["Parameters"]}

        _github_config = {
            "app_id": params.get("app-id", ""),
            "private_key": params.get("private-key", ""),
            "client_id": params.get("client-id", ""),
            "client_secret": params.get("client-secret", ""),
            "callback_url": params.get("callback-url", ""),
            "app_slug": params.get("app-slug", ""),
        }

        return _github_config

    except ClientError as e:
        logger.error(f"Failed to get GitHub config from SSM: {e}")
        raise


def generate_jwt() -> str:
    """
    Generate a JWT for GitHub App authentication.

    The JWT is valid for 10 minutes (GitHub's max is 10 min).
    Used to authenticate as the App itself, not as an installation.
    """
    config = get_github_config()
    app_id = config["app_id"]
    private_key = config["private_key"]

    if not app_id or app_id == "PLACEHOLDER_CONFIGURE_ME":
        raise ValueError("GitHub App ID not configured")
    if not private_key or private_key == "PLACEHOLDER_CONFIGURE_ME":
        raise ValueError("GitHub App private key not configured")

    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued 60s ago (clock skew tolerance)
        "exp": now + 600,  # Expires in 10 minutes
        "iss": app_id,
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")
    return token


def get_installation_token(installation_id: str) -> Tuple[str, str]:
    """
    Exchange App JWT for an installation access token.

    Returns: (access_token, expires_at)

    The installation token is valid for 1 hour.
    """
    jwt_token = generate_jwt()

    url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"

    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data["token"], data["expires_at"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get installation token: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to get installation token: {e.code}")


def get_app_installations() -> List[Dict]:
    """
    List all installations of the GitHub App.

    Used to show which orgs/users have installed the App.
    """
    jwt_token = generate_jwt()

    url = f"{GITHUB_API_BASE}/app/installations"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to list installations: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to list installations: {e.code}")


def get_installation_repos(installation_id: str) -> List[Dict]:
    """
    List repositories accessible to an installation.

    Returns a list of repository objects.
    """
    token, _ = get_installation_token(installation_id)

    repos = []
    page = 1
    per_page = 100

    while True:
        url = f"{GITHUB_API_BASE}/installation/repositories?per_page={per_page}&page={page}"

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                repos.extend(data.get("repositories", []))

                # Check if there are more pages
                if len(data.get("repositories", [])) < per_page:
                    break
                page += 1
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            logger.error(f"Failed to list repos: {e.code} - {error_body}")
            raise GitHubAppError(f"Failed to list repos: {e.code}")

    return repos


def get_repo_contents(
    installation_id: str,
    owner: str,
    repo: str,
    path: str = "",
    ref: Optional[str] = None,
) -> List[Dict]:
    """
    Get contents of a repository directory.

    Returns a list of content items (files and directories).
    """
    token, _ = get_installation_token(installation_id)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    if ref:
        url += f"?ref={ref}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            # API returns a list for directories, single object for files
            if isinstance(data, list):
                return data
            return [data]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get contents: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to get repo contents: {e.code}")


def get_file_content(
    installation_id: str,
    owner: str,
    repo: str,
    path: str,
    ref: Optional[str] = None,
) -> Optional[str]:
    """
    Get the content of a file from a repository.

    Returns the decoded file content as a string, or None if not found.
    """
    token, _ = get_installation_token(installation_id)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    if ref:
        url += f"?ref={ref}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            if data.get("type") != "file":
                return None
            # Content is base64 encoded
            import base64

            content = data.get("content", "")
            return base64.b64decode(content).decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get file: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to get file content: {e.code}")


def get_repo_default_branch(
    installation_id: str,
    owner: str,
    repo: str,
) -> str:
    """Get the default branch of a repository."""
    token, _ = get_installation_token(installation_id)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data.get("default_branch", "main")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get repo info: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to get repo info: {e.code}")


def list_repo_branches(
    installation_id: str,
    owner: str,
    repo: str,
    max_pages: int = 5,
) -> list:
    """List all branch names for a repository.

    Uses ``GET /repos/{owner}/{repo}/branches`` (paginated, 100/page).
    Returns a list of ``{"name": str, "protected": bool}`` dicts.

    ``max_pages`` caps quota burn on repos with thousands of branches
    (5 pages * 100 = 500 branches — enough for every real-world repo;
    the edit-branch modal doesn't need the extreme tail).
    """
    token, _ = get_installation_token(installation_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    branches = []
    for page in range(1, max_pages + 1):
        url = (
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches"
            f"?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                page_data = json.loads(response.read().decode())
                if not page_data:
                    break
                for b in page_data:
                    name = b.get("name")
                    if name:
                        branches.append({
                            "name": name,
                            "protected": bool(b.get("protected", False)),
                        })
                if len(page_data) < 100:
                    break
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            logger.error(f"Failed to list branches: {e.code} - {error_body}")
            raise GitHubAppError(f"Failed to list branches: {e.code}")

    return branches


def get_latest_commit_sha(
    installation_id: str,
    owner: str,
    repo: str,
    branch: Optional[str] = None,
) -> str:
    """Get the SHA of the latest commit on a branch."""
    token, _ = get_installation_token(installation_id)

    if not branch:
        branch = get_repo_default_branch(installation_id, owner, repo)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{branch}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data["sha"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get commit: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to get latest commit: {e.code}")


def get_tree_recursive(
    installation_id: str,
    owner: str,
    repo: str,
    tree_sha: str,
) -> List[Dict]:
    """
    Get the entire repository tree recursively.

    More efficient than walking directories for large repos.
    """
    token, _ = get_installation_token(installation_id)

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
            return data.get("tree", [])
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get tree: {e.code} - {error_body}")
        raise GitHubAppError(f"Failed to get repo tree: {e.code}")


class GitHubAppError(Exception):
    """Custom exception for GitHub App errors."""

    pass
