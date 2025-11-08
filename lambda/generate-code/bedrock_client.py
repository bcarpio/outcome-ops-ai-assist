"""
Bedrock client for Claude API invocations.

Handles communication with Claude via AWS Bedrock.
"""

import json
import logging
import time
from typing import Dict, Any

import boto3
from botocore.exceptions import ClientError

from models import ClaudeResponse, BedrockUsage

logger = logging.getLogger()

# Initialize Bedrock client (once per container)
bedrock_client = boto3.client("bedrock-runtime")

# Claude model configuration
CLAUDE_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"  # Cross-region inference profile
MAX_RETRIES = 3


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
            response = bedrock_client.converse(**request_params)

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

            return ClaudeResponse(
                text=response_text,
                usage=bedrock_usage,
                stop_reason=stop_reason
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code == "ThrottlingException":
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
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

    Handles responses with markdown code fences and extra text.

    Args:
        response_text: Raw response from Claude

    Returns:
        dict: Parsed JSON object

    Raises:
        ValueError: If no valid JSON found
    """
    # Try to find JSON in markdown code fence first
    import re

    markdown_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if markdown_match:
        json_text = markdown_match.group(1).strip()
    else:
        json_text = response_text

    # Extract JSON object (handles extra text before/after)
    json_match = re.search(r'\{[\s\S]*\}', json_text)
    if not json_match:
        raise ValueError("No JSON found in Claude response")

    try:
        return json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        logger.error(f"JSON text: {json_match.group(0)[:500]}")
        raise ValueError(f"Invalid JSON in response: {e}")
