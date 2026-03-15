"""
Lambda handler for querying the knowledge base (RAG orchestrator).

This handler orchestrates the full RAG pipeline:
1. Receives user query with optional topK parameter
2. Generates query embedding using Bedrock Titan v2
3. Queries S3 Vectors for relevant documents (semantic search)
4. If no results found, returns helpful "not found" message
5. Invokes ask-claude Lambda with query + context chunks (unless raw=true)
6. Returns natural language answer with cited sources

Modes:
- Normal mode (raw=false): Full RAG with Claude synthesis (for human users)
- Raw mode (raw=true): Returns vector search results directly (for code generation)
  This avoids double-processing when results will be passed to Claude anyway.

This is the single entry point for knowledge base queries, invoked by:
- MS Teams bot
- CLI tools
- Slack integrations
- Code generation (with raw=true to skip synthesis)
"""

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import log_audit_entry

# Bedrock model configuration
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)
RERANK_MODEL_ID = os.environ.get("BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0")
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() == "true"
RERANK_MAX_FETCH = int(os.environ.get("RERANK_MAX_FETCH", "50"))
RERANK_MIN_SCORE = float(os.environ.get("RERANK_MIN_SCORE", "0"))

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
# Lambda client with extended timeout for ask-claude invocations (can take 5+ minutes with large context)
lambda_config = Config(
    read_timeout=600,  # 10 minutes
    connect_timeout=10,
    retries={"max_attempts": 0}  # No retries for Lambda invoke - let the caller handle failures
)
lambda_client = boto3.client("lambda", config=lambda_config)
ssm_client = boto3.client("ssm")
s3vectors_client = boto3.client("s3vectors")
bedrock_client = boto3.client("bedrock-runtime")
bedrock_agent_runtime_client = boto3.client("bedrock-agent-runtime")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Configuration (loaded from SSM at container startup)
ASK_CLAUDE_LAMBDA_ARN = None
VECTOR_BUCKET = None
VECTOR_INDEX = None


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global ASK_CLAUDE_LAMBDA_ARN, VECTOR_BUCKET, VECTOR_INDEX

    try:
        ask_claude_param = f"/{ENVIRONMENT}/{APP_NAME}/lambda/ask-claude-arn"
        ASK_CLAUDE_LAMBDA_ARN = ssm_client.get_parameter(Name=ask_claude_param)["Parameter"]["Value"]

        bucket_param = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/bucket-name"
        VECTOR_BUCKET = ssm_client.get_parameter(Name=bucket_param)["Parameter"]["Value"]

        index_param = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/index-name"
        VECTOR_INDEX = ssm_client.get_parameter(Name=index_param)["Parameter"]["Value"]

        logger.info(f"[query-kb] Configuration loaded: BUCKET={VECTOR_BUCKET}, INDEX={VECTOR_INDEX}")
    except ClientError as e:
        logger.error(f"[query-kb] Failed to load configuration from SSM: {e}")
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

        bedrock_start = time.time()
        response = bedrock_client.invoke_model(
            modelId=EMBEDDING_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload),
        )
        bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding", [])

        if not embedding:
            logger.warning(f"[query-kb] Empty embedding returned for query: {text[:100]}")
            return []

        logger.info(f"[query-kb] Generated query embedding with {len(embedding)} dimensions")

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="query-kb",
            model_id=EMBEDDING_MODEL_ID,
            input_text=text,
            output_text=f"embedding[{len(embedding)}]",
            duration_ms=bedrock_duration_ms,
        )

        return embedding

    except ClientError as e:
        logger.error(f"[query-kb] Failed to generate embedding: {e}")
        raise


def format_source(metadata: Dict[str, Any]) -> str:
    """
    Format document source for display.

    Args:
        metadata: Vector metadata containing type, repo, file_path

    Returns:
        Formatted source string (e.g., "ADR: ADR-001", "README.md - repo-name")
    """
    doc_type = metadata.get("type", "")
    repo = metadata.get("repo", "")
    file_path = metadata.get("file_path", "")

    if doc_type == "adr":
        # Extract ADR ID from file path or use a fallback
        if file_path:
            filename = file_path.split("/")[-1]
            adr_id = filename.rsplit(".", 1)[0] if "." in filename else filename
            return f"ADR: {adr_id}"
        return f"ADR - {repo}"

    elif doc_type == "readme":
        return f"README.md - {repo}"

    elif doc_type == "doc":
        if file_path:
            filename = file_path.split("/")[-1]
            doc_name = filename.rsplit(".", 1)[0] if "." in filename else filename
            return f"{doc_name}.md - {repo}"
        return f"Doc - {repo}"

    elif doc_type == "dependency":
        language = metadata.get("language", "")
        if file_path:
            filename = file_path.split("/")[-1]
            return f"{filename} ({language}) - {repo}"
        return f"Dependencies ({language}) - {repo}"

    elif doc_type in ("code-map", "handler-group-summary", "infrastructure-summary"):
        return f"Code map - {repo}"

    else:
        return f"{doc_type} - {repo}" if doc_type else f"Document - {repo}"


