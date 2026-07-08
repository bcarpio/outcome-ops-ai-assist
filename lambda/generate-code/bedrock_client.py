"""
generate-code-local facade over shared.model_client.

Historically constructed its own bedrock-runtime client and called
converse directly. Now routes through shared.model_client.invoke so a
tfvars swap of BEDROCK_ADVANCED_MODEL_ID picks up Converse or Mantle
Responses-API per ADR-035 without per-Lambda code changes. The
ClaudeResponse return type and the retry semantics are preserved so
adr_fixer / tests_fixer / readme_fixer / step-execution callers don't
need to change.
"""

import json
import logging
import os
import sys
import time
from typing import Dict, Any

from botocore.exceptions import ClientError

from models import ClaudeResponse, BedrockUsage

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import log_audit_entry
from shared.model_client import invoke as invoke_model

logger = logging.getLogger()

# Model selection (configurable via env var; tfvars-managed)
CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
MAX_RETRIES = 6  # Increased for Bedrock throttling
BASE_BACKOFF_SECONDS = 5  # Aggressive retry window per project guidance


# ============================================================================
# Model invocation (Claude name retained for compatibility; the actual
# backend is whichever shared.model_client dispatches to based on the
# configured model ID).
# ============================================================================


def invoke_claude(
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    system_prompt: str = None
) -> ClaudeResponse:
    """
    Invoke the configured model via shared.model_client with retry logic.

    Despite the name, this dispatches to whichever backend matches
    CLAUDE_MODEL_ID (BEDROCK_ADVANCED_MODEL_ID env): Converse for
    anthropic/meta/nova/gpt-oss; Mantle Responses for openai.gpt-5*/codex
    per ADR-035.

    Args:
        prompt: User prompt to send to the model
        temperature: Sampling temperature (0.0-1.0, default 0.3 for
            factual responses)
        max_tokens: Maximum tokens to generate (default 4000)
        system_prompt: Optional system prompt

    Returns:
        ClaudeResponse: response with usage stats (legacy type name; the
        content is the actual model's response, not necessarily Claude's)

    Raises:
        Exception: If invocation fails after retries
    """
    logger.info(f"[bedrock] Invoking {CLAUDE_MODEL_ID} with {len(prompt)} chars")

    for attempt in range(MAX_RETRIES):
        try:
            bedrock_start = time.time()
            # ADR-016 v1.2 -- code-gen workload attribution.
            resp = invoke_model(
                workload="code-gen",
                tier="advanced",
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                system=system_prompt if system_prompt else None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

            if not resp.text:
                raise Exception(
                    f"No text in {CLAUDE_MODEL_ID} response "
                    f"(stop_reason={resp.stop_reason})"
                )

            bedrock_usage = BedrockUsage(
                inputTokens=resp.usage.input_tokens,
                outputTokens=resp.usage.output_tokens,
            )

            logger.info(
                f"[bedrock] Response: {len(resp.text)} chars, "
                f"{bedrock_usage.inputTokens} input tokens, "
                f"{bedrock_usage.outputTokens} output tokens, "
                f"stop_reason={resp.stop_reason.value}"
            )

            log_audit_entry(
                audit_table_name=os.environ.get("AUDIT_TABLE", ""),
                lambda_name="generate-code",
                model_id=CLAUDE_MODEL_ID,
                input_text=prompt,
                output_text=resp.text,
                input_tokens=bedrock_usage.inputTokens,
                output_tokens=bedrock_usage.outputTokens,
                duration_ms=bedrock_duration_ms,
            )

            return ClaudeResponse(
                text=resp.text,
                usage=bedrock_usage,
                stop_reason=resp.stop_reason.value,
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
            logger.error(f"[bedrock] Unexpected error invoking model: {e}")
            raise

    raise Exception(f"Failed to invoke {CLAUDE_MODEL_ID} after {MAX_RETRIES} retries")


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
