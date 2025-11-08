"""
Breaking Changes check handler.

Detects schema, logic, and infrastructure changes, then queries the knowledge base
to find direct consumers with high confidence. Only shows dependencies with HIGH
confidence (handler name + queue/topic/invocation explicitly mentioned).
"""

import json
import logging
from typing import Dict, List, Any, Literal, Set
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
lambda_client = boto3.client("lambda")

ChangeType = Literal["schema", "logic", "infra"]
ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]


class KbVectorResult:
    """Vector search result with metadata."""
    def __init__(self, score: float, text: str, source: str):
        self.score = score
        self.text = text
        self.source = source


class Dependency:
    """Detected dependency on a handler."""
    def __init__(self, handler_name: str, description: str, confidence: ConfidenceLevel, source: str):
        self.handler_name = handler_name
        self.description = description
        self.confidence = confidence
        self.source = source


def query_vector_search(lambda_name: str, query: str, top_k: int = 5) -> List[KbVectorResult]:
    """
    Query knowledge base vector search directly to get full results with metadata.

    Args:
        lambda_name: query-kb Lambda name
        query: Search query
        top_k: Number of results to return

    Returns:
        List of KbVectorResult objects with score, text, and source metadata
    """
    # Query the vector-query Lambda directly (not query-kb orchestrator)
    vector_lambda_name = lambda_name.replace("query-kb", "vector-query")

    payload = {
        "query": query,
        "topK": top_k
    }

    try:
        response = lambda_client.invoke(
            FunctionName=vector_lambda_name,
            Payload=json.dumps(payload)
        )

        results = json.loads(response["Payload"].read())

        # Parse results into KbVectorResult objects
        if isinstance(results, list):
            return [
                KbVectorResult(
                    score=r.get("score", 0.0),
                    text=r.get("text", ""),
                    source=r.get("source", "")
                )
                for r in results
            ]

        return []

    except ClientError as e:
        logger.error(f"Failed to query vector search: {e}")
        raise


def extract_handler_name(file_path: str) -> str:
    """
    Extract handler name from file path.

    Examples:
        lambda/hello/handler.py -> hello
        lambda/analyze-pr/handler.py -> analyze-pr
        terraform/lambda.tf -> lambda
    """
    parts = file_path.split("/")

    # Lambda handler: lambda/<name>/handler.py -> <name>
    if len(parts) >= 3 and parts[0] == "lambda" and parts[2] == "handler.py":
        return parts[1]

    # Terraform file: terraform/<name>.tf -> <name>
    if parts[0] == "terraform" and parts[-1].endswith(".tf"):
        return parts[-1].replace(".tf", "")

    # Fallback: use last part without extension
    return parts[-1].replace(".py", "").replace(".tf", "")


def detect_change_type(file_path: str) -> ChangeType:
    """Detect change type from file path."""
    # Terraform infrastructure changes
    if file_path.startswith("terraform/") and file_path.endswith(".tf"):
        return "infra"

    # Lambda handler changes (logic)
    if file_path.startswith("lambda/") and file_path.endswith("/handler.py"):
        return "logic"

    # Schema changes (we don't have separate schema files in Python, but check for models/schemas)
    if "schema" in file_path.lower() or "model" in file_path.lower():
        return "schema"

    return "logic"  # Default


def filter_handler_summaries(results: List[KbVectorResult]) -> List[KbVectorResult]:
    """
    Filter KB results to only handler-group-summary types.

    Filters out generic architectural descriptions (code-map, repo-overview).
    """
    filtered = []

    for result in results:
        source_lower = result.source.lower()

        # Keep only handler-group summaries, filter out code-maps and repo overviews
        is_code_map = "code-map" in source_lower or "code map" in source_lower
        is_repo_overview = "repo-overview" in source_lower or "repo overview" in source_lower

        if not is_code_map and not is_repo_overview:
            filtered.append(result)

    return filtered


def calculate_confidence(handler_name: str, summary_text: str) -> ConfidenceLevel:
    """
    Calculate confidence level for a dependency.

    HIGH: Handler name + queue/topic explicitly mentioned
    MEDIUM: Generic "processes events from..."
    LOW: Vague architectural reference
    """
    lower_text = summary_text.lower()
    lower_handler = handler_name.lower()

    # High confidence: Handler name + specific queue/topic/source
    has_handler_name = lower_handler in lower_text
    has_queue = "queue" in lower_text or "sqs" in lower_text
    has_topic = "topic" in lower_text or "sns" in lower_text
    has_eventbridge = "eventbridge" in lower_text or "event bridge" in lower_text
    has_invoke = "invoke" in lower_text or "calls" in lower_text

    if has_handler_name and (has_queue or has_topic or has_eventbridge or has_invoke):
        return "HIGH"

    # Medium confidence: Generic dependency mention
    if has_handler_name and any(word in lower_text for word in ["depend", "process", "consume"]):
        return "MEDIUM"

    # Low confidence: Vague reference
    return "LOW"


def create_tailored_query(handler_name: str, change_type: ChangeType) -> str:
    """Create tailored query based on change type."""
    if change_type == "schema":
        return f"handlers that consume {handler_name} events or validate {handler_name} schema"
    elif change_type == "logic":
        return f"handlers that invoke or depend on {handler_name}"
    elif change_type == "infra":
        return f"services that depend on {handler_name} infrastructure"
    else:
        return f"handlers that depend on {handler_name}"


def check_breaking_changes(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    query_kb_lambda_name: str
) -> Dict[str, Any]:
    """
    Breaking Changes check handler.

    Detects schema, logic, and infrastructure changes, then queries the knowledge base
    to find direct consumers with high confidence. Only shows dependencies with HIGH
    confidence (handler name + queue/topic/invocation explicitly mentioned).

    Filters out generic architectural descriptions and focuses on handler-group summaries.
    """
    logger.info(f"Running breaking changes check for PR #{pr_number}")

    # Detect change types for all relevant files
    relevant_files = [
        f for f in changed_files
        if (
            (f.startswith("lambda/") and f.endswith("/handler.py") and "/tests/" not in f)
            or (f.startswith("terraform/") and f.endswith(".tf"))
        )
    ]

    if not relevant_files:
        return {
            "checkType": check_type,
            "status": "PASS",
            "message": "No schema, logic, or infrastructure changes detected",
            "details": []
        }

    # Group files by handler and change type
    changes_by_handler: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"files": [], "change_types": set()}
    )

    for file in relevant_files:
        handler_name = extract_handler_name(file)
        change_type = detect_change_type(file)

        changes_by_handler[handler_name]["files"].append(file)
        changes_by_handler[handler_name]["change_types"].add(change_type)

    logger.info(f"Checking dependencies for {len(changes_by_handler)} handlers")

    # Query knowledge base for dependencies with tailored queries
    high_confidence_dependencies: List[Dependency] = []

    for handler_name, change_data in changes_by_handler.items():
        change_types: Set[ChangeType] = change_data["change_types"]

        # Skip infra-only changes (just note them later)
        if len(change_types) == 1 and "infra" in change_types:
            logger.info(f"Skipping KB query for {handler_name} (infra-only change)")
            continue

        # Determine primary change type (schema takes precedence over logic)
        primary_change_type: ChangeType = "schema" if "schema" in change_types else "logic"
        query = create_tailored_query(handler_name, primary_change_type)

        try:
            logger.info(f"Querying KB for {handler_name} ({primary_change_type} change): {query}")
            raw_results = query_vector_search(query_kb_lambda_name, query, top_k=5)

            # Filter to handler-group-summary only
            filtered_results = filter_handler_summaries(raw_results)

            logger.info(
                f"Filtered {len(raw_results)} -> {len(filtered_results)} results for {handler_name}"
            )

            # Calculate confidence and collect HIGH confidence dependencies
            for result in filtered_results:
                confidence = calculate_confidence(handler_name, result.text)

                if confidence == "HIGH":
                    high_confidence_dependencies.append(
                        Dependency(
                            handler_name=handler_name,
                            description=result.text,
                            confidence=confidence,
                            source=result.source
                        )
                    )
                    logger.info(f"HIGH confidence dependency found: {result.source}")

        except Exception as e:
            logger.error(f"Error querying dependencies for {handler_name}: {e}")

    # Build response based on findings
    details = []

    if high_confidence_dependencies:
        status = "WARN"
        message = f"{len(high_confidence_dependencies)} direct consumer(s) detected"

        details.append("**Direct consumers identified:**")
        details.append("")

        # Group by handler
        deps_by_handler: Dict[str, List[Dependency]] = defaultdict(list)
        for dep in high_confidence_dependencies:
            deps_by_handler[dep.handler_name].append(dep)

        for handler_name, deps in deps_by_handler.items():
            details.append(f"**{handler_name}:**")
            for dep in deps:
                details.append(f"- **{dep.source}**")
                # Truncate description to first 200 chars
                desc_preview = dep.description[:200] + "..." if len(dep.description) > 200 else dep.description
                details.append(f"  {desc_preview}")
            details.append("")

        details.append("**Action Required:**")
        details.append("- Review the consumers listed above")
        details.append("- Test with actual consumers if possible")
        details.append("- Consider backward compatibility")
        details.append("- Update integration tests")

    else:
        # No HIGH confidence dependencies found
        status = "PASS"
        message = "No dependencies found - safe to modify"

        # Collect all unique change types
        all_change_types = set()
        for change_data in changes_by_handler.values():
            all_change_types.update(change_data["change_types"])

        change_types_list = ", ".join(sorted(all_change_types))

        details.append(f"Modified {len(relevant_files)} file(s): {change_types_list} changes")
        details.append("")
        details.append("No direct consumers detected in knowledge base.")
        details.append("These may be new handlers, internal-only, or not yet documented.")
        details.append("Changes appear safe to merge.")

    return {
        "checkType": check_type,
        "status": status,
        "message": message,
        "details": details
    }
