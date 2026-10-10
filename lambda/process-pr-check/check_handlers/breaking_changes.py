"""
Breaking Changes check handler.

Detects schema, logic, and infrastructure changes, then queries the knowledge base
to find direct consumers with high confidence. Only shows dependencies with HIGH
confidence (handler name + queue/topic/invocation explicitly mentioned).
"""

import json
import logging
from typing import Dict, List, Any, Literal, Optional, Set
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

from .bedrock_utils import UsageTracker

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


def query_vector_search(
    lambda_name: str, query: str, top_k: int = 5,
    workspace_ids: Optional[List[str]] = None,
) -> List[KbVectorResult]:
    """
    Query knowledge base vector search directly to get full results with metadata.

    Args:
        lambda_name: query-kb Lambda name
        query: Search query
        top_k: Number of results to return
        workspace_ids: Optional per-check retrieval scope (chunk 04
            of the workspace-scope plan). BREAKING_CHANGES is a
            "standards" check: walks the repo's workspace + all
            is_global workspaces.

    Returns:
        List of KbVectorResult objects with score, text, and source metadata
    """
    # Invoke query-kb in RAW mode. The standalone vector-query Lambda was folded
    # into query-kb (S3 Vectors search now runs in-process there), so we call
    # query-kb directly and skip Claude synthesis -- the same raw-mode pattern
    # architectural_duplication uses. process-pr-check already holds invoke on
    # query-kb; there is no separate vector-query function to grant or target.
    payload: Dict[str, Any] = {
        "query": query,
        "topK": top_k,
        "raw": True,
    }
    if workspace_ids:
        payload["workspaceIds"] = workspace_ids

    try:
        response = lambda_client.invoke(
            FunctionName=lambda_name,
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        # query-kb returns an API-Gateway-style envelope: {statusCode, body}.
        if response_payload.get("statusCode") != 200:
            error_body = response_payload.get("body", "{}")
            if isinstance(error_body, str):
                error_body = json.loads(error_body)
            raise RuntimeError(
                f"query-kb raw search failed: {error_body.get('error', 'Unknown error')}"
            )

        body = response_payload.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)

        # Raw mode returns {"results": [{source, text, score}, ...]}
        results = body.get("results", [])
        return [
            KbVectorResult(
                score=r.get("score", 0.0),
                text=r.get("text", ""),
                source=r.get("source", "")
            )
            for r in results
        ]

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
    query_kb_lambda_name: str,
    *,
    anchors: Optional[Dict[str, int]] = None,
    provider: str = "github",
    connection_id: str | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
) -> Dict[str, Any]:
    """
    Breaking Changes check handler.

    Detects schema, logic, and infrastructure changes, then queries the knowledge base
    to find direct consumers with high confidence. Only shows dependencies with HIGH
    confidence (handler name + queue/topic/invocation explicitly mentioned).

    Filters out generic architectural descriptions and focuses on handler-group summaries.

    Provider-aware: BREAKING_CHANGES is provider-agnostic — it works
    entirely off ``changed_files`` (already normalized in the CheckJob)
    and KB queries (already indexed for both GitHub and Azure DevOps
    per the ADO Repos plan chunk 05a). The ADO-specific kwargs
    (``connection_id``, ``ado_org``, ``ado_project``, ``ado_repo``)
    are accepted for parity with sibling handlers but not used.

    ``anchors`` maps repo-relative file path -> 1-based first added
    RIGHT-side line in the PR diff. When supplied, each distinct
    high-confidence breaking change on a changed file whose path is in
    ``anchors`` is emitted as a line-anchored inline Finding
    (shared.pr_findings). Files absent from ``anchors`` (pure deletions,
    or when anchor computation failed and ``anchors`` is {}/None) are not
    anchored and stay in the ``details`` summary only.
    """
    logger.info(
        f"Running breaking changes check for PR #{pr_number} "
        f"(provider={provider})"
    )

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
            "details": [],
            # No line-anchored findings: BREAKING_CHANGES works off file paths
            # and KB summaries only -- it never sees the PR diff, so there is no
            # RIGHT-side line to anchor to. Per shared.pr_findings / ADR-031 we
            # return [] rather than guess a line; the summary carries the result.
            "findings": [],
            "usage": UsageTracker()
        }

    # Resolve per-check retrieval scope. BREAKING_CHANGES is a
    # "standards" check: walks the repo's workspace(s) + all
    # is_global workspaces. Fail-soft: falls back to
    # workspace-blind on any error.
    scope_workspace_ids: Optional[List[str]] = None
    try:
        from shared.pr_check_scope import resolve_pr_check_workspace_scope
        scope_workspace_ids = resolve_pr_check_workspace_scope(
            check_type=check_type, repo_name=repository,
        )
    except Exception as scope_exc:
        logger.warning(
            f"Could not resolve workspace scope for BREAKING_CHANGES: "
            f"{scope_exc}. Falling back to workspace-blind retrieval."
        )

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
            raw_results = query_vector_search(
                query_kb_lambda_name, query, top_k=5,
                workspace_ids=scope_workspace_ids,
            )

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

    # Line-anchored findings for inline PR comments (shared.pr_findings).
    # Each HIGH-confidence dependency is a distinct breaking change attributed
    # to a changed handler. The caller now supplies `anchors` (repo-relative
    # file path -> 1-based first added RIGHT-side line), so we anchor each
    # distinct breaking change to the changed file(s) of its handler that
    # appear in the diff. Per ADR-031 we NEVER guess a line: a flagged file
    # absent from `anchors` (pure deletion, or anchors is {}/None because
    # anchor computation failed) gets no Finding and stays in the `details`
    # summary above. BREAKING_CHANGES verdicts are PASS/WARN only -- no FAIL --
    # so severity is always "warning". enclosing_symbol stays None (ADR-031:
    # never guessed).
    from shared.pr_findings import Finding

    findings: List[Dict[str, Any]] = []
    if anchors:
        # Group the flagged breaking changes by handler (same grouping the
        # summary uses), then map each handler back to its changed files.
        deps_for_findings: Dict[str, List[Dependency]] = defaultdict(list)
        for dep in high_confidence_dependencies:
            deps_for_findings[dep.handler_name].append(dep)

        for handler_name, deps in deps_for_findings.items():
            handler_files = changes_by_handler[handler_name]["files"]
            # Only files that are in the PR diff with a resolvable first-added
            # line can be anchored; others stay in the summary.
            anchorable_files = [f for f in handler_files if f in anchors]
            for path in anchorable_files:
                for dep in deps:
                    message = (
                        f"Potential breaking change in '{handler_name}': "
                        f"direct consumer '{dep.source}' depends on this and "
                        f"may be affected."
                    )
                    findings.append(
                        Finding(
                            path=path,
                            line=anchors[path],
                            side="RIGHT",
                            severity="warning",
                            message=message,
                            enclosing_symbol=None,
                        ).model_dump()
                    )

    # BREAKING_CHANGES doesn't use Claude, return empty usage tracker
    return {
        "checkType": check_type,
        "status": status,
        "message": message,
        "details": details,
        "findings": findings,
        "usage": UsageTracker()
    }
