"""
Bedrock client for Claude API invocations.

Handles communication with Claude via AWS Bedrock.
"""

import json
import logging
import os
import sys
import time
from typing import Dict, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from models import ClaudeResponse, BedrockUsage

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import log_audit_entry

logger = logging.getLogger()

# Configure client with longer timeouts for code generation
# Code generation can take 60-120 seconds, sometimes MUCH longer (10+ min) for complex steps
# Lambda timeout is 900s (15 min), set read timeout to 850s to allow maximum time
bedrock_config = Config(
    connect_timeout=60,      # 60 seconds to establish connection
    read_timeout=850,        # ~14 minutes for response (Claude can be very slow, allow near-max time)
    retries={'max_attempts': 0}  # We handle retries ourselves
)

# Initialize Bedrock client (once per container)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)

# Claude model configuration
CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)  # Cross-region inference profile (required by Bedrock)
MAX_RETRIES = 6  # Increased for Bedrock throttling
BASE_BACKOFF_SECONDS = 5  # Aggressive retry window per project guidance


# ============================================================================
# Claude Invocation
# ============================================================================


def invoke_claude(
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    system_prompt: str = None
) -> ClaudeResponse:
    """
    Invoke Claude via Bedrock Converse API.

    Args:
        prompt: User prompt to send to Claude
        temperature: Sampling temperature (0.0-1.0, default 0.3 for factual responses)
        max_tokens: Maximum tokens to generate (default 4000)
        system_prompt: Optional system prompt

    Returns:
        ClaudeResponse: Claude's response with usage stats

    Raises:
        Exception: If Claude invocation fails after retries
    """
    logger.info(f"[bedrock] Invoking Claude with {len(prompt)} chars")

    messages = [
        {
            "role": "user",
            "content": [{"text": prompt}]
        }
    ]

    inference_config = {
        "temperature": temperature,
        "maxTokens": max_tokens,
    }

    # Build request
    request_params = {
        "modelId": CLAUDE_MODEL_ID,
        "messages": messages,
        "inferenceConfig": inference_config,
    }

    if system_prompt:
        request_params["system"] = [{"text": system_prompt}]

    # Retry logic with exponential backoff
    for attempt in range(MAX_RETRIES):
        try:
            bedrock_start = time.time()
            response = bedrock_client.converse(**request_params)
            bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

            # Extract response text
            output_message = response.get("output", {}).get("message", {})
            content_blocks = output_message.get("content", [])

            if not content_blocks:
                raise Exception("No content in Claude response")

            response_text = content_blocks[0].get("text", "")

            # Extract usage stats
            usage = response.get("usage", {})
            bedrock_usage = BedrockUsage(
                inputTokens=usage.get("inputTokens", 0),
                outputTokens=usage.get("outputTokens", 0)
            )

            # Extract stop reason
            stop_reason = response.get("stopReason", "end_turn")

            logger.info(
                f"[bedrock] Claude response: {len(response_text)} chars, "
                f"{bedrock_usage.inputTokens} input tokens, "
                f"{bedrock_usage.outputTokens} output tokens, "
                f"stop_reason={stop_reason}"
            )

            log_audit_entry(
                audit_table_name=os.environ.get("AUDIT_TABLE", ""),
                lambda_name="generate-code",
                model_id=CLAUDE_MODEL_ID,
                input_text=prompt,
                output_text=response_text,
                input_tokens=bedrock_usage.inputTokens,
                output_tokens=bedrock_usage.outputTokens,
                duration_ms=bedrock_duration_ms,
            )

            return ClaudeResponse(
                text=response_text,
                usage=bedrock_usage,
                stop_reason=stop_reason
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "ThrottlingException":
                wait_time = BASE_BACKOFF_SECONDS * (2 ** attempt)  # 5s, 10s, 20s, 40s, 80s, 160s
                logger.warning(
                    f"[bedrock] Throttled by Bedrock API, retrying in {wait_time}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"[bedrock] Bedrock API error: {e}")
                raise

        except Exception as e:
            logger.error(f"[bedrock] Unexpected error invoking Claude: {e}")
            raise

    raise Exception(f"Failed to invoke Claude after {MAX_RETRIES} retries")


# ============================================================================
# JSON Extraction
# ============================================================================


def extract_json_from_response(response_text: str) -> Dict[str, Any]:
    """
    Extract JSON from Claude response.

    Handles responses with markdown code fences and nested markdown content.
    Uses bracket matching to find the complete JSON object.

    Args:
        response_text: Raw response from Claude

    Returns:
        dict: Parsed JSON object

    Raises:
        ValueError: If no valid JSON found
    """
    import re

    # Strategy: Find the start of the JSON object ({"files": or {"steps":)
    # then use bracket counting to find the matching closing brace

    # Look for expected JSON structure patterns
    json_start_patterns = [
        r'\{\s*"files"\s*:',   # Code generation response
        r'\{\s*"steps"\s*:',   # Plan generation response
    ]

    start_pos = -1
    for pattern in json_start_patterns:
        match = re.search(pattern, response_text)
        if match:
            start_pos = match.start()
            break

    # Fallback: find first { if no known pattern
    if start_pos == -1:
        first_brace = response_text.find('{')
        if first_brace == -1:
            raise ValueError("No JSON found in Claude response")
        start_pos = first_brace

    # Use bracket counting to find the matching closing brace
    json_str = _extract_balanced_json(response_text, start_pos)

    if not json_str:
        raise ValueError("No JSON found in Claude response")

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        # If JSON parsing fails due to control characters, try to sanitize
        if "control character" in str(e).lower():
            logger.warning(f"[bedrock] JSON has invalid control characters, attempting to sanitize")
            # Remove or escape control characters in string values
            # This regex finds control chars (0x00-0x1F except \t\n\r) and removes them
            sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', json_str)
            try:
                return json.loads(sanitized)
            except json.JSONDecodeError as e2:
                logger.error(f"Failed to parse JSON even after sanitization: {e2}")
                logger.error(f"JSON text: {json_str[:500]}")
                raise ValueError(f"Invalid JSON in response: {e2}")
        logger.error(f"Failed to parse JSON: {e}")
        logger.error(f"JSON text: {json_str[:500]}")
        raise ValueError(f"Invalid JSON in response: {e}")


def _extract_balanced_json(text: str, start_pos: int) -> str:
    """
    Extract a balanced JSON object starting at start_pos.

    Uses bracket counting while respecting string boundaries to handle
    nested content (like markdown with code fences inside JSON strings).

    Args:
        text: Full text to extract from
        start_pos: Position of opening brace

    Returns:
        The balanced JSON string, or empty string if not found
    """
    if start_pos >= len(text) or text[start_pos] != '{':
        return ""

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start_pos, len(text)):
        char = text[i]

        if escape_next:
            escape_next = False
            continue

        if char == '\\' and in_string:
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if not in_string:
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start_pos:i + 1]

    # If we get here, braces weren't balanced
    return ""
