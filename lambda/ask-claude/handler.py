"""
Lambda handler for generating RAG answers using Claude 3.5 Sonnet.

This handler:
1. Receives a natural language query and context chunks from vector search
2. Constructs a prompt with context and question
3. Calls Claude 3.5 Sonnet via Bedrock Converse API
4. Returns grounded answer citing sources from context
5. Uses temperature 0.3 for factual, deterministic responses

This is an internal Lambda invoked by query-kb orchestrator.
"""

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import log_audit_entry
from shared.prompts import build_rag_system_prompt

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
bedrock_client = boto3.client("bedrock-runtime")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Claude model IDs (configurable via environment variables)
# Basic: Cost-effective for simple RAG synthesis (default)
# Advanced: Advanced reasoning for complex architecture/integration questions
BASIC_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
ADVANCED_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)

# Pricing (USD per million tokens, configurable via env vars)
HAIKU_PRICE_PER_MILLION_INPUT_TOKENS = float(os.environ.get("BEDROCK_BASIC_INPUT_PRICE", "0.80"))
HAIKU_PRICE_PER_MILLION_OUTPUT_TOKENS = float(os.environ.get("BEDROCK_BASIC_OUTPUT_PRICE", "4.0"))
SONNET_PRICE_PER_MILLION_INPUT_TOKENS = float(os.environ.get("BEDROCK_ADVANCED_INPUT_PRICE", "3.0"))
SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS = float(os.environ.get("BEDROCK_ADVANCED_OUTPUT_PRICE", "15.0"))


def calculate_cost(input_tokens: int, output_tokens: int, model: str = "haiku") -> float:
    """
    Calculate USD cost for token usage.

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        model: Model type - "haiku" or "sonnet" (default: "haiku")

    Returns:
        Total cost in USD
    """
    if model == "sonnet":
        input_price = SONNET_PRICE_PER_MILLION_INPUT_TOKENS
        output_price = SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS
    else:
        input_price = HAIKU_PRICE_PER_MILLION_INPUT_TOKENS
        output_price = HAIKU_PRICE_PER_MILLION_OUTPUT_TOKENS

    input_cost = (input_tokens / 1_000_000) * input_price
    output_cost = (output_tokens / 1_000_000) * output_price
    return round(input_cost + output_cost, 6)


def _get_workspace_custom_prompt(workspace_id: str) -> str:
    """Fetch custom system prompt for a workspace, if set."""
    if not workspace_id:
        return None
    try:
        table_name = os.environ.get("WORKSPACES_TABLE")
        if not table_name:
            return None
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)
        response = table.get_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            ProjectionExpression="custom_system_prompt",
        )
        return response.get("Item", {}).get("custom_system_prompt")
    except ClientError:
        return None


def build_prompt(query: str, context: List[Dict[str, Any]], workspace_prompt: str = None) -> str:
    """
    Build a RAG prompt with context and query.

    Args:
        query: User's natural language question
        context: List of relevant document chunks with scores and sources
        workspace_prompt: Optional workspace-specific instructions

    Returns:
        Formatted prompt for Claude
    """
    # Build context section
    context_text = ""
    for i, doc in enumerate(context, 1):
        source = doc.get("source", "Unknown")
        text = doc.get("text", "")
        score = doc.get("rerank_score", doc.get("score", 0.0))

        context_text += f"\n[Document {i}] (Relevance: {score:.2f}) - Source: {source}\n"
        context_text += f"{text}\n"

    # Build full prompt using shared system prompt
    system_prompt = build_rag_system_prompt(workspace_prompt=workspace_prompt)
    prompt = f"""{system_prompt}

CONTEXT:
{context_text}

QUESTION: {query}

ANSWER:"""

    return prompt


def invoke_claude_with_retry(prompt: str, model: str = "haiku", max_retries: int = 3) -> Dict[str, Any]:
    """
    Invoke Claude via Bedrock Converse API with retry logic.

    Args:
        prompt: The prompt to send to Claude
        model: Model to use - "haiku" (default) or "sonnet" (advanced)
        max_retries: Maximum number of retry attempts

    Returns:
        Response from Claude

    Raises:
        ClientError: If all retries fail
    """
    model_id = ADVANCED_MODEL_ID if model == "sonnet" else BASIC_MODEL_ID
    max_tokens = 4000 if model == "sonnet" else 2000  # More tokens for complex reasoning

    logger.info(f"[ask-claude] Using model: {model} ({model_id})")

    for attempt in range(max_retries):
        try:
            response = bedrock_client.converse(
                modelId=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}]
                    }
                ],
                inferenceConfig={
                    "temperature": 0.3,  # Low temperature for factual, grounded responses
                    "maxTokens": max_tokens,
                },
            )

            logger.info(f"[ask-claude] Successfully invoked Claude on attempt {attempt + 1}")
            return response

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            # Retry on throttling or server errors
            if error_code in ["ThrottlingException", "ServiceUnavailableException", "InternalServerException"]:
                if attempt < max_retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    wait_time = 2 ** attempt
                    logger.warning(f"[ask-claude] {error_code} on attempt {attempt + 1}, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"[ask-claude] Max retries reached after {error_code}")
                    raise

            # Don't retry on validation or access errors
            elif error_code in ["ValidationException", "AccessDeniedException"]:
                logger.error(f"[ask-claude] Non-retryable error: {error_code}")
                raise

            # Unknown error, don't retry
            else:
                logger.error(f"[ask-claude] Unknown error: {error_code}")
                raise

    raise ClientError(
        {"Error": {"Code": "MaxRetriesExceeded", "Message": "Failed after maximum retries"}},
        "converse"
    )


def extract_sources_from_context(context: List[Dict[str, Any]]) -> List[str]:
    """
    Extract unique sources from context documents.

    Args:
        context: List of context documents

    Returns:
        List of unique source strings
    """
    sources = []
    seen = set()

    for doc in context:
        source = doc.get("source", "Unknown")
        if source not in seen:
            sources.append(source)
            seen.add(source)

    return sources


def handler(event, context):
    """
    Lambda handler for RAG answer generation.

    Expected event format:
    {
        "query": "How should API Gateway routes be defined?",
        "context": [
            {
                "score": 0.93,
                "text": "API Gateway routes are defined...",
                "source": "ADR: api-gateway-standards"
            }
        ],
        "model": "haiku"  // Optional: "haiku" (default) or "sonnet" (advanced)
    }

    Returns:
    {
        "statusCode": 200,
        "body": {
            "answer": "API routes should be defined...",
            "sources": ["ADR: api-gateway-standards", "README.md - hpe-journey"],
            "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001, "model": "haiku"}
        }
    }
    """
    try:
        # Extract query, context, model, and workspace_id from event
        query = event.get("query")
        context_docs = event.get("context", [])
        model = event.get("model", "haiku")  # Default to haiku for cost-effectiveness
        workspace_id = event.get("workspace_id")

        # Validate model
        if model not in ("haiku", "sonnet"):
            logger.warning(f"[ask-claude] Invalid model '{model}', defaulting to haiku")
            model = "haiku"

        if not query:
            logger.warning("[ask-claude] Missing required field: query")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required field: query"}),
            }

        if not context_docs:
            logger.warning("[ask-claude] No context provided")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "answer": "I don't have enough information in the knowledge base to answer this question. Please ensure relevant documentation has been ingested.",
                    "sources": []
                }),
            }

        logger.info(f"[ask-claude] Generating answer for query: '{query}' with {len(context_docs)} context documents (model: {model})")

        # Fetch workspace custom prompt if workspace_id is provided
        workspace_prompt = _get_workspace_custom_prompt(workspace_id)

        # Build prompt
        prompt = build_prompt(query, context_docs, workspace_prompt=workspace_prompt)

        # Invoke Claude with selected model
        bedrock_start = time.time()
        response = invoke_claude_with_retry(prompt, model=model)
        bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

        # Extract answer from response
        output = response.get("output", {})
        message = output.get("message", {})
        content = message.get("content", [])

        if not content or not content[0].get("text"):
            logger.error("[ask-claude] No text in Claude response")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Failed to generate answer"}),
            }

        answer = content[0]["text"]

        # Extract sources from context
        sources = extract_sources_from_context(context_docs)

        # Calculate token usage and cost based on model
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        cost_usd = calculate_cost(input_tokens, output_tokens, model=model)

        logger.info(
            f"[ask-claude] Token usage ({model}) - Input: {input_tokens}, Output: {output_tokens}, "
            f"Cost: ${cost_usd:.6f}"
        )

        logger.info(f"[ask-claude] Successfully generated answer ({len(answer)} chars) with {len(sources)} sources")

        # Audit log the Bedrock call
        ask_model_id = ADVANCED_MODEL_ID if model == "sonnet" else BASIC_MODEL_ID
        # Build structured sources for audit
        audit_sources = []
        seen_paths = set()
        for doc in context_docs:
            path = doc.get("file_path", doc.get("source", ""))
            if path and path not in seen_paths:
                seen_paths.add(path)
                audit_sources.append({
                    "repo": doc.get("repo", ""),
                    "type": doc.get("type", ""),
                    "path": path,
                })

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="ask-claude",
            model_id=ask_model_id,
            input_text=prompt,
            output_text=answer,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=bedrock_duration_ms,
            sources=audit_sources,
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "answer": answer,
                "sources": sources,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost_usd,
                    "model": model
                }
            }),
        }

    except Exception as e:
        logger.error(f"[ask-claude] Unexpected error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }
