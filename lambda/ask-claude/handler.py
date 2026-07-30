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
from shared.model_client import invoke as invoke_model
from shared.model_client_types import Response as _UnifiedResponse

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

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


def invoke_claude_with_retry(prompt: str, model: str = "haiku", max_retries: int = 3) -> _UnifiedResponse:
    """
    Invoke the configured model via shared.model_client with retry logic.

    Despite the legacy name, this routes through shared.model_client.invoke
    -- Converse for anthropic/meta/nova/gpt-oss, Bedrock-Mantle Responses
    for openai.gpt-5*/codex per ADR-035. The Lambda rename is tracked in
    docs/backlog.md.

    Args:
        prompt: The prompt to send to the model
        model: Tier -- "haiku" (basic) or "sonnet" (advanced)
        max_retries: Maximum number of retry attempts

    Returns:
        Unified Response from the shared model client.

    Raises:
        ClientError: If the Converse backend retries are exhausted.
        Other shared.model_client errors propagate.
    """
    # ADR-016 v1.2 -- chat workload (ask-claude is chat-adjacent Q&A).
    tier = "advanced" if model == "sonnet" else "basic"
    max_tokens = 4000 if model == "sonnet" else 2000  # More tokens for complex reasoning

    logger.info(f"[ask-claude] Using workload=chat tier={tier}")

    for attempt in range(max_retries):
        try:
            resp = invoke_model(
                workload="chat",
                tier=tier,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                temperature=0.3,  # Low temperature for factual, grounded responses
                max_tokens=max_tokens,
            )
            logger.info(f"[ask-claude] Successfully invoked on attempt {attempt + 1}")
            return resp

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
        "invoke",
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
            "sources": ["ADR: api-gateway-standards", "README.md - acme-journey"],
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

        # Extract answer from unified Response
        if not response.text:
            logger.error(
                f"[ask-claude] No text in model response "
                f"(stop_reason={response.stop_reason})"
            )
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Failed to generate answer"}),
            }

        answer = response.text

        # Extract sources from context
        sources = extract_sources_from_context(context_docs)

        # Calculate token usage and cost based on model
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
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
