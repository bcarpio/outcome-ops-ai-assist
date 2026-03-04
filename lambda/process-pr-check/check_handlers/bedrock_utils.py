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

import boto3
from botocore.config import Config

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from shared.audit import log_audit_entry

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configure retry strategy for Bedrock to handle throttling
bedrock_config = Config(
    retries={
        'max_attempts': 10,
        'mode': 'adaptive'
    }
)

# Bedrock client (initialized once per container)
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1", config=bedrock_config)

# Model IDs (configurable via environment variables)
SONNET_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
HAIKU_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)

# Pricing per 1M tokens (USD)
MODEL_PRICING = {
    SONNET_MODEL_ID: {
        "input": 3.00,
        "output": 15.00,
        "display_name": "Advanced Model"
    },
    HAIKU_MODEL_ID: {
        "input": 0.80,
        "output": 4.00,
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
    temperature: float = 0.1
) -> tuple[str, BedrockUsage]:
    """
    Call Bedrock with usage tracking.

    Args:
        prompt: The prompt to send to Claude
        model_id: The model to use (default: Sonnet)
        max_tokens: Maximum tokens in response
        temperature: Temperature for response generation

    Returns:
        Tuple of (response_text, BedrockUsage)

    Raises:
        Exception: If Bedrock call fails
    """
    logger.info(f"Calling Bedrock model {model_id}...")

    bedrock_start = time.time()
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [{"text": prompt}]
            }
        ],
        inferenceConfig={
            "maxTokens": max_tokens,
            "temperature": temperature
        }
    )
    bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

    # Extract response text
    output_message = response.get("output", {}).get("message", {})
    content = output_message.get("content", [])

    if not content or not content[0].get("text"):
        raise Exception("No content in Bedrock response")

    response_text = content[0]["text"].strip()

    # Extract usage
    usage_data = response.get("usage", {})
    usage = BedrockUsage(
        model_id=model_id,
        input_tokens=usage_data.get("inputTokens", 0),
        output_tokens=usage_data.get("outputTokens", 0)
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
    )

    return response_text, usage
