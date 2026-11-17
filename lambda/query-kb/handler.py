# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
from shared import retrieval as shared_retrieval
from shared.model_client import resolve_profile_arn
from shared.hybrid_augment import augment_with_symbol_graph

# Bedrock model configuration
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)
RERANK_MODEL_ID = os.environ.get("BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0")
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() == "true"
RERANK_MAX_FETCH = int(os.environ.get("RERANK_MAX_FETCH", "50"))
RERANK_MIN_SCORE = float(os.environ.get("RERANK_MIN_SCORE", "0"))

# Symbol-graph table for opt-in hybrid augmentation (ADR-023 seam). When set
# AND the caller passes ``hybrid: true``, query-kb fuses deterministic
# symbol-graph facts into the retrieval context. Unset -> hybrid requests
# degrade to RAG-only (fail-soft).
SYMBOL_GRAPH_TABLE = os.environ.get("SYMBOL_GRAPH_TABLE", "")

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
# ADR-035: bedrock_client is embeddings-only here (Titan via
# invoke_model at line ~114). LLM-completion synthesis is delegated to
# the ask-claude Lambda (which routes through shared.model_client per
# ). bedrock_agent_runtime_client is for Bedrock Knowledge
# Bases -- separate API, separate concern, also out-of-scope per ADR-035.
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
        # ADR-016 v1.2 -- chat/titan-embed profile ARN.
        embed_model_id = (
            resolve_profile_arn("chat", "titan-embed") or EMBEDDING_MODEL_ID
        )
        response = bedrock_client.invoke_model(
            modelId=embed_model_id,
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


def format_source(
    metadata: Dict[str, Any],
    workspace_names: Optional[Dict[str, str]] = None,
) -> str:
    """
    Format document source for display.

    Args:
        metadata: Vector metadata containing type, repo, file_path,
            and ``workspace_id``.
        workspace_names: Optional map from workspace_id -> human-readable
            name. When present and the metadata carries a workspace_id
            resolvable through the map, the returned source string gets
            a `` (Workspace Name)`` suffix so compliance auditors can
            trace which tier of standards a citation came from.

    Returns:
        Formatted source string (e.g., "ADR: ADR-001 (Enterprise
        Architecture)", "README.md - repo-name")
    """
    doc_type = metadata.get("type", "")
    repo = metadata.get("repo", "")
    file_path = metadata.get("file_path", "")
    workspace_id = metadata.get("workspace_id")

    def _suffix() -> str:
        # Attach a resolvable workspace name only when we have one.
        # Silent no-op when the caller didn't supply the map.
        if not workspace_names or not workspace_id:
            return ""
        name = workspace_names.get(workspace_id)
        return f" ({name})" if name else ""

    if doc_type == "adr":
        # Extract ADR ID from file path or use a fallback
        if file_path:
            filename = file_path.split("/")[-1]
            adr_id = filename.rsplit(".", 1)[0] if "." in filename else filename
            return f"ADR: {adr_id}{_suffix()}"
        return f"ADR - {repo}{_suffix()}"

    elif doc_type == "readme":
        return f"README.md - {repo}{_suffix()}"

    elif doc_type == "doc":
        if file_path:
            filename = file_path.split("/")[-1]
            doc_name = filename.rsplit(".", 1)[0] if "." in filename else filename
            return f"{doc_name}.md - {repo}{_suffix()}"
        return f"Doc - {repo}{_suffix()}"

    elif doc_type == "dependency":
        language = metadata.get("language", "")
        if file_path:
            filename = file_path.split("/")[-1]
            return f"{filename} ({language}) - {repo}{_suffix()}"
        return f"Dependencies ({language}) - {repo}{_suffix()}"

    elif doc_type in ("code-map", "handler-group-summary", "infrastructure-summary"):
        return f"Code map - {repo}{_suffix()}"

    else:
        return (
            f"{doc_type} - {repo}{_suffix()}"
            if doc_type
            else f"Document - {repo}{_suffix()}"
        )


def query_s3_vectors(
    query_embedding: List[float],
    top_k: int = 5,
    repo_filter: Optional[str] = None,
    doc_types: Optional[List[str]] = None,
    workspace_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Query S3 Vectors for similar documents.

    Args:
        query_embedding: 1024-dimensional query vector
        top_k: Number of results to return
        repo_filter: Optional repo name to filter
        doc_types: Optional list of document types to filter
        workspace_ids: Optional list of workspace_ids to scope retrieval
            to. Powers the per-check PR scope (of the
            workspace-scope plan): standards checks pass repo+globals,
            README passes just the repo's workspace, dup passes every
            workspace. Empty list == no filter (backwards compat).

    Returns:
        List of documents with scores and metadata
    """
    if repo_filter:
        logger.info(f"[query-kb] Filtering by repo: {repo_filter}")
    if doc_types:
        logger.info(f"[query-kb] Will filter doc_types client-side: {doc_types}")
    if workspace_ids:
        logger.info(f"[query-kb] Scoping to workspace_ids: {workspace_ids}")

    # Build the S3 Vectors filter. ``$in`` for workspace scoping;
    # ``equals`` for the legacy single-repo filter. Both compose so a
    # PR check can filter to (this repo, [these workspaces]) at once.
    filter_dict = None
    if repo_filter and workspace_ids:
        filter_dict = {
            "repo": {"equals": repo_filter},
            "workspace_id": {"$in": list(workspace_ids)},
        }
    elif repo_filter:
        filter_dict = {"repo": {"equals": repo_filter}}
    elif workspace_ids:
        filter_dict = {"workspace_id": {"$in": list(workspace_ids)}}

    # Retrieval mechanics (S3 Vectors query + normalization) live in the
    # shared module so chat and the MCP server cannot drift. query-kb
    # keeps its own filter shape ({"repo": {"equals": ...}}) and projects
    # the normalized result onto its long-standing 6-key output, applying
    # format_source from the passed-through metadata.
    try:
        if doc_types:
            # Explicit doc-type filter -> single-channel. The caller asked for
            # a specific type scope; the two-lane split would broaden it.
            docs = shared_retrieval.query_vectors(
                s3vectors_client=s3vectors_client,
                vector_bucket=VECTOR_BUCKET,
                vector_index=VECTOR_INDEX,
                query_embedding=query_embedding,
                top_k=top_k,
                filter_dict=filter_dict,
                doc_types=doc_types,
                text_truncate=20000,
            )
        else:
            # Default: dual-channel (ADR-033) so the code_map lane is not
            # starved by docs on structural questions -- parity with chat. The
            # repo/workspace scope becomes the base_filter ANDed with each
            # lane's type taxonomy. Lanes are stamped for per-lane rerank.
            docs = shared_retrieval.search_kb_dual_channel(
                query="",
                fetch_k=top_k,
                s3vectors_client=s3vectors_client,
                vector_bucket=VECTOR_BUCKET,
                vector_index=VECTOR_INDEX,
                query_embedding=query_embedding,
                base_filter=filter_dict,
                text_truncate=20000,
            )
    except ClientError as e:
        logger.error(f"[query-kb] Failed to query S3 Vectors: {e}")
        raise

    # Resolve workspace names for citation polish. Only
    # for workspaces that show up in the result set -- one batch DDB
    # lookup, no per-doc round trip. Fail-soft: on any error, sources
    # render without the workspace suffix (pre-fix format).
    workspace_ids_in_results = {
        d["metadata"].get("workspace_id")
        for d in docs
        if d["metadata"].get("workspace_id")
    }
    workspace_names = _resolve_workspace_names(workspace_ids_in_results)

    results = [
        {
            "score": d["score"],
            "text": d["text"],
            "source": format_source(d["metadata"], workspace_names=workspace_names),
            "type": d["type"],
            "repo": d["repo"],
            "file_path": d["file_path"],
            # Surface the workspace_id + human-readable name so
            # downstream consumers (chat, PR-check comment renderer)
            # can display or filter by workspace without re-fetching.
            "workspace_id": d["metadata"].get("workspace_id"),
            "workspace_name": workspace_names.get(
                d["metadata"].get("workspace_id"),
            ),
            # Lane stamp (dual-channel path) so the handler can rerank per lane;
            # empty string on the single-channel (explicit doc_types) path.
            "lane": d.get("lane", ""),
        }
        for d in docs
    ]

    logger.info(f"[query-kb] Found {len(results)} results from S3 Vectors")
    return results


def _resolve_workspace_names(workspace_ids: set) -> Dict[str, str]:
    """Batch-lookup workspace names for the given ids. Returns empty
    dict on any failure so callers degrade cleanly to name-less
    source strings."""
    if not workspace_ids:
        return {}
    try:
        import boto3
        ddb = boto3.resource("dynamodb")
        table_name = os.environ.get("WORKSPACES_TABLE")
        if not table_name:
            return {}
        table = ddb.Table(table_name)
        # BatchGetItem via table resource caps at 100 items per call.
        # Chunk defensively.
        names: Dict[str, str] = {}
        ids = list(workspace_ids)
        for chunk_start in range(0, len(ids), 100):
            batch_keys = [
                {"PK": f"WS#{wid}", "SK": "METADATA"}
                for wid in ids[chunk_start:chunk_start + 100]
            ]
            response = ddb.batch_get_item(
                RequestItems={
                    table_name: {"Keys": batch_keys},
                },
            )
            for item in response.get("Responses", {}).get(table_name, []):
                wid = item.get("workspace_id")
                if wid:
                    names[wid] = item.get("name", wid)
        return names
    except Exception as e:
        logger.warning(f"[query-kb] Could not resolve workspace names: {e}")
        return {}


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
    # Rerank mechanics live in the shared module (same Cohere call, same
    # graceful cosine-order fallback) so chat and the MCP server share one
    # reranker. lambda_name="query-kb" preserves the audit + log shape.
    # NOTE: the Bedrock Rerank API only accepts a FOUNDATION-MODEL ARN and
    # rejects application inference profile ARNs ("invalid model ARN"), so --
    # unlike InvokeModel -- rerank can't attribute to the workload profile
    # (ADR-016 v1.2). Use the bare model id; rerank spend is negligible.
    rerank_id = RERANK_MODEL_ID
    return shared_retrieval.rerank(
        bedrock_agent_runtime_client=bedrock_agent_runtime_client,
        rerank_model_id=rerank_id,
        query=query,
        documents=documents,
        top_n=top_n,
        lambda_name="query-kb",
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
    )


def rerank_dual_channel(
    query: str,
    documents: List[Dict[str, Any]],
    top_k: int,
) -> List[Dict[str, Any]]:
    """Per-lane rerank for dual-channel results (ADR-033).

    Reranks the code_map and docs lanes against their own adaptive budgets so
    the code_map lane keeps its slots instead of losing them to doc-lane prose
    on a combined-pool rerank -- the actual fix this plan ships for query-kb.
    Bare foundation-model id (Bedrock Rerank rejects profile ARNs; ADR-016 v1.2).
    """
    return shared_retrieval.rerank_dual_channel(
        bedrock_agent_runtime_client=bedrock_agent_runtime_client,
        rerank_model_id=RERANK_MODEL_ID,
        query=query,
        documents=documents,
        top_k=top_k,
        lambda_name="query-kb",
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
    )


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
        "model": "haiku",  // Optional, "haiku" (default) or "sonnet" (advanced reasoning)
        "hybrid": false  // Optional (default false). When true AND SYMBOL_GRAPH_TABLE
                         // is set, fuse deterministic symbol-graph facts into the
                         // context for structural questions (ADR-023 seam). Default
                         // off keeps callers RAG-only and cost-neutral (no router/
                         // graph calls). Fail-soft: errors degrade to RAG-only.
    }

    Returns (normal mode):
    {
        "statusCode": 200,
        "body": {
            "answer": "API routes should be defined using Terraform...",
            "sources": ["ADR: api-gateway-standards", "README.md - acme-journey"]
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
        workspace_ids = event.get("workspaceIds") or event.get("workspace_ids")
        model = event.get("model", "haiku")  # Optional model selection
        hybrid = event.get("hybrid", False)  # Optional opt-in symbol-graph fusion

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
        context_docs = query_s3_vectors(
            query_embedding, fetch_k, repo_filter, doc_types,
            workspace_ids=workspace_ids,
        )

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

        # Step 2.5: Rerank results if enabled. Dual-channel results carry a
        # lane stamp -> rerank per lane (protects the code_map lane); the
        # single-channel path (explicit doc_types) has no lanes -> combined.
        if RERANK_ENABLED and len(context_docs) > 1:
            if any(d.get("lane") for d in context_docs):
                context_docs = rerank_dual_channel(query, context_docs, top_k=top_k)
            else:
                context_docs = rerank_results(query, context_docs, top_n=top_k)
            # Log score distribution for threshold tuning
            has_rerank_scores = any("rerank_score" in d for d in context_docs)
            if has_rerank_scores:
                scores = sorted([d.get("rerank_score", 0) for d in context_docs], reverse=True)
                logger.info(f"[query-kb] Rerank scores: max={scores[0]:.3f} min={scores[-1]:.3f} median={scores[len(scores)//2]:.3f} ({len(scores)} docs)")
            # Filter out low-relevance results if min score threshold is set
            if RERANK_MIN_SCORE > 0 and has_rerank_scores:
                before_count = len(context_docs)
                context_docs = [d for d in context_docs if d.get("rerank_score", 0) >= RERANK_MIN_SCORE]
                logger.info(f"[query-kb] Min score filter ({RERANK_MIN_SCORE}): {before_count} -> {len(context_docs)} docs")
            logger.info(f"[query-kb] Reranked and filtered to {len(context_docs)} docs (requested {top_k})")

        # Step 2.75: Opt-in hybrid augmentation (ADR-023 seam). Fuse
        # deterministic symbol-graph facts into the context for structural
        # questions, independent of rerank phrasing. Added AFTER the rerank +
        # min-score filter so the graph block is never evicted by the score
        # gate. Default OFF (``hybrid`` absent) -> zero router/graph calls, so
        # the PR-check fan-out stays cost-neutral. Fail-soft throughout: any
        # error (or an unset SYMBOL_GRAPH_TABLE) degrades to RAG-only.
        if hybrid and SYMBOL_GRAPH_TABLE:
            try:
                # Anchor the graph on the scoped workspaces when provided; for
                # an unscoped query, derive the workspaces the RAG actually hit
                # from the retrieved docs so the symbol resolves where it lives.
                augment_ws = workspace_ids or sorted(
                    {
                        d.get("workspace_id")
                        for d in context_docs
                        if d.get("workspace_id")
                    }
                )
                graph_out = augment_with_symbol_graph(
                    query,
                    augment_ws,
                    user_id=event.get("userEmail", "") or "",
                    source_ip=event.get("sourceIp", "") or "",
                    user_agent=event.get("userAgent", "") or "",
                    symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                )
                graph_block = graph_out.get("graph_block", "")
                if graph_block:
                    # A synthetic context entry carrying the structural facts.
                    # lane="symbol_graph" lets raw-mode consumers tell it apart;
                    # ask-claude reads source/text/score so it flows into
                    # synthesis + the sources list unchanged.
                    context_docs.append(
                        {
                            "score": 1.0,
                            "text": graph_block,
                            "source": "Symbol graph (structural facts)",
                            "type": "symbol-graph",
                            "repo": "",
                            "file_path": "",
                            "workspace_id": (augment_ws[0] if augment_ws else None),
                            "workspace_name": None,
                            "lane": "symbol_graph",
                            "graph_sites": graph_out.get("graph_sites", []),
                            "graph_relation": graph_out.get("graph_relation"),
                        }
                    )
                    logger.info(
                        "[query-kb] Hybrid: added symbol-graph block "
                        "(relation=%s sites=%d)",
                        graph_out.get("graph_relation"),
                        len(graph_out.get("graph_sites") or []),
                    )
                else:
                    logger.info(
                        "[query-kb] Hybrid: no symbol-graph block "
                        "(skip_reason=%s)",
                        graph_out.get("graph_skip_reason"),
                    )
            except Exception as exc:  # noqa: BLE001 - fail-soft to RAG-only
                logger.warning(
                    "[query-kb] Hybrid augmentation failed (%s); RAG-only", exc
                )
        elif hybrid and not SYMBOL_GRAPH_TABLE:
            logger.warning(
                "[query-kb] hybrid requested but SYMBOL_GRAPH_TABLE unset; RAG-only"
            )

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
