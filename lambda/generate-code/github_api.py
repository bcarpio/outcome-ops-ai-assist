"""
GitHub API client.

Handles all GitHub API operations: branch creation, file commits, PR creation, etc.
"""

import base64
import json
import logging
import os
from typing import Dict, Any, Optional

import boto3
import requests
from botocore.exceptions import ClientError

logger = logging.getLogger()

# AWS clients
ssm_client = boto3.client("ssm")

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# GitHub API base URL
# GHES-aware (shared.github_base): api.github.com on SaaS, {base}/api/v3
# on GHES. Import-time resolution is fine -- env is static per deploy.
from shared.github_base import github_api_base as _github_api_base

GITHUB_API_BASE = _github_api_base()


# ============================================================================
# Authentication
# ============================================================================


def get_webhook_secret() -> str:
    """
    Fetch GitHub webhook secret from SSM Parameter Store.

    Returns:
        str: GitHub webhook secret for signature validation

    Raises:
        Exception: If secret not found in SSM
    """
    param_name = f"/{ENV}/{APP_NAME}/github/webhook-secret"

    try:
        response = ssm_client.get_parameter(
            Name=param_name,
            WithDecryption=True
        )

        secret = response.get("Parameter", {}).get("Value")
        if not secret:
            raise Exception(f"Webhook secret not found in parameter: {param_name}")

        logger.info(f"Retrieved webhook secret from SSM: {param_name}")
        return secret

    except ClientError as e:
        logger.error(f"Failed to retrieve webhook secret: {e}")
        raise


def get_github_token() -> str:
    """
    Fetch GitHub token from SSM Parameter Store.

    Returns:
        str: GitHub personal access token

    Raises:
        Exception: If token not found in SSM
    """
    param_name = f"/{ENV}/{APP_NAME}/github/token"

    try:
        response = ssm_client.get_parameter(
            Name=param_name,
            WithDecryption=True
        )

        token = response.get("Parameter", {}).get("Value")
        if not token:
            raise Exception(f"GitHub token not found in parameter: {param_name}")

        logger.info(f"Retrieved GitHub token from SSM: {param_name}")
        return token

    except ClientError as e:
        logger.error(f"Failed to retrieve GitHub token: {e}")
        raise


def get_headers(github_token: str) -> Dict[str, str]:
    """
    Get GitHub API request headers.

    Args:
        github_token: GitHub personal access token

    Returns:
        dict: Headers for GitHub API requests
    """
    return {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


# ============================================================================
# Branch Operations
# ============================================================================


def get_default_branch(
    repo_full_name: str,
    github_token: str,
) -> str:
    """Return the repo's default branch name.

    Calls ``GET /repos/{owner}/{repo}`` and reads ``default_branch``.
    Falls back to ``"main"`` on any API error so a transient GitHub
    hiccup doesn't block the whole codegen flow -- the wrong branch
    surfaces loudly on the next ``create_branch`` call, which is a
    better failure mode than an unattributed 500 out of the handler.

    Used by both the Jira-driven codegen path
    (``handler.py::process_jira_event``) and the PR-command path
    (``handle-command`` sends ``base_branch`` in the SQS message)
    to stop hardcoding "main" for repos still on ``master`` / etc.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}"
    try:
        response = requests.get(url, headers=get_headers(github_token), timeout=30)
        response.raise_for_status()
        branch = response.json().get("default_branch") or "main"
        logger.info(f"[github] Resolved default branch for {repo_full_name}: {branch}")
        return branch
    except requests.exceptions.RequestException as e:
        logger.warning(
            f"[github] Could not resolve default branch for {repo_full_name}: {e}. "
            f"Falling back to 'main'."
        )
        return "main"


def create_branch(
    repo_full_name: str,
    branch_name: str,
    base_branch: str,
    github_token: str
) -> Dict[str, Any]:
    """
    Create a branch in GitHub repository.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        branch_name: Name for the new branch
        base_branch: Base branch to create from (e.g., "main")
        github_token: GitHub personal access token

    Returns:
        dict: Result with success status and optional error

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    # Get SHA of base branch
    ref_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/git/ref/heads/{base_branch}"
    headers = get_headers(github_token)

    try:
        # Fetch base branch SHA
        ref_response = requests.get(ref_url, headers=headers, timeout=30)
        ref_response.raise_for_status()

        base_sha = ref_response.json()["object"]["sha"]
        logger.info(f"[github] Base branch {base_branch} SHA: {base_sha}")

        # Create new branch
        create_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/git/refs"

        create_response = requests.post(
            create_url,
            headers=headers,
            json={
                "ref": f"refs/heads/{branch_name}",
                "sha": base_sha
            },
            timeout=30
        )

        if create_response.status_code == 201:
            logger.info(f"[github] Branch created successfully: {branch_name}")
            return {
                "success": True,
                "branch_name": branch_name,
                "sha": base_sha
            }
        elif create_response.status_code == 422:
            # Branch already exists
            error_data = create_response.json()
            if "already exists" in error_data.get("message", "").lower():
                logger.info(f"[github] Branch already exists: {branch_name}")
                return {
                    "success": True,
                    "branch_name": branch_name,
                    "sha": base_sha,
                    "already_exists": True
                }

        # Other error
        create_response.raise_for_status()

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to create branch: {e}")
        raise Exception(f"GitHub API error: {str(e)}")

    return {"success": False, "error": "Unknown error"}


def get_branch_head_sha(
    repo_full_name: str,
    branch_name: str,
    github_token: str
) -> Optional[str]:
    """
    Retrieve the head commit SHA for a branch.

    Args:
        repo_full_name: Repository name (owner/repo)
        branch_name: Branch to inspect
        github_token: GitHub personal access token

    Returns:
        str: Commit SHA if branch exists
    """
    ref_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/git/ref/heads/{branch_name}"
    headers = get_headers(github_token)

    try:
        response = requests.get(ref_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json().get("object", {}).get("sha")
    except requests.exceptions.RequestException as exc:
        logger.error("[github] Failed to fetch branch head SHA: %s", exc)
        return None


# ============================================================================
# File Operations
# ============================================================================


def get_file(
    repo_full_name: str,
    file_path: str,
    branch_name: str,
    github_token: str
) -> Optional[Dict[str, Any]]:
    """
    Get file content from GitHub.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        file_path: Path to file in repo
        branch_name: Branch name
        github_token: GitHub personal access token

    Returns:
        dict: File data with 'content' and 'sha', or None if not found
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{file_path}"
    headers = get_headers(github_token)

    try:
        response = requests.get(
            url,
            headers=headers,
            params={"ref": branch_name},
            timeout=30
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to get file {file_path}: {e}")
        return None


def commit_file(
    repo_full_name: str,
    file_path: str,
    content: str,
    branch_name: str,
    commit_message: str,
    github_token: str
) -> Dict[str, Any]:
    """
    Create a new file in GitHub repository.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        file_path: Path for the new file
        content: File content (plain text)
        branch_name: Branch to commit to
        commit_message: Commit message
        github_token: GitHub personal access token

    Returns:
        dict: Commit result

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{file_path}"
    headers = get_headers(github_token)

    # Encode content to base64
    content_base64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    try:
        response = requests.put(
            url,
            headers=headers,
            json={
                "message": commit_message,
                "content": content_base64,
                "branch": branch_name
            },
            timeout=30
        )

        response.raise_for_status()

        logger.info(f"[github] File created: {file_path}")
        return {"success": True, "file_path": file_path}

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to commit file {file_path}: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def update_file(
    repo_full_name: str,
    file_path: str,
    content: str,
    branch_name: str,
    commit_message: str,
    github_token: str
) -> Dict[str, Any]:
    """
    Update an existing file in GitHub repository.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        file_path: Path to the file
        content: New file content (plain text)
        branch_name: Branch to commit to
        commit_message: Commit message
        github_token: GitHub personal access token

    Returns:
        dict: Update result

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    # First, get the current file to obtain its SHA
    existing_file = get_file(repo_full_name, file_path, branch_name, github_token)

    if not existing_file:
        raise Exception(f"File not found: {file_path}")

    file_sha = existing_file.get("sha")

    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{file_path}"
    headers = get_headers(github_token)

    # Encode content to base64
    content_base64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    try:
        response = requests.put(
            url,
            headers=headers,
            json={
                "message": commit_message,
                "content": content_base64,
                "branch": branch_name,
                "sha": file_sha
            },
            timeout=30
        )

        response.raise_for_status()

        logger.info(f"[github] File updated: {file_path}")
        return {"success": True, "file_path": file_path}

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to update file {file_path}: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


# ============================================================================
# Pull Request Operations
# ============================================================================


def create_pull_request(
    repo_full_name: str,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
    github_token: str
) -> Dict[str, Any]:
    """
    Create a pull request.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        title: PR title
        body: PR description
        head_branch: Source branch
        base_branch: Target branch (e.g., "main")
        github_token: GitHub personal access token

    Returns:
        dict: PR data including 'html_url' and 'number'

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls"
    headers = get_headers(github_token)

    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch
            },
            timeout=30
        )

        response.raise_for_status()

        pr_data = response.json()
        logger.info(f"[github] PR created: {pr_data.get('html_url')}")

        return {
            "success": True,
            "html_url": pr_data.get("html_url"),
            "number": pr_data.get("number")
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to create PR: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def post_pr_comment(
    repo_full_name: str,
    pr_number: int,
    comment_body: str,
    github_token: str
) -> Dict[str, Any]:
    """
    Post a comment on a pull request.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        pr_number: PR number
        comment_body: Comment text
        github_token: GitHub personal access token

    Returns:
        dict: Comment result

    Raises:
        requests.exceptions.RequestException: On API errors
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"
    headers = get_headers(github_token)

    try:
        response = requests.post(
            url,
            headers=headers,
            json={"body": comment_body},
            timeout=30
        )

        response.raise_for_status()

        logger.info(f"[github] Comment posted on PR #{pr_number}")
        return {"success": True}

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to post PR comment: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def get_pr_comments(
    repo_full_name: str,
    pr_number: int,
    github_token: str
) -> list:
    """
    Get all comments on a pull request.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        pr_number: PR number
        github_token: GitHub personal access token

    Returns:
        list: List of comment dicts with 'body', 'user', 'created_at', etc.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"
    headers = get_headers(github_token)

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        comments = response.json()
        logger.info(f"[github] Fetched {len(comments)} comments from PR #{pr_number}")
        return comments

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to get PR comments: {e}")
        return []


def get_pr_changed_files(
    repo_full_name: str,
    pr_number: int,
    github_token: str
) -> list:
    """
    Get list of files changed in a PR.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        pr_number: PR number
        github_token: GitHub personal access token

    Returns:
        list: List of file paths changed in the PR
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/files"
    headers = get_headers(github_token)

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        files = response.json()
        file_paths = [f.get("filename") for f in files if f.get("filename")]

        logger.info(f"[github] PR #{pr_number} has {len(file_paths)} changed files")
        return file_paths

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to get PR files: {e}")
        return []


def get_pr_diff(
    repo_full_name: str,
    pr_number: int,
    github_token: str
) -> str:
    """
    Get the full diff for a PR.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        pr_number: PR number
        github_token: GitHub personal access token

    Returns:
        str: Full diff text for the PR
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=60)
        response.raise_for_status()

        logger.info(f"[github] Fetched diff for PR #{pr_number}")
        return response.text

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to get PR diff: {e}")
        return ""


def get_pr_info(
    repo_full_name: str,
    pr_number: int,
    github_token: str
) -> Optional[Dict[str, Any]]:
    """
    Get PR information including branch name.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        pr_number: PR number
        github_token: GitHub personal access token

    Returns:
        dict: PR info with head branch, base branch, etc.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}"
    headers = get_headers(github_token)

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        pr_data = response.json()
        return {
            "head_branch": pr_data.get("head", {}).get("ref"),
            "base_branch": pr_data.get("base", {}).get("ref"),
            "title": pr_data.get("title"),
            "html_url": pr_data.get("html_url")
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"[github] Failed to get PR info: {e}")
        return None
