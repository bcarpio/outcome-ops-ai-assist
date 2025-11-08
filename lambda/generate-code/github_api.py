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
GITHUB_API_BASE = "https://api.github.com"


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
