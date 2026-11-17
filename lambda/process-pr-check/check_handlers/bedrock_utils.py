# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Bedrock utility functions with usage tracking.

Provides wrapper functions for Bedrock API calls that track token usage
and calculate costs for inclusion in PR comments.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from shared.audit import log_audit_entry
from shared.model_client import invoke as invoke_model

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Model IDs (configurable via environment variables)
SONNET_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
HAIKU_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

# Pricing per 1M tokens (USD). Sourced from BEDROCK_*_INPUT_PRICE /
# OUTPUT_PRICE env vars wired in by Terraform from <env>.tfvars, with
# fallback constants for local dev / pre-deploy tests.
def _price(env_name: str, fallback: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return fallback
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "[bedrock_utils] %s=%r is not a number; falling back to %.4f",
            env_name, raw, fallback,
        )
        return fallback


MODEL_PRICING = {
    SONNET_MODEL_ID: {
        "input":  _price("BEDROCK_ADVANCED_INPUT_PRICE",  3.00),
        "output": _price("BEDROCK_ADVANCED_OUTPUT_PRICE", 15.00),
        "display_name": "Advanced Model"
    },
    HAIKU_MODEL_ID: {
        "input":  _price("BEDROCK_BASIC_INPUT_PRICE",  0.80),
        "output": _price("BEDROCK_BASIC_OUTPUT_PRICE", 4.00),
        "display_name": "Basic Model"
    }
}


@dataclass
class BedrockUsage:
    """Tracks Bedrock API usage for cost calculation."""
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost(self) -> float:
        """Calculate cost in USD."""
        if self.model_id not in MODEL_PRICING:
            return 0.0

        pricing = MODEL_PRICING[self.model_id]
        input_cost = (self.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    @property
    def model_name(self) -> str:
        """Get human-readable model name."""
        if self.model_id in MODEL_PRICING:
            return MODEL_PRICING[self.model_id]["display_name"]
        return self.model_id

    def add(self, other: "BedrockUsage") -> None:
        """Add usage from another BedrockUsage instance."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        # Keep the model_id from the first non-empty one
        if not self.model_id and other.model_id:
            self.model_id = other.model_id


@dataclass
class UsageTracker:
    """Aggregates usage across multiple Bedrock calls."""
    usages: List[BedrockUsage] = field(default_factory=list)

    def add(self, usage: BedrockUsage) -> None:
        """Add a usage record."""
        self.usages.append(usage)

    @property
    def total_input_tokens(self) -> int:
        return sum(u.input_tokens for u in self.usages)

    @property
    def total_output_tokens(self) -> int:
        return sum(u.output_tokens for u in self.usages)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost(self) -> float:
        return sum(u.cost for u in self.usages)

    @property
    def primary_model(self) -> str:
        """Get the most-used model name."""
        if not self.usages:
            return "Unknown"
        # Return the model with the most tokens used
        model_tokens: Dict[str, int] = {}
        for u in self.usages:
            model_tokens[u.model_id] = model_tokens.get(u.model_id, 0) + u.total_tokens
        if not model_tokens:
            return "Unknown"
        primary_id = max(model_tokens, key=model_tokens.get)
        return MODEL_PRICING.get(primary_id, {}).get("display_name", primary_id)

    def format_usage_block(self) -> str:
        """Format usage as a markdown block for PR comments."""
        if not self.usages:
            return ""

        return (
            f"\n**Usage:**\n"
            f"Model: {self.primary_model}\n"
            f"Tokens: {self.total_input_tokens:,} input + {self.total_output_tokens:,} output = {self.total_tokens:,} total\n"
            f"Cost: ${self.total_cost:.4f}"
        )


def call_bedrock(
    prompt: str,
    model_id: str = SONNET_MODEL_ID,
    max_tokens: int = 1000,
    temperature: float = 0.1,
    workspace_id: str = ""
) -> tuple[str, BedrockUsage]:
    """
    Call Bedrock with usage tracking.

    Args:
        prompt: The prompt to send to Claude
        model_id: The model to use (default: Sonnet)
        max_tokens: Maximum tokens in response
        temperature: Temperature for response generation
        workspace_id: Owning workspace for spend attribution in the audit
            row. Default "" keeps backward-compatible callers landing under
            WS#SYSTEM (analytics-skipped); PR-check callers thread the PR
            repo's owning workspace so spend attributes correctly.

    Returns:
        Tuple of (response_text, BedrockUsage)

    Raises:
        Exception: If Bedrock call fails
    """
    # ADR-016 v1.2 -- pr-analyzer workload. Tier derived from the
    # legacy model_id string so existing callers (call_bedrock(prompt)
    # / call_bedrock(prompt, HAIKU_MODEL_ID)) don't need to change.
    tier = "basic" if "haiku" in model_id.lower() else "advanced"
    logger.info(f"Calling Bedrock workload=pr-analyzer tier={tier}...")

    bedrock_start = time.time()
    resp = invoke_model(
        workload="pr-analyzer",
        tier=tier,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

    if not resp.text:
        raise Exception(
            f"No text in {model_id} response (stop_reason={resp.stop_reason})"
        )

    response_text = resp.text.strip()

    usage = BedrockUsage(
        model_id=model_id,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )

    logger.info(
        f"Bedrock call complete: {usage.input_tokens} input + "
        f"{usage.output_tokens} output tokens (${usage.cost:.4f})"
    )

    log_audit_entry(
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        lambda_name="process-pr-check",
        model_id=model_id,
        input_text=prompt,
        output_text=response_text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost,
        duration_ms=bedrock_duration_ms,
        workspace_id=workspace_id,
    )

    return response_text, usage
