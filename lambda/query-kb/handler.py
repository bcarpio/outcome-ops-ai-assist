"""
Lambda handler for querying the knowledge base (RAG orchestrator).

This handler orchestrates the full RAG pipeline:
1. Receives user query with optional topK parameter
2. Invokes vector-query Lambda to find relevant documents
3. If no results found, returns helpful "not found" message
4. Invokes ask-claude Lambda with query + context chunks
5. Returns natural language answer with cited sources

This is the single entry point for knowledge base queries, invoked by:
- MS Teams bot
- CLI tools
- Slack integrations
- Any user-facing interface
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
lambda_client = boto3.client("lambda")
ssm_client = boto3.client("ssm")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Configuration (loaded from SSM at container startup)
VECTOR_QUERY_LAMBDA_ARN = None
ASK_CLAUDE_LAMBDA_ARN = None


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global VECTOR_QUERY_LAMBDA_ARN, ASK_CLAUDE_LAMBDA_ARN

    try:
        vector_query_param = f"/{ENVIRONMENT}/{APP_NAME}/lambda/vector-query-arn"
        VECTOR_QUERY_LAMBDA_ARN = ssm_client.get_parameter(Name=vector_query_param)["Parameter"]["Value"]

        ask_claude_param = f"/{ENVIRONMENT}/{APP_NAME}/lambda/ask-claude-arn"
        ASK_CLAUDE_LAMBDA_ARN = ssm_client.get_parameter(Name=ask_claude_param)["Parameter"]["Value"]

        logger.info(f"[query-kb] Configuration loaded successfully")
    except ClientError as e:
        logger.error(f"[query-kb] Failed to load configuration from SSM: {e}")
        raise


def invoke_lambda(function_arn: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Invoke another Lambda function synchronously.

    Args:
        function_arn: ARN of Lambda function to invoke
        payload: Event payload to send

    Returns:
        Parsed response from Lambda, or None if error
    """
    try:
        response = lambda_client.invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",  # Synchronous
            Payload=json.dumps(payload)
        )

        # Parse response
        response_payload = json.loads(response["Payload"].read())

        # Check for Lambda errors
        if "FunctionError" in response:
            logger.error(f"[query-kb] Lambda {function_arn} returned error: {response_payload}")
            return None

        return response_payload

    except ClientError as e:
        logger.error(f"[query-kb] Failed to invoke Lambda {function_arn}: {e}")
        return None


def handler(event, context):
    """
    Lambda handler for knowledge base queries.

    Expected event format:
    {
        "query": "How should API Gateway routes be defined?",
        "topK": 5  // Optional, defaults to 5
    }

    Returns:
    {
        "statusCode": 200,
        "body": {
            "answer": "API routes should be defined using Terraform...",
            "sources": ["ADR: api-gateway-standards", "README.md - hpe-journey"]
        }
    }

    Error responses:
    {
        "statusCode": 404,
        "body": {
            "answer": "I couldn't find any relevant information...",
            "sources": []
        }
    }
    """
    logger.info(f"[query-kb] Handler invoked: {json.dumps(event)}")

    # Load configuration at container startup
    if VECTOR_QUERY_LAMBDA_ARN is None:
        load_config()

    try:
        # Extract query and topK from event
        query = event.get("query")
        top_k = event.get("topK", 5)

        if not query:
            logger.warning("[query-kb] Missing required field: query")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required field: query"}),
            }

        logger.info(f"[query-kb] Processing query: '{query}' (top {top_k} results)")

        # Step 1: Invoke vector-query Lambda to find relevant documents
        logger.info("[query-kb] Invoking vector-query Lambda...")
        vector_response = invoke_lambda(
            VECTOR_QUERY_LAMBDA_ARN,
            {
                "query": query,
                "topK": top_k
            }
        )

        if not vector_response or vector_response.get("statusCode") != 200:
            logger.error("[query-kb] vector-query Lambda failed")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Failed to search knowledge base"}),
            }

        # Parse vector search results
        context_docs = json.loads(vector_response.get("body", "[]"))

        if not context_docs:
            logger.info("[query-kb] No relevant documents found")
            return {
                "statusCode": 404,
                "body": json.dumps({
                    "answer": "I couldn't find any relevant information in the knowledge base to answer this question. This could mean:\n\n1. The topic hasn't been documented yet\n2. The relevant documentation hasn't been ingested\n3. The query uses different terminology than the documentation\n\nTry rephrasing your question or check if the relevant ADRs and documentation have been added to the knowledge base.",
                    "sources": []
                }),
            }

        logger.info(f"[query-kb] Found {len(context_docs)} relevant documents")

        # Step 2: Invoke ask-claude Lambda to generate answer
        logger.info("[query-kb] Invoking ask-claude Lambda...")
        claude_response = invoke_lambda(
            ASK_CLAUDE_LAMBDA_ARN,
            {
                "query": query,
                "context": context_docs
            }
        )

        if not claude_response or claude_response.get("statusCode") != 200:
            logger.error("[query-kb] ask-claude Lambda failed")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Failed to generate answer"}),
            }

        # Parse Claude response
        result = json.loads(claude_response.get("body", "{}"))
        answer = result.get("answer", "")
        sources = result.get("sources", [])

        logger.info(f"[query-kb] Successfully generated answer with {len(sources)} sources")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "answer": answer,
                "sources": sources
            }),
        }

    except Exception as e:
        logger.error(f"[query-kb] Unexpected error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }
