# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Utility functions for code generation.

Helper functions for cost calculation, text formatting, etc.
"""

import hashlib
import hmac
import logging
import os
import re
from typing import Optional

from models import TokenUsage

logger = logging.getLogger()


# ============================================================================
# Cost Calculation
# ============================================================================

# Pricing (USD per million tokens, configurable via env vars)
SONNET_PRICE_PER_MILLION_INPUT_TOKENS = float(os.environ.get("BEDROCK_ADVANCED_INPUT_PRICE", "3.0"))
SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS = float(os.environ.get("BEDROCK_ADVANCED_OUTPUT_PRICE", "15.0"))
HAIKU_PRICE_PER_MILLION_INPUT_TOKENS = float(os.environ.get("BEDROCK_BASIC_INPUT_PRICE", "0.80"))
HAIKU_PRICE_PER_MILLION_OUTPUT_TOKENS = float(os.environ.get("BEDROCK_BASIC_OUTPUT_PRICE", "4.0"))

# Legacy alias
PRICE_PER_MILLION_INPUT_TOKENS = SONNET_PRICE_PER_MILLION_INPUT_TOKENS
PRICE_PER_MILLION_OUTPUT_TOKENS = SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS


def calculate_cost(input_tokens: int, output_tokens: int, model: str = "sonnet") -> TokenUsage:
    """
    Calculate USD cost from token usage.

    Pricing (as of 2025):
    - Claude Sonnet 4.5: Input $3/1M, Output $15/1M
    - Claude Haiku 3.5: Input $0.80/1M, Output $4/1M

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        model: Model type - "sonnet" or "haiku" (default: "sonnet")

    Returns:
        TokenUsage: Token counts and total cost in USD
    """
    if model == "haiku":
        input_price = HAIKU_PRICE_PER_MILLION_INPUT_TOKENS
        output_price = HAIKU_PRICE_PER_MILLION_OUTPUT_TOKENS
    else:
        input_price = SONNET_PRICE_PER_MILLION_INPUT_TOKENS
        output_price = SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS

    input_cost = (input_tokens / 1_000_000) * input_price
    output_cost = (output_tokens / 1_000_000) * output_price
    total_cost = input_cost + output_cost

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=round(total_cost, 6)
    )


# ============================================================================
# Text Formatting
# ============================================================================


def to_kebab_case(text: str) -> str:
    """
    Convert text to kebab-case for branch names.

    Args:
        text: Text to convert (e.g., "Add User Authentication")

    Returns:
        str: Kebab-case version (e.g., "add-user-authentication")
    """
    # Convert to lowercase and replace non-alphanumeric with hyphens
    kebab = re.sub(r'[^a-z0-9]+', '-', text.lower())
    # Remove leading/trailing hyphens
    kebab = kebab.strip('-')
    # Limit length
    return kebab[:50]


def generate_branch_name(issue_number: int, title: str) -> str:
    """
    Generate branch name following git standards.

    Format: {issue_number}-{kebab-case-title}
    Example: 123-add-user-authentication

    Args:
        issue_number: GitHub issue number
        title: Issue title

    Returns:
        str: Branch name
    """
    kebab_title = to_kebab_case(title)
    return f"{issue_number}-{kebab_title}"


# ============================================================================
# Webhook Signature Verification
# ============================================================================


def verify_webhook_signature(payload: str, signature_header: str, secret: str) -> bool:
    """
    Verify GitHub webhook signature.

    Args:
        payload: Raw request body
        signature_header: X-Hub-Signature-256 header value
        secret: Webhook secret

    Returns:
        bool: True if signature is valid
    """
    if not signature_header:
        logger.warning("No signature header provided")
        return False

    # GitHub signature format: sha256=<hex_digest>
    if not signature_header.startswith("sha256="):
        logger.warning(f"Invalid signature format: {signature_header}")
        return False

    expected_signature = signature_header.split("=")[1]

    # Compute HMAC-SHA256
    computed_signature = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(computed_signature, expected_signature)

    if not is_valid:
        logger.warning("Webhook signature validation failed")

    return is_valid


# ============================================================================
# URL Extraction
# ============================================================================


def extract_pr_number_from_url(pr_url: str) -> Optional[int]:
    """
    Extract PR number from GitHub PR URL.

    Args:
        pr_url: GitHub PR URL (e.g., "https://github.com/owner/repo/pull/123")

    Returns:
        int: PR number, or None if not found
    """
    match = re.search(r'/pull/(\d+)', pr_url)
    if match:
        return int(match.group(1))
    return None


def resolve_owning_workspace(repo_full_name: str) -> str:
    """Resolve the repo's owning workspace for Bedrock audit attribution.

    Returns the first workspace_id that owns ``repo_full_name`` (sorted for
    determinism when a repo belongs to more than one workspace), or "" when
    none is found or resolution fails. Threaded into the generate-code audit
    row so code-gen spend attributes to the workspace in Analytics instead of
    landing under WS#SYSTEM.

    Fail-soft by design: attribution must never break code generation, so any
    error (missing WORKSPACES_TABLE env/IAM, scan failure) logs a warning and
    returns "" -- the audit row then stays under WS#SYSTEM, today's behaviour.
    """
    if not repo_full_name:
        return ""
    try:
        from shared.pr_check_scope import find_workspaces_for_repo
        return (sorted(find_workspaces_for_repo(repo_full_name)) or [""])[0]
    except Exception as exc:  # fail-soft: never block codegen on attribution
        logger.warning(
            "[attribution] workspace resolution failed for %s: %s",
            repo_full_name, exc,
        )
        return ""