def query_s3_vectors(
    query_embedding: List[float],
    top_k: int = 5,
    repo_filter: Optional[str] = None,
    doc_types: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Query S3 Vectors for similar documents.

    Args:
        query_embedding: 1024-dimensional query vector
        top_k: Number of results to return
        repo_filter: Optional repo name to filter
        doc_types: Optional list of document types to filter

    Returns:
        List of documents with scores and metadata
    """
    try:
        query_params = {
            "vectorBucketName": VECTOR_BUCKET,
            "indexName": VECTOR_INDEX,
            "queryVector": {"float32": query_embedding},
            "topK": top_k,
            "returnMetadata": True,
            "returnDistance": True,
        }

        # Build filter expression
        # S3 Vectors uses a structured filter format
        if repo_filter:
            query_params["filter"] = {"repo": {"equals": repo_filter}}
            logger.info(f"[query-kb] Filtering by repo: {repo_filter}")

        # Note: doc_types filter would need multiple OR conditions
        # For now, we filter client-side if doc_types is specified
        if doc_types:
            logger.info(f"[query-kb] Will filter doc_types client-side: {doc_types}")

        response = s3vectors_client.query_vectors(**query_params)

        results = []
        for vector in response.get("vectors", []):
            metadata = vector.get("metadata", {})

            # Client-side doc_types filtering
            if doc_types and metadata.get("type") not in doc_types:
                continue

            # Convert distance to similarity score (cosine distance -> similarity)
            distance = vector.get("distance", 0)
            similarity = 1 - distance  # For cosine, similarity = 1 - distance

            # Extract repo: prefer explicit field, fall back to folder_path for GitHub sources
            repo = metadata.get("repo", "")
            if not repo and metadata.get("source") == "github":
                folder_path = metadata.get("folder_path", "")
                repo = folder_path.split("/")[0] if folder_path else ""

            result = {
                "score": round(similarity, 4),
                "text": metadata.get("content", "")[:20000],
                "source": format_source(metadata),
                "type": metadata.get("type", ""),
                "repo": repo,
                "file_path": metadata.get("file_path", ""),
            }
            results.append(result)

        # Re-sort after client-side filtering and limit to top_k
        results.sort(key=lambda x: x["score"], reverse=True)
        results = results[:top_k]

        logger.info(f"[query-kb] Found {len(results)} results from S3 Vectors")
        return results

    except ClientError as e:
        logger.error(f"[query-kb] Failed to query S3 Vectors: {e}")
        raise


def rerank_results(
    query: str,
    documents: List[Dict[str, Any]],
    top_n: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Rerank documents using Bedrock Rerank API (Cohere Rerank 3.5).

    Uses cross-attention scoring for better precision than cosine similarity.
    Falls back to original order if reranking fails.

    Args:
        query: Original user query
        documents: List of documents from S3 Vectors search
        top_n: Number of results to return (None = return all, reordered)

    Returns:
        Reranked list of documents
    """
    if not documents:
        return documents

    try:
        sources = []
        for doc in documents:
            sources.append({
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "TEXT",
                    "textDocument": {
                        "text": doc.get("text", "")[:4096]
                    }
                }
            })

        rerank_start = time.time()

        rerank_params = {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": sources,
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {
                        "modelArn": f"arn:aws:bedrock:{os.environ.get('RERANK_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))}::foundation-model/{RERANK_MODEL_ID}"
                    }
                }
            }
        }

        if top_n is not None:
            rerank_params["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfResults"] = top_n

        response = bedrock_agent_runtime_client.rerank(**rerank_params)

        rerank_duration_ms = int((time.time() - rerank_start) * 1000)
        logger.info(f"[query-kb] Reranked {len(documents)} documents in {rerank_duration_ms}ms")

        reranked = []
        for result in response.get("results", []):
            idx = result.get("index", 0)
            if idx < len(documents):
                doc = documents[idx].copy()
                doc["rerank_score"] = result.get("relevanceScore", 0)
                reranked.append(doc)

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="query-kb",
            model_id=RERANK_MODEL_ID,
            input_text=f"rerank:{query}",
            output_text=f"reranked {len(reranked)} of {len(documents)} docs",
            duration_ms=rerank_duration_ms,
        )

        return reranked

    except ClientError as e:
        logger.warning(f"[query-kb] Reranking failed, falling back to cosine order: {e}")
        return documents[:top_n] if top_n else documents
    except Exception as e:
        logger.warning(f"[query-kb] Unexpected rerank error, falling back: {e}")
        return documents[:top_n] if top_n else documents


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
        "topK": 5,  // Optional, defaults to 5
        "raw": false,  // Optional, if true skips Claude synthesis
        "repo": "spring-petclinic",  // Optional, filter to specific repo
        "docTypes": ["adr", "doc"],  // Optional, filter to specific document types
        "model": "haiku"  // Optional, "haiku" (default) or "sonnet" (advanced reasoning)
    }

    Returns (normal mode):
    {
        "statusCode": 200,
        "body": {
            "answer": "API routes should be defined using Terraform...",
            "sources": ["ADR: api-gateway-standards", "README.md - hpe-journey"]
        }
    }

    Returns (raw mode):
    {
        "statusCode": 200,
        "body": {
            "results": [
                {"score": 0.93, "text": "...", "source": "ADR: api-gateway-standards"}
            ]
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
    if VECTOR_BUCKET is None:
        load_config()

    try:
        # Extract query, topK, raw mode, model, and optional filters from event
        query = event.get("query")
        top_k = event.get("topK", 5)
        raw_mode = event.get("raw", False)
        repo_filter = event.get("repo")  # Optional repo filter
        doc_types = event.get("docTypes")  # Optional doc type filter
        model = event.get("model", "haiku")  # Optional model selection

        if not query:
            logger.warning("[query-kb] Missing required field: query")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required field: query"}),
            }

        if repo_filter:
            logger.info(f"[query-kb] Processing query: '{query}' in repo '{repo_filter}' (top {top_k} results)")
        else:
            logger.info(f"[query-kb] Processing query: '{query}' (top {top_k} results, all repos)")

        if doc_types:
            logger.info(f"[query-kb] Filtering to doc types: {doc_types}")

        # Step 1: Generate query embedding
        logger.info("[query-kb] Generating query embedding...")
        query_embedding = generate_embedding(query)
        if not query_embedding:
            logger.error("[query-kb] Failed to generate query embedding")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Failed to generate query embedding"}),
            }

        # Step 2: Query S3 Vectors for relevant documents
        logger.info("[query-kb] Querying S3 Vectors...")
        fetch_k = min(top_k * 4, RERANK_MAX_FETCH) if RERANK_ENABLED else top_k
        context_docs = query_s3_vectors(query_embedding, fetch_k, repo_filter, doc_types)

        if not context_docs:
            logger.info("[query-kb] No relevant documents found")
            return {
                "statusCode": 404,
                "body": json.dumps({
                    "answer": "I couldn't find any relevant information in the knowledge base to answer this question. This could mean:\n\n1. The topic hasn't been documented yet\n2. The relevant documentation hasn't been ingested\n3. The query uses different terminology than the documentation\n\nTry rephrasing your question or check if the relevant ADRs and documentation have been added to the knowledge base.",
                    "sources": [],
                    "results": []  # For raw mode compatibility
                }),
            }

        logger.info(f"[query-kb] Found {len(context_docs)} relevant documents")

        # Step 2.5: Rerank results if enabled
        if RERANK_ENABLED and len(context_docs) > 1:
            context_docs = rerank_results(query, context_docs, top_n=top_k)
            # Filter out low-relevance results if min score threshold is set
            # Only apply if results actually have rerank_score (not fallback)
            has_rerank_scores = any("rerank_score" in d for d in context_docs)
            if RERANK_MIN_SCORE > 0 and has_rerank_scores:
                before_count = len(context_docs)
                context_docs = [d for d in context_docs if d.get("rerank_score", 0) >= RERANK_MIN_SCORE]
                logger.info(f"[query-kb] Min score filter ({RERANK_MIN_SCORE}): {before_count} -> {len(context_docs)} docs")
            logger.info(f"[query-kb] Reranked and filtered to {len(context_docs)} docs (requested {top_k})")

        # Raw mode: Return vector results directly without Claude synthesis
        # Used by code generation to avoid double-processing (Claude will process them anyway)
        if raw_mode:
            logger.info("[query-kb] Raw mode enabled, skipping Claude synthesis")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "results": context_docs
                }),
            }

        # Step 3: Invoke ask-claude Lambda to generate answer (normal mode only)
        logger.info(f"[query-kb] Invoking ask-claude Lambda (model: {model})...")
        claude_response = invoke_lambda(
            ASK_CLAUDE_LAMBDA_ARN,
            {
                "query": query,
                "context": context_docs,
                "model": model
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
        usage = result.get("usage", {})

        logger.info(f"[query-kb] Successfully generated answer with {len(sources)} sources")

        # Build response, include usage if available
        response_body = {
            "answer": answer,
            "sources": sources
        }
        if usage:
            response_body["usage"] = usage
            logger.info(
                f"[query-kb] Cost: ${usage.get('cost_usd', 0):.6f} "
                f"({usage.get('input_tokens', 0)} in, {usage.get('output_tokens', 0)} out)"
            )

        return {
            "statusCode": 200,
            "body": json.dumps(response_body),
        }

    except Exception as e:
        logger.error(f"[query-kb] Unexpected error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }
