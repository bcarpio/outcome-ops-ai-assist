"""
Lambda handler for vector search over the knowledge base.

This handler:
1. Receives a natural language query and optional topK parameter
2. Generates an embedding for the query using Bedrock Titan Embeddings v2
3. Scans DynamoDB for all document embeddings
4. Calculates cosine similarity between query and documents
5. Returns top K most similar documents with scores

This is an internal Lambda invoked by query-kb orchestrator.
"""

import json
import logging
import math
import os
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
dynamodb_client = boto3.client("dynamodb")
bedrock_client = boto3.client("bedrock-runtime")
ssm_client = boto3.client("ssm")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Configuration (loaded from SSM at container startup)
CODE_MAPS_TABLE = None


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global CODE_MAPS_TABLE

    try:
        code_maps_param = f"/{ENVIRONMENT}/{APP_NAME}/dynamodb/code-maps-table"
        CODE_MAPS_TABLE = ssm_client.get_parameter(Name=code_maps_param)["Parameter"]["Value"]

        logger.info(f"[vector-query] Configuration loaded: TABLE={CODE_MAPS_TABLE}")
    except ClientError as e:
        logger.error(f"[vector-query] Failed to load configuration from SSM: {e}")
        raise


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
            logger.warning(f"[vector-query] Empty embedding returned for query: {text[:100]}")
            return []

        logger.info(f"[vector-query] Generated query embedding with {len(embedding)} dimensions")
        return embedding

    except ClientError as e:
        logger.error(f"[vector-query] Failed to generate embedding: {e}")
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


def scan_documents() -> List[Dict[str, Any]]:
    """
    Scan DynamoDB for all documents with embeddings.

    Returns:
        List of documents with embeddings and metadata
    """
    documents = []
    scan_kwargs = {
        "TableName": CODE_MAPS_TABLE,
    }

    try:
        # Scan with pagination
        while True:
            response = dynamodb_client.scan(**scan_kwargs)

            for item in response.get("Items", []):
                # Extract fields
                doc = {
                    "pk": item.get("PK", {}).get("S", ""),
                    "sk": item.get("SK", {}).get("S", ""),
                    "type": item.get("type", {}).get("S", ""),
                    "content": item.get("content", {}).get("S", ""),
                    "repo": item.get("repo", {}).get("S", ""),
                    "file_path": item.get("file_path", {}).get("S", ""),
                }

                # Extract embedding (stored as list of numbers)
                embedding_list = item.get("embedding", {}).get("L", [])
                doc["embedding"] = [float(x.get("N", 0)) for x in embedding_list]

                # Only include documents with valid embeddings
                if doc["embedding"] and len(doc["embedding"]) == 1024:
                    documents.append(doc)

            # Check if there are more items to scan
            if "LastEvaluatedKey" not in response:
                break

            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        logger.info(f"[vector-query] Scanned {len(documents)} documents from DynamoDB")
        return documents

    except ClientError as e:
        logger.error(f"[vector-query] Failed to scan DynamoDB: {e}")
        raise


def search_documents(query_embedding: List[float], documents: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Search documents by cosine similarity to query embedding.

    Args:
        query_embedding: Query embedding vector
        documents: List of documents with embeddings
        top_k: Number of top results to return

    Returns:
        List of top K documents with similarity scores
    """
    results = []

    for doc in documents:
        similarity = cosine_similarity(query_embedding, doc["embedding"])

        # Create result with score and metadata
        result = {
            "score": round(similarity, 4),
            "text": doc["content"][:5000],  # Limit content size in response
            "source": format_source(doc),
            "type": doc["type"],
            "repo": doc["repo"],
            "file_path": doc.get("file_path", ""),
        }

        results.append(result)

    # Sort by similarity score (descending) and take top K
    results.sort(key=lambda x: x["score"], reverse=True)
    top_results = results[:top_k]

    logger.info(f"[vector-query] Found {len(results)} results, returning top {len(top_results)}")
    return top_results


def format_source(doc: Dict[str, Any]) -> str:
    """
    Format document source for display.

    Args:
        doc: Document metadata

    Returns:
        Formatted source string
    """
    doc_type = doc.get("type", "")
    repo = doc.get("repo", "")
    sk = doc.get("sk", "")

    if doc_type == "adr":
        # Extract ADR ID from SK (e.g., "adr#ADR-001" -> "ADR-001")
        adr_id = sk.split("#")[-1] if "#" in sk else sk
        return f"ADR: {adr_id}"
    elif doc_type == "readme":
        # Extract README location (e.g., "readme#root" -> "README.md")
        location = sk.split("#")[-1] if "#" in sk else sk
        if location == "root":
            return f"README.md - {repo}"
        else:
            return f"README.md ({location}) - {repo}"
    elif doc_type == "doc":
        # Extract doc name (e.g., "doc#architecture" -> "architecture.md")
        doc_name = sk.split("#")[-1] if "#" in sk else sk
        return f"{doc_name}.md - {repo}"
    elif doc_type in ["code-map", "handler-group-summary", "infrastructure-summary"]:
        return f"Code map - {repo}"
    else:
        return f"{doc_type} - {repo}"


def handler(event, context):
    """
    Lambda handler for vector search.

    Expected event format:
    {
        "query": "How should API Gateway routes be defined?",
        "topK": 5
    }

    Returns:
    {
        "statusCode": 200,
        "body": [
            {
                "score": 0.93,
                "text": "...",
                "source": "ADR: api-gateway-standards",
                "type": "adr",
                "repo": "outcome-ops-ai-assist"
            }
        ]
    }
    """
    logger.info(f"[vector-query] Handler invoked: {json.dumps(event)}")

    # Load configuration at container startup
    if CODE_MAPS_TABLE is None:
        load_config()

    try:
        # Extract query and topK from event
        query = event.get("query")
        top_k = event.get("topK", 5)

        if not query:
            logger.warning("[vector-query] Missing required field: query")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required field: query"}),
            }

        logger.info(f"[vector-query] Searching for: '{query}' (top {top_k} results)")

        # Generate query embedding
        query_embedding = generate_embedding(query)
        if not query_embedding:
            logger.error("[vector-query] Failed to generate query embedding")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Failed to generate query embedding"}),
            }

        # Scan all documents
        documents = scan_documents()
        if not documents:
            logger.warning("[vector-query] No documents found in knowledge base")
            return {
                "statusCode": 200,
                "body": json.dumps([]),
            }

        # Search and rank documents
        results = search_documents(query_embedding, documents, top_k)

        logger.info(f"[vector-query] Returning {len(results)} results")
        return {
            "statusCode": 200,
            "body": json.dumps(results),
        }

    except Exception as e:
        logger.error(f"[vector-query] Unexpected error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }
