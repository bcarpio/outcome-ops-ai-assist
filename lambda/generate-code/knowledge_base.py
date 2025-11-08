"""
Knowledge base client.

Invokes the query-kb Lambda to retrieve relevant standards and examples.
"""

import json
import logging
import os
from typing import List

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()

# Initialize Lambda client (once per container)
lambda_client = boto3.client("lambda")

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


# ============================================================================
# Knowledge Base Query
# ============================================================================


def query_knowledge_base(queries: List[str], top_k: int = 3) -> List[str]:
    """
    Query knowledge base for relevant standards and examples.

    Invokes the query-kb Lambda function with multiple queries and returns
    the combined results.

    Args:
        queries: List of search queries (e.g., ["Lambda handler standards", "Testing patterns"])
        top_k: Number of top results to return per query (default 3)

    Returns:
        List[str]: List of relevant document excerpts from the knowledge base

    Raises:
        Exception: If query-kb Lambda invocation fails
    """
    if not queries:
        logger.warning("[kb] No queries provided")
        return []

    logger.info(f"[kb] Querying knowledge base with {len(queries)} queries")

    # Query-kb Lambda expects: {"query": "search text", "top_k": 5}
    # We'll invoke it once per query and aggregate results
    results = []

    query_kb_function_name = f"{ENV}-{APP_NAME}-query-kb"

    for query in queries:
        try:
            logger.info(f"[kb] Query: {query}")

            payload = {
                "query": query,
                "top_k": top_k
            }

            response = lambda_client.invoke(
                FunctionName=query_kb_function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload)
            )

            # Parse response
            response_payload = json.loads(response["Payload"].read())

            # Check for Lambda errors
            if response.get("FunctionError"):
                error_msg = response_payload.get("errorMessage", "Unknown error")
                logger.error(f"[kb] query-kb Lambda error: {error_msg}")
                continue

            # Parse body (query-kb returns API Gateway format)
            if "body" in response_payload:
                body = json.loads(response_payload["body"])
                answer = body.get("answer", "")
                if answer:
                    results.append(f"# Query: {query}\n\n{answer}")
            else:
                logger.warning(f"[kb] No body in query-kb response for query: {query}")

        except ClientError as e:
            logger.error(f"[kb] Failed to invoke query-kb Lambda: {e}")
        except Exception as e:
            logger.error(f"[kb] Unexpected error querying KB: {e}")

    logger.info(f"[kb] Retrieved {len(results)} results from knowledge base")

    return results


# ============================================================================
# Standard Query Helpers
# ============================================================================


def get_lambda_standards() -> List[str]:
    """
    Get Lambda handler standards from knowledge base.

    Returns:
        List[str]: Lambda handler standards and patterns
    """
    return query_knowledge_base([
        "Lambda handler standards and patterns",
        "Lambda error handling and logging best practices"
    ])


def get_terraform_standards() -> List[str]:
    """
    Get Terraform standards from knowledge base.

    Returns:
        List[str]: Terraform module standards and conventions
    """
    return query_knowledge_base([
        "Terraform Lambda module configuration standards",
        "Terraform resource naming conventions"
    ])


def get_testing_standards() -> List[str]:
    """
    Get testing standards from knowledge base.

    Returns:
        List[str]: Testing patterns and requirements
    """
    return query_knowledge_base([
        "Testing standards and patterns",
        "Test coverage requirements and best practices"
    ])


def get_github_standards() -> List[str]:
    """
    Get GitHub standards from knowledge base.

    Returns:
        List[str]: GitHub workflow and PR standards
    """
    return query_knowledge_base([
        "GitHub PR standards and conventions",
        "Git commit message standards"
    ])
