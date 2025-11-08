"""
Utility functions for code generation.

Helper functions for cost calculation, text formatting, etc.
"""

import hashlib
import hmac
import logging
import re
from typing import Optional

from models import TokenUsage

logger = logging.getLogger()


# ============================================================================
# Cost Calculation
# ============================================================================

# Claude Sonnet 4.5 pricing (as of 2025)
PRICE_PER_MILLION_INPUT_TOKENS = 3.0
PRICE_PER_MILLION_OUTPUT_TOKENS = 15.0


def calculate_cost(input_tokens: int, output_tokens: int) -> TokenUsage:
    """
    Calculate USD cost from token usage.

    Claude Sonnet 4.5 pricing:
    - Input: $3 per 1M tokens
    - Output: $15 per 1M tokens

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens

    Returns:
        TokenUsage: Token counts and total cost in USD
    """
    input_cost = (input_tokens / 1_000_000) * PRICE_PER_MILLION_INPUT_TOKENS
    output_cost = (output_tokens / 1_000_000) * PRICE_PER_MILLION_OUTPUT_TOKENS
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
