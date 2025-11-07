"""
Lambda handler for hello world test.

This is a simple test Lambda to validate the PR analysis workflow.
"""

import json
import logging
import math
from typing import List

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
bedrock_client = boto3.client("bedrock-runtime")


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding for text using Bedrock Titan Embeddings v2.

    Args:
        text: Text to embed

    Returns:
        1024-dimensional embedding vector
    """
    try:
        payload = {
            "inputText": text,
            "dimensions": 1024,
            "normalize": True,
        }

        response = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload),
        )

        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding", [])

        if not embedding:
            logger.warning(f"Empty embedding returned for query: {text[:100]}")
            return []

        logger.info(f"Generated query embedding with {len(embedding)} dimensions")
        return embedding

    except Exception as e:
        logger.error(f"Failed to generate embedding: {e}")
        raise


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity score (0-1, higher is more similar)
    """
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0

    # Dot product
    dot_product = sum(a * b for a, b in zip(vec1, vec2))

    # Magnitudes
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


def handler(event, context):
    """
    Simple hello world Lambda handler.

    Args:
        event: Lambda event payload
        context: Lambda context object

    Returns:
        dict: API Gateway response with hello world message
    """
    logger.info(f"Hello world handler invoked: {json.dumps(event)}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hello, World!",
            "requestId": context.request_id
        })
    }
