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
import time
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
bedrock_client = boto3.client("bedrock-runtime")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Claude model ID (using cross-region inference profile for better availability)
CLAUDE_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def build_prompt(query: str, context: List[Dict[str, Any]]) -> str:
    """
    Build a RAG prompt with context and query.

    Args:
        query: User's natural language question
        context: List of relevant document chunks with scores and sources

    Returns:
        Formatted prompt for Claude
    """
    # Build context section
    context_text = ""
    for i, doc in enumerate(context, 1):
        source = doc.get("source", "Unknown")
        text = doc.get("text", "")
        score = doc.get("score", 0.0)

        context_text += f"\n[Document {i}] (Relevance: {score:.2f}) - Source: {source}\n"
        context_text += f"{text}\n"

    # Build full prompt
    prompt = f"""You are a helpful assistant that answers questions about software development patterns and architectural decisions based ONLY on the provided context.

CONTEXT:
{context_text}

INSTRUCTIONS:
1. Answer the question using ONLY information from the provided context
2. Cite the specific sources you use (e.g., "According to ADR-001...")
3. If the context doesn't contain enough information, say so clearly
4. Be concise but thorough
5. Do not make assumptions or add information not in the context

QUESTION: {query}

ANSWER:"""

    return prompt


def invoke_claude_with_retry(prompt: str, max_retries: int = 3) -> Dict[str, Any]:
    """
    Invoke Claude 3.5 Sonnet via Bedrock Converse API with retry logic.

    Args:
        prompt: The prompt to send to Claude
        max_retries: Maximum number of retry attempts

    Returns:
        Response from Claude

    Raises:
        ClientError: If all retries fail
    """
    for attempt in range(max_retries):
        try:
            response = bedrock_client.converse(
                modelId=CLAUDE_MODEL_ID,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}]
                    }
                ],
                inferenceConfig={
                    "temperature": 0.3,  # Low temperature for factual, grounded responses
                    "maxTokens": 2000,
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
        ]
    }

    Returns:
    {
        "statusCode": 200,
        "body": {
            "answer": "API routes should be defined...",
            "sources": ["ADR: api-gateway-standards", "README.md - hpe-journey"]
        }
    }
    """
    logger.info(f"[ask-claude] Handler invoked: {json.dumps(event)}")

    try:
        # Extract query and context from event
        query = event.get("query")
        context_docs = event.get("context", [])

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

        logger.info(f"[ask-claude] Generating answer for query: '{query}' with {len(context_docs)} context documents")

        # Build prompt
        prompt = build_prompt(query, context_docs)

        # Invoke Claude
        response = invoke_claude_with_retry(prompt)

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

        # Log token usage
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        logger.info(f"[ask-claude] Token usage - Input: {input_tokens}, Output: {output_tokens}")

        logger.info(f"[ask-claude] Successfully generated answer ({len(answer)} chars) with {len(sources)} sources")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "answer": answer,
                "sources": sources
            }),
        }

    except Exception as e:
        logger.error(f"[ask-claude] Unexpected error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }
