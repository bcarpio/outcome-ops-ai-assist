"""
Shared audit logging module for all Bedrock invocations.

Logs full plaintext inputs and outputs to DynamoDB for enterprise audit visibility.
Includes refusal detection that triggers downstream SNS alerting via DynamoDB Streams.

Usage:
    from shared.audit import log_audit_entry

    log_audit_entry(
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        lambda_name="chat",
        model_id=ADVANCED_MODEL_ID,
        input_text=user_prompt,
        output_text=answer,
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
    )
"""

import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Lazy-initialized DynamoDB resource (one per container)
_dynamodb_resource = None


def _get_dynamodb_table(table_name: str):
    """Get or create DynamoDB Table resource."""
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource.Table(table_name)


# ============================================================================
# Model Configuration (configurable via environment variables)
# ============================================================================

ADVANCED_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
BASIC_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

# Pricing per 1M tokens (USD) — tied to model family, not exact version.
# Values are sourced from environment variables wired in by Terraform
# (BEDROCK_*_INPUT_PRICE / BEDROCK_*_OUTPUT_PRICE), which let operators
# update rates via terraform/<env>.tfvars without a code change. The
# fallback constants are used only when an env var is unset, e.g. local
# development or pre-deploy tests.


def _price(env_name: str, fallback: float) -> float:
    """Read a per-1M-token price from the environment, falling back if unset."""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return fallback
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "[audit] %s=%r is not a number; falling back to %.4f",
            env_name, raw, fallback,
        )
        return fallback


ADVANCED_PRICING = {
    "input":  _price("BEDROCK_ADVANCED_INPUT_PRICE",  3.00),
    "output": _price("BEDROCK_ADVANCED_OUTPUT_PRICE", 15.00),
}
BASIC_PRICING = {
    "input":  _price("BEDROCK_BASIC_INPUT_PRICE",  0.80),
    "output": _price("BEDROCK_BASIC_OUTPUT_PRICE", 4.00),
}
EMBEDDING_PRICING = {
    "input":  _price("BEDROCK_EMBEDDING_INPUT_PRICE",  0.02),
    "output": _price("BEDROCK_EMBEDDING_OUTPUT_PRICE", 0.0),
}

MODEL_PRICING = {
    ADVANCED_MODEL_ID: ADVANCED_PRICING,
    BASIC_MODEL_ID: BASIC_PRICING,
    EMBEDDING_MODEL_ID: EMBEDDING_PRICING,
}
# Auto-register non-prefixed variants for audit compatibility
for _id, _pricing in list(MODEL_PRICING.items()):
    if _id.startswith("us."):
        MODEL_PRICING[_id.replace("us.", "", 1)] = _pricing

# Backward compatibility for previous model versions
MODEL_PRICING["us.anthropic.claude-sonnet-4-5-20250929-v1:0"] = ADVANCED_PRICING
MODEL_PRICING["anthropic.claude-sonnet-4-5-20250929-v1:0"] = ADVANCED_PRICING
MODEL_PRICING["us.anthropic.claude-3-5-haiku-20241022-v1:0"] = BASIC_PRICING
MODEL_PRICING["anthropic.claude-3-5-haiku-20241022-v1:0"] = BASIC_PRICING


