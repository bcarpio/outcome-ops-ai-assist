# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Knowledge base client.

Invokes the query-kb Lambda to retrieve relevant standards and examples.
Supports dynamic queries based on code generator backend.

Uses raw mode (raw=true) for vector search, then passes results to ask-claude
for Haiku-powered summarization. This keeps cached standards in plans concise
while preserving key patterns and requirements.

Tracks Haiku summarization costs separately from Sonnet code generation costs.
"""

import json
import logging
import os
from typing import TYPE_CHECKING, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from models import TokenUsage
from utils import calculate_cost

if TYPE_CHECKING:
    from codegen_backends.base import CodeGeneratorBackend, StandardsContext
    from repo_config import RequiredAdrsConfig

logger = logging.getLogger()

# Initialize Lambda client (once per container)
lambda_client = boto3.client("lambda")

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Module-level accumulator for Haiku summarization costs
# Reset this before each plan generation to track per-plan costs
_summarization_cost = TokenUsage()


def reset_summarization_cost() -> None:
    """Reset the summarization cost accumulator. Call before each plan generation."""
    global _summarization_cost
    _summarization_cost = TokenUsage()
    logger.info("[kb] Reset summarization cost accumulator")


def get_summarization_cost() -> TokenUsage:
    """Get the accumulated Haiku summarization cost."""
    return _summarization_cost


def _accumulate_summarization_cost(input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    """Add to the accumulated summarization cost."""
    global _summarization_cost
    _summarization_cost = TokenUsage(
        input_tokens=_summarization_cost.input_tokens + input_tokens,
        output_tokens=_summarization_cost.output_tokens + output_tokens,
        total_cost_usd=round(_summarization_cost.total_cost_usd + cost_usd, 6)
    )


# ============================================================================
# Knowledge Base Query
# ============================================================================


def _query_kb_raw(
    query: str,
    top_k: int = 3,
    repo: Optional[str] = None,
    doc_types: Optional[List[str]] = None
) -> List[dict]:
    """
    Query knowledge base and return raw vector search results.

    Args:
        query: Search query
        top_k: Number of top results to return
        repo: Optional repo name to filter results
        doc_types: Optional list of document types to include

    Returns:
        List of raw result dicts with score, text, source
    """
    query_kb_function_name = f"{ENV}-{APP_NAME}-query-kb"

    try:
        payload = {
            "query": query,
            "topK": top_k,
            "raw": True  # Get raw vector results
        }
        if repo:
            payload["repo"] = repo
        if doc_types:
            payload["docTypes"] = doc_types

        response = lambda_client.invoke(
            FunctionName=query_kb_function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        if response.get("FunctionError"):
            error_msg = response_payload.get("errorMessage", "Unknown error")
            logger.error(f"[kb] query-kb Lambda error: {error_msg}")
            return []

        if "body" in response_payload:
            body = json.loads(response_payload["body"])
            return body.get("results", [])

        return []

    except ClientError as e:
        logger.error(f"[kb] Failed to invoke query-kb Lambda: {e}")
        return []
    except Exception as e:
        logger.error(f"[kb] Unexpected error querying KB: {e}")
        return []


def _summarize_with_haiku(query: str, context: List[dict]) -> str:
    """
    Invoke ask-claude Lambda to summarize vector search results with Haiku.

    Tracks token usage and cost, accumulating them in the module-level counter.

    Args:
        query: The original search query
        context: List of raw vector results (score, text, source)

    Returns:
        Summarized answer string, or empty string on failure
    """
    if not context:
        return ""

    ask_claude_function_name = f"{ENV}-{APP_NAME}-ask-claude"

    try:
        payload = {
            "query": query,
            "context": context
        }

        response = lambda_client.invoke(
            FunctionName=ask_claude_function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        if response.get("FunctionError"):
            error_msg = response_payload.get("errorMessage", "Unknown error")
            logger.error(f"[kb] ask-claude Lambda error: {error_msg}")
            return ""

        if "body" in response_payload:
            body = json.loads(response_payload["body"])
            answer = body.get("answer", "")
            sources = body.get("sources", [])

            # Extract and accumulate usage/cost data
            usage = body.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost_usd = usage.get("cost_usd", 0.0)

            if input_tokens or output_tokens:
                _accumulate_summarization_cost(input_tokens, output_tokens, cost_usd)
                logger.info(
                    f"[kb] Haiku summarization: {input_tokens} in, {output_tokens} out, "
                    f"${cost_usd:.6f}"
                )

            if answer:
                # Format with query header and sources
                source_str = ", ".join(sources) if sources else "Unknown"
                return f"# {query}\n\nSources: {source_str}\n\n{answer}"

        return ""

    except ClientError as e:
        logger.error(f"[kb] Failed to invoke ask-claude Lambda: {e}")
        return ""
    except Exception as e:
        logger.error(f"[kb] Unexpected error calling ask-claude: {e}")
        return ""


def query_knowledge_base(
    queries: List[str],
    top_k: int = 3,
    repo: Optional[str] = None,
    doc_types: Optional[List[str]] = None
) -> List[str]:
    """
    Query knowledge base for relevant standards and examples.

    Invokes the query-kb Lambda with raw mode for each query, then passes
    results to ask-claude for Haiku-powered summarization. This keeps
    cached standards concise while preserving key patterns.

    Args:
        queries: List of search queries (e.g., ["Lambda handler standards", "Testing patterns"])
        top_k: Number of top results to return per query (default 3)
        repo: Optional repo name to filter results (e.g., "spring-petclinic")
        doc_types: Optional list of document types to include (e.g., ["adr", "doc"])
                   If not specified, returns all document types.

    Returns:
        List[str]: List of summarized standards from the knowledge base

    Raises:
        Exception: If Lambda invocations fail
    """
    if not queries:
        logger.warning("[kb] No queries provided")
        return []

    if repo:
        logger.info(f"[kb] Querying knowledge base with {len(queries)} queries for repo '{repo}'")
    else:
        logger.info(f"[kb] Querying knowledge base with {len(queries)} queries (all repos)")

    if doc_types:
        logger.info(f"[kb] Filtering to doc types: {doc_types}")

    results = []

    for query in queries:
        logger.info(f"[kb] Query: {query}")

        # Get raw vector results
        raw_results = _query_kb_raw(query, top_k=top_k, repo=repo, doc_types=doc_types)

        if not raw_results:
            logger.info(f"[kb] No results for query: {query}")
            continue

        # Filter by relevance threshold
        relevant_results = [r for r in raw_results if r.get("score", 0) >= 0.3]

        if not relevant_results:
            logger.info(f"[kb] No relevant results (score >= 0.3) for query: {query}")
            continue

        logger.info(f"[kb] Found {len(relevant_results)} relevant results, summarizing with Haiku...")

        # Summarize with ask-claude (Haiku)
        summary = _summarize_with_haiku(query, relevant_results)

        if summary:
            results.append(summary)
            logger.info(f"[kb] Summarized query '{query}' to {len(summary)} chars")
        else:
            logger.warning(f"[kb] Failed to summarize results for query: {query}")

    logger.info(f"[kb] Retrieved {len(results)} summarized results from knowledge base")

    return results


def query_knowledge_base_prioritized(
    queries: List[str],
    target_repo: str,
    top_k: int = 3,
    doc_types: Optional[List[str]] = None
) -> List[str]:
    """
    Query knowledge base with repo prioritization.

    First queries for repo-specific results, then backfills with global results.
    Repo-specific ADRs and patterns take precedence over org-wide standards.

    Args:
        queries: List of search queries
        target_repo: Target repo name (e.g., "spring-petclinic")
        top_k: Number of top results to return per query
        doc_types: Optional list of document types to include (e.g., ["adr", "doc"])
                   If not specified, returns all document types.

    Returns:
        List[str]: Combined results with repo-specific first, then global
    """
    if not queries:
        return []

    logger.info(f"[kb] Prioritized query for repo '{target_repo}' with {len(queries)} queries")

    if doc_types:
        logger.info(f"[kb] Filtering to doc types: {doc_types}")

    # Extract just the repo name from full path if needed (e.g., "owner/repo" -> "repo")
    repo_name = target_repo.split("/")[-1] if "/" in target_repo else target_repo

    # Step 1: Query repo-specific first
    repo_results = query_knowledge_base(queries, top_k=top_k, repo=repo_name, doc_types=doc_types)

    # Step 2: Query global (all repos) to backfill
    global_results = query_knowledge_base(queries, top_k=top_k, repo=None, doc_types=doc_types)

    # Step 3: Merge with deduplication (repo-specific takes priority)
    # Use a simple approach: repo results first, then add global results that aren't duplicates
    seen_texts = set()
    combined = []

    for result in repo_results:
        # Use first 500 chars as dedup key
        key = result[:500] if result else ""
        if key not in seen_texts:
            seen_texts.add(key)
            combined.append(result)

    for result in global_results:
        key = result[:500] if result else ""
        if key not in seen_texts:
            seen_texts.add(key)
            combined.append(result)

    logger.info(
        f"[kb] Prioritized query returned {len(combined)} results "
        f"({len(repo_results)} repo-specific, {len(global_results)} global, "
        f"{len(combined)} after dedup)"
    )

    return combined




# ============================================================================
# Required ADRs Fetcher
# ============================================================================


def fetch_required_adrs(
    adr_names: List[str],
    target_repo: Optional[str] = None
) -> List[tuple]:
    """
    Fetch specific ADRs by name from the knowledge base.

    This is used for required_adrs in .outcomeops.yaml - ADRs that should
    always be included in context regardless of vector search results.

    When target_repo is provided, searches the target repo first, then
    falls back to global search. This allows repo-specific ADRs to
    override organization-wide ADRs with the same name.

    Args:
        adr_names: List of ADR names (e.g., ["ADR-001-terraform-infrastructure-patterns"])
        target_repo: Optional target repo for prioritized search (e.g., "owner/repo")

    Returns:
        List[tuple]: List of (adr_name, content) tuples for found ADRs
    """
    if not adr_names:
        return []

    logger.info(f"[kb] Fetching {len(adr_names)} required ADRs: {adr_names}")
    if target_repo:
        logger.info(f"[kb] Prioritizing repo: {target_repo}")

    results = []
    for adr_name in adr_names:
        # Query KB with the ADR name - using prioritized query if target_repo provided
        # This ensures repo-specific ADRs override global ones
        if target_repo:
            adr_results = query_knowledge_base_prioritized(
                queries=[adr_name],
                target_repo=target_repo,
                top_k=5,
                doc_types=["adr"]
            )
        else:
            adr_results = query_knowledge_base(
                queries=[adr_name],
                top_k=5,
                doc_types=["adr"]
            )

        if adr_results:
            # Return tuple of (name, content) so categorization can use the name
            results.append((adr_name, adr_results[0]))
            logger.info(f"[kb] Found required ADR: {adr_name}")
        else:
            logger.warning(f"[kb] Required ADR not found: {adr_name}")

    logger.info(f"[kb] Fetched {len(results)} required ADRs")
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


# ============================================================================
# Backend-Aware Standards Query
# ============================================================================


def get_standards_for_backend(
    backend: "CodeGeneratorBackend",
    additional_queries: Optional[List[str]] = None,
    target_repo: Optional[str] = None,
    required_adrs: Optional["RequiredAdrsConfig"] = None,
) -> "StandardsContext":
    """
    Get all standards for a specific code generator backend.

    This function queries the knowledge base using the backend's
    language-specific queries to retrieve relevant standards.

    When target_repo is provided, queries are prioritized:
    1. First queries for repo-specific ADRs and patterns
    2. Then backfills with global/org-wide standards
    Repo-specific results take precedence to ensure project-specific
    patterns override generic org standards.

    Args:
        backend: Code generator backend instance
        additional_queries: Optional additional queries from repo config
        target_repo: Optional target repo for prioritized queries (e.g., "spring-petclinic")
        required_adrs: Optional RequiredAdrsConfig with repo and global ADR lists

    Returns:
        StandardsContext with all retrieved standards
    """
    from codegen_backends.base import StandardsContext

    if target_repo:
        logger.info(f"[kb] Getting standards for backend: {backend.name} (prioritizing repo: {target_repo})")
    else:
        logger.info(f"[kb] Getting standards for backend: {backend.name}")

    # Only query ADRs for standards - this filters out general docs (like lambda-generate-code-*.md)
    # which are implementation docs, not prescriptive standards. Repos configure which paths
    # are ingested as ADRs via ingestion.standards_paths in .outcomeops.yaml
    standards_doc_types = ["adr"]

    # Choose query function based on whether we have a target repo
    # If target_repo is set, use prioritized queries (repo-specific first, then global)
    def query_fn(queries: List[str]) -> List[str]:
        if not queries:
            return []
        if target_repo:
            return query_knowledge_base_prioritized(queries, target_repo, doc_types=standards_doc_types)
        return query_knowledge_base(queries, doc_types=standards_doc_types)

    # Query for handler standards
    handler_queries = backend.get_handler_standards_queries()
    handler_standards = query_fn(handler_queries)

    # Query for testing standards
    testing_queries = backend.get_testing_standards_queries()
    testing_standards = query_fn(testing_queries)

    # Query for infrastructure standards
    infra_queries = backend.get_infrastructure_standards_queries()
    infrastructure_standards = query_fn(infra_queries)

    # Query for language-specific standards
    language_queries = backend.get_language_standards_queries()
    language_standards = query_fn(language_queries) if language_queries else []

    # Query for framework-specific standards
    framework_queries = backend.get_framework_standards_queries()
    framework_standards = query_fn(framework_queries) if framework_queries else []

    # Query for code style/verbosity standards (ADR-012)
    code_style_queries = backend.get_code_style_queries()
    code_style_standards = query_fn(code_style_queries) if code_style_queries else []

    # Query additional repo-specific queries
    if additional_queries:
        logger.info(f"[kb] Querying {len(additional_queries)} additional repo-specific queries")
        additional_results = query_fn(additional_queries)
        # Add to language standards (most relevant place)
        language_standards.extend(additional_results)

    # Fetch required ADRs (always included regardless of vector search)
    # Categorize ADRs into appropriate standards lists based on their content
    if required_adrs:
        all_required_adrs = []

        # Fetch repo-specific ADRs (search target repo only)
        # fetch_required_adrs returns List[tuple(name, content)]
        if required_adrs.repo and target_repo:
            logger.info(f"[kb] Fetching {len(required_adrs.repo)} repo-specific required ADRs")
            repo_adr_results = fetch_required_adrs(required_adrs.repo, target_repo=target_repo)
            all_required_adrs.extend(repo_adr_results)

        # Fetch global ADRs (search all repos, no prioritization)
        # fetch_required_adrs returns List[tuple(name, content)]
        if required_adrs.global_adrs:
            logger.info(f"[kb] Fetching {len(required_adrs.global_adrs)} global required ADRs")
            global_adr_results = fetch_required_adrs(required_adrs.global_adrs, target_repo=None)
            all_required_adrs.extend(global_adr_results)

        # Categorize ADRs into appropriate standards lists
        infrastructure_keywords = ["terraform", "infrastructure", "cloudformation", "pulumi", "iac"]
        testing_keywords = ["test", "testing", "pytest", "junit", "jest"]

        for adr_name, adr_content in all_required_adrs:
            adr_name_lower = adr_name.lower()

            if any(kw in adr_name_lower for kw in infrastructure_keywords):
                infrastructure_standards.append(adr_content)
                logger.info(f"[kb] Categorized {adr_name} as infrastructure standard")
            elif any(kw in adr_name_lower for kw in testing_keywords):
                testing_standards.append(adr_content)
                logger.info(f"[kb] Categorized {adr_name} as testing standard")
            else:
                # Default to handler standards for Lambda/handler/general ADRs
                handler_standards.append(adr_content)
                logger.info(f"[kb] Categorized {adr_name} as handler standard")

    context = StandardsContext(
        handler_standards=handler_standards,
        testing_standards=testing_standards,
        infrastructure_standards=infrastructure_standards,
        language_standards=language_standards,
        framework_standards=framework_standards,
        code_style_standards=code_style_standards,
    )

    total_results = (
        len(context.handler_standards)
        + len(context.testing_standards)
        + len(context.infrastructure_standards)
        + len(context.language_standards)
        + len(context.framework_standards)
        + len(context.code_style_standards)
    )
    logger.info(f"[kb] Retrieved {total_results} total standards for {backend.display_name}")

    return context


def get_step_context(
    queries: List[str],
    backend: "CodeGeneratorBackend",
    top_k: int = 3,
) -> List[str]:
    """
    Get step-specific context from knowledge base.

    This is used for per-step queries that aren't covered by general standards.

    Args:
        queries: Step-specific queries
        backend: Code generator backend (for potential query augmentation)
        top_k: Number of results per query

    Returns:
        List of relevant context strings
    """
    if not queries:
        return []

    logger.info(f"[kb] Querying {len(queries)} step-specific queries for {backend.name}")
    return query_knowledge_base(queries, top_k=top_k)