def _calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD from model ID and token counts."""
    pricing = MODEL_PRICING.get(model_id)
    if not pricing:
        return 0.0
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


# ============================================================================
# Refusal Detection
# ============================================================================

# Broad patterns that indicate Claude refused to answer.
# Combined with a length threshold to distinguish ToS refusals (short, blunt)
# from "no KB context" responses (long, with suggestions and explanations).
REFUSAL_PATTERNS = [
    r"I (?:cannot|can't|won't|will not|am unable to|'m unable to) (?:help|assist|provide|generate|create|write|do that)",
    r"I'm not able to (?:help|assist|provide|generate|create|write)",
    r"I (?:must|have to) (?:decline|refuse)",
    r"(?:This|That|I) (?:goes|would go|is) (?:against|beyond) my",
    r"I (?:don't|do not) (?:think I should|feel comfortable)",
    r"(?:violates?|against) (?:my|the) (?:guidelines|policies|ethical|principles)",
    r"I (?:apologize|'m sorry),? but I (?:cannot|can't|won't) (?:help|assist|provide|generate|create|write|do that|comply|fulfill)",
]

_compiled_refusal_patterns = [re.compile(p, re.IGNORECASE) for p in REFUSAL_PATTERNS]

# ToS refusals are short and blunt. "No context" responses are long with
# explanations, suggestions, and alternatives. If output exceeds this
# threshold, it's Claude being helpful about missing data, not a ToS refusal.
REFUSAL_MAX_LENGTH = 250


def detect_refusal(output_text: str) -> Optional[str]:
    """
    Detect if Claude's response is a ToS/safety refusal.

    Uses two signals:
    1. Pattern match: output starts with a refusal phrase
    2. Length check: ToS refusals are short (<250 chars). Longer responses
       that match patterns are "I don't have data" responses, not abuse.

    Args:
        output_text: The model's response text

    Returns:
        The matched refusal reason string, or None if not a refusal
    """
    if not output_text:
        return None

    # Long responses are not ToS refusals - Claude is being helpful
    # about missing context, not refusing due to policy
    if len(output_text) > REFUSAL_MAX_LENGTH:
        return None

    # Only check the first 500 chars - refusals appear at the start
    check_text = output_text[:500]

    for pattern in _compiled_refusal_patterns:
        match = pattern.search(check_text)
        if match:
            return match.group(0)

    return None


# ============================================================================
# Audit Logging
# ============================================================================


def log_audit_entry(
    audit_table_name: str,
    lambda_name: str,
    model_id: str,
    input_text: str,
    output_text: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    status: str = "success",
    flagged_reason: str = "",
    user_email: str = "",
    workspace_id: str = "",
    workspace_ids: Optional[List[str]] = None,
    sources: Optional[List] = None,
) -> None:
    """
    Log a Bedrock invocation to the audit table.

    Non-fatal: catches all exceptions and logs a warning. Never fails the main request.

    Args:
        audit_table_name: DynamoDB table name (from AUDIT_TABLE env var)
        lambda_name: Name of the calling Lambda (e.g., "chat", "generate-code")
        model_id: Bedrock model ID used
        input_text: Full prompt/input sent to the model
        output_text: Full response from the model
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        cost_usd: Estimated cost in USD
        duration_ms: Call duration in milliseconds
        status: "success", "error", or "refusal"
        flagged_reason: Reason for flagging (if refusal detected)
        user_email: User email (empty for pipeline/internal calls)
        workspace_id: Workspace ID (empty for system-level calls)
    """
    if not audit_table_name:
        return

    try:
        # Auto-calculate cost from model pricing if not provided
        if cost_usd == 0.0 and (input_tokens or output_tokens):
            cost_usd = _calculate_cost(model_id, input_tokens, output_tokens)

        # Run refusal detection on output before writing
        if status == "success" and output_text:
            refusal_reason = detect_refusal(output_text)
            if refusal_reason:
                status = "refusal"
                flagged_reason = refusal_reason

        # Generate unique request ID and timestamp
        request_id = f"audit_{secrets.token_hex(8)}"
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()

        # Build partition keys
        ws_key = f"WS#{workspace_id}" if workspace_id else "WS#SYSTEM"
        user_key = f"USER#{user_email}" if user_email else f"LAMBDA#{lambda_name}"
        sort_key = f"{timestamp}#{request_id}"

        # TTL: 365 days from now
        ttl = int(time.time()) + (365 * 24 * 60 * 60)

        item = {
            "PK": ws_key,
            "SK": sort_key,
            "GSI1PK": user_key,
            "GSI1SK": sort_key,
            # GSI2: category-partitioned index. Bedrock rows go to CAT#bedrock
            # so they stay out of the admin dashboard's GSI2 Query (which
            # uses CAT#admin) and also keeps the schema uniform with chunk
            # 02's audit_writer.
            "GSI2PK": "CAT#bedrock",
            "GSI2SK": sort_key,
            "category": "bedrock",
            "request_id": request_id,
            "lambda_name": lambda_name,
            "model_id": model_id,
            "input_text": input_text[:50000] if input_text else "",
            "output_text": output_text[:50000] if output_text else "",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": str(cost_usd),
            "duration_ms": duration_ms,
            "status": status,
            "flagged_reason": flagged_reason,
            "user_email": user_email,
            "workspace_id": workspace_id,
            "workspace_ids": workspace_ids or ([workspace_id] if workspace_id else []),
            "timestamp": timestamp,
            "ttl": ttl,
        }

        # Add KB sources if provided
        if sources:
            item["sources"] = sources
            item["source_count"] = len(sources)

        table = _get_dynamodb_table(audit_table_name)
        table.put_item(Item=item)

        if status == "refusal":
            logger.warning(
                f"[audit] Refusal detected in {lambda_name}: {flagged_reason}"
            )
        else:
            logger.debug(f"[audit] Logged {lambda_name} call ({model_id})")

    except Exception as e:
        # Non-fatal: never fail the main request due to audit logging
        logger.warning(f"[audit] Failed to log audit entry: {e}")
