# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Breaking Changes check handler.

Detects schema, logic, and infrastructure changes, then queries the knowledge base
to find direct consumers with high confidence. Only shows dependencies with HIGH
confidence (handler name + queue/topic/invocation explicitly mentioned).
"""

import json
import logging
from typing import Dict, List, Any, Literal, Optional, Set, Tuple
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
        workspace_ids: REQUIRED per-check retrieval scope. BREAKING_CHANGES is a
            "standards" check scoped to the repo's workspace(s) + is_global
            workspaces. An empty/None scope means resolution failed or the repo
            maps to no workspace; we fail CLOSED (return no results) rather than
            query unscoped -- see the security note below.

    Returns:
        List of KbVectorResult objects with score, text, and source metadata
    """
    # Scope is mandatory. query-kb treats a missing ``workspaceIds`` as "no
    # filter" and searches EVERY workspace, so an unscoped raw query would paste
    # other teams'/tenants' KB chunk text into the PR comment. If the caller
    # could not resolve a scope (None, or [] for an unmapped repo) we fail CLOSED
    # -- report no consumers rather than leak cross-workspace content. Never send
    # an unscoped query from here.
    if not workspace_ids:
        logger.warning(
            "BREAKING_CHANGES vector search skipped: no workspace scope resolved; "
            "failing closed (no consumers) to avoid an unscoped cross-workspace query."
        )
        return []

    # Invoke query-kb in RAW mode, scoped to handler-group-summaries only so that
    # docs/markdown/yaml cannot masquerade as consumers (name + "invoke" in a doc
    # is not a real dependency). The standalone vector-query Lambda was folded
    # into query-kb (S3 Vectors search runs in-process there); process-pr-check
    # already holds invoke on query-kb, so there is no separate function to grant.
    # hybrid=True opts this consumer-detection query into symbol-graph
    # augmentation (ADR-023 seam): query-kb fuses deterministic caller/importer
    # facts for the changed symbol on top of the RAG hits, so consumer
    # detection no longer depends on the rerank scoring a handler-group-summary
    # above drifting docs. Opt-in is per-check: only structural checks pay the
    # router + graph cost, keeping the rest of the PR-check fan-out RAG-only.
    payload: Dict[str, Any] = {
        "query": query,
        "topK": top_k,
        "raw": True,
        "docTypes": ["handler-group-summary"],
        "workspaceIds": workspace_ids,
        "hybrid": True,
    }

    try:
        response = lambda_client.invoke(
            FunctionName=lambda_name,
            Payload=json.dumps(payload)
        )

        # A Lambda-level failure (unhandled exception in query-kb) returns
        # FunctionError with the error payload in Payload. Surface its cause
        # instead of masking it as "Unknown error" in the envelope check below.
        if response.get("FunctionError"):
            raw = response["Payload"].read()
            detail = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            raise RuntimeError(f"query-kb invocation error ({response['FunctionError']}): {detail[:500]}")

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


# Source-code extensions the symbol graph carries facts for (one per registered
# extractor language: Python, TypeScript, Java, C#, Go, ABAP). Plane 1 resolves
# symbols language-agnostically through the reader, so its file scope keys off the
# extension, never a repo-specific path.
_PLANE1_CODE_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".java", ".cs", ".go", ".abap",
)

# Test-file markers excluded from plane 1 (a test's symbols rarely have
# consumers, and co-located tests have no test *directory*). Covers the common
# per-language conventions plus the ``/tests/`` and ``/test/`` directory forms.
_PLANE1_TEST_SUFFIXES = (
    "_test.go", "_test.py",
    ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
    "tests.cs", "test.cs",  # *Tests.cs / *Test.cs (compared lowercased)
)


def _is_plane1_test_file(path: str) -> bool:
    lf = path.lower()
    if "/tests/" in lf or "/test/" in lf or lf.startswith("test/") or lf.startswith("tests/"):
        return True
    if any(lf.endswith(s) for s in _PLANE1_TEST_SUFFIXES):
        return True
    basename = lf.rsplit("/", 1)[-1]
    if basename.startswith("test_") and basename.endswith(".py"):
        return True
    return False


def _plane1_file_scope(changed_files: List[str]) -> List[str]:
    """Plane-1 (symbol-graph) file scope: every changed source file of a
    symbol-graph language that is not a test -- regardless of repo layout.

    Unlike the heuristic's handler.py-only ``relevant_files``, plane 1 resolves
    real code symbols via the (language-agnostic) symbol-graph reader, so it must
    see every changed source module -- a ``shared/`` helper, a Go subpackage, a
    C# service, a TS util -- exactly the high-fan-out code whose change breaks the
    most consumers. The scope is therefore keyed on the source-code EXTENSION, not
    a ``lambda/*.py`` path: the platform is language- and repo-agnostic, and the
    earlier path-scoped version silently returned nothing for every non-Python,
    non-``lambda/`` repo (Go/C#/TS/Java), making plane 1 a no-op there. Terraform
    and docs carry no resolvable code symbol (heuristic / plane-2 territory) and
    are excluded by not being code extensions. (ADR-054 / ADR-055.)
    """
    return [
        f for f in changed_files
        if f.endswith(_PLANE1_CODE_EXTENSIONS) and not _is_plane1_test_file(f)
    ]


def symbol_graph_consumers(
    workspace_ids: List[str],
    repo: str,
    changed_lines_by_file: Dict[str, List[int]],
    *,
    reader: Any = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Plane 1 (ADR-054): real code-symbol consumers of the changed symbols.

    Resolves the symbols whose body the PR changed (``symbol_at_line`` on each
    changed RIGHT-side line) and returns their callers + importers from the
    symbol graph -- precise, and cross-repo because the graph spans ingested
    repos. This replaces the retired prose-similarity heuristic for the
    code-symbol plane.

    Args:
        workspace_ids: candidate scope (the repo's workspace + is_global ones);
            a changed symbol resolves in exactly one, we take the first hit.
        repo: bare repo name of the changed files (symbol-graph key --
            see reference_app_vs_repo_naming: BARE repo, not the full slug).
        changed_lines_by_file: {repo_relative_path: [changed RIGHT-side lines]}.

    Returns:
        ``(consumers, undetermined_files)``.
        ``consumers`` -- one dict per distinct external caller/importer:
        ``{changed_file, changed_line, changed_symbol_id, relation,
        consumer_repo, consumer_file, consumer_line, truncated}``.
        ``changed_line`` is the first changed RIGHT-side line that resolved the
        symbol, so each finding anchors to the symbol it concerns (not the
        file's first added line).
        ``undetermined_files`` -- files whose consumers could NOT be determined
        because the graph was unavailable or errored. Per ADR-054 the caller
        MUST fail closed on these (manual-review WARN), never report "safe".

    ``reader`` is injectable for tests; in production it defaults to the shared
    ``symbol_graph_reader`` module.
    """
    if reader is None:
        from shared import symbol_graph_reader as reader

    consumers: List[Dict[str, Any]] = []
    undetermined: List[str] = []
    seen = set()

    for file_path, lines in changed_lines_by_file.items():
        try:
            # 1. Resolve the enclosing symbols the PR actually changed. A symbol
            #    lives in one workspace; take the first scope that resolves it.
            resolved: Dict[str, str] = {}  # target_symbol_id -> workspace_id
            resolved_line: Dict[str, int] = {}  # target_symbol_id -> first line
            for line in sorted(set(lines)):
                for ws in workspace_ids:
                    enc = reader.symbol_at_line(ws, repo, file_path, line)
                    if enc and enc.target_symbol_id:
                        if enc.target_symbol_id not in resolved:
                            resolved[enc.target_symbol_id] = ws
                            resolved_line[enc.target_symbol_id] = line
                        break

            # No resolvable symbol (comment/whitespace change, or a graph gap
            # such as a decorated fn) is NOT "undetermined" -- the graph was
            # reachable, there is simply no code-symbol signal for this file.
            if not resolved:
                continue

            # 2. Callers + importers of each changed symbol are its consumers.
            #    A cross-workspace consumer's edges live in ITS OWN workspace
            #    partition (the target GSI is keyed {consumer_ws}#{sid}), so query
            #    EVERY workspace in scope, not just the symbol's defining one
            #    (ADR-056 cross-workspace consumer detection). Resolution already
            #    consent-gated which bindings exist, so a workspace with no such
            #    edge simply returns nothing -- no false consumers.
            for sid in resolved:
                for relation, query in (
                    ("caller", reader.callers),
                    ("importer", reader.importers),
                ):
                    for ws in workspace_ids:
                        result = query(ws, sid)
                        for site in result.sites:
                            # Exclude self-references inside the changed file.
                            if site.repo == repo and site.file_path == file_path:
                                continue
                            # Exclude test-file consumers: a test using the symbol
                            # is expected-to-break, not a production-impact signal.
                            if _is_plane1_test_file(site.file_path):
                                continue
                            key = (
                                relation, site.repo, site.file_path,
                                site.line, sid,
                            )
                            if key in seen:
                                continue
                            seen.add(key)
                            consumers.append({
                                "changed_file": file_path,
                                "changed_line": resolved_line[sid],
                                "changed_symbol_id": sid,
                                "relation": relation,
                                "consumer_repo": site.repo,
                                "consumer_file": site.file_path,
                                "consumer_line": site.line,
                                "truncated": not result.complete,
                            })

        except reader.SymbolGraphUnavailable:
            # Fail closed: graph unreachable -> we do not know the consumers.
            logger.warning(
                f"[breaking_changes] symbol graph unavailable for {file_path}; "
                f"failing closed (manual review)."
            )
            undetermined.append(file_path)
        except Exception as e:  # noqa: BLE001 -- any reader error fails closed
            logger.warning(
                f"[breaking_changes] symbol-graph consumer lookup failed for "
                f"{file_path}: {e}; failing closed (manual review)."
            )
            undetermined.append(file_path)

    return consumers, undetermined


def check_breaking_changes(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    query_kb_lambda_name: str,
    *,
    anchors: Optional[Dict[str, List[int]]] = None,
    provider: str = "github",
    connection_id: str | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    reader: Any = None,
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
    per the ADO Repos plan ). The ADO-specific kwargs
    (``connection_id``, ``ado_org``, ``ado_project``, ``ado_repo``)
    are accepted for parity with sibling handlers but not used.

    ``anchors`` maps repo-relative file path -> the full list of 1-based
    added RIGHT-side lines in the PR diff. When supplied, each distinct
    high-confidence breaking change on a changed file whose path is in
    ``anchors`` is emitted as a line-anchored inline Finding
    (shared.pr_findings). The plane-1 symbol path anchors each finding on
    the specific changed line that resolved the symbol; the heuristic path
    (no per-symbol line) anchors on the file's first added line and emits
    at most one finding per (file, handler) so co-located findings are not
    fanned out. Files absent from ``anchors`` (pure deletions, or when
    anchor computation failed and ``anchors`` is {}/None) are not anchored
    and stay in the ``details`` summary only.

    Scope is split by plane (ADR-054/ADR-055). The heuristic path keeps the
    handler.py + terraform ``relevant_files`` set (handler-named by
    construction). The plane-1 symbol-graph path scans ALL changed non-test
    Python under ``lambda/`` -- crucially ``shared/*.py`` and non-handler
    modules -- so a change to a high-fan-out shared symbol surfaces its real
    cross-file consumers; a PR touching only ``shared/*.py`` still runs plane 1
    instead of short-circuiting to PASS. ``reader`` is injectable for tests;
    production leaves it ``None`` and plane 1 imports ``symbol_graph_reader``.
    """
    logger.info(
        f"Running breaking changes check for PR #{pr_number} "
        f"(provider={provider})"
    )

    # Heuristic scope (handler-to-handler runtime topology via KB
    # handler-group-summaries): handler.py + terraform only. This path is
    # handler-named by construction, so plane-1 widening must NOT leak into it.
    relevant_files = [
        f for f in changed_files
        if (
            (f.startswith("lambda/") and f.endswith("/handler.py") and "/tests/" not in f)
            or (f.startswith("terraform/") and f.endswith(".tf"))
        )
    ]

    # Plane-1 scope (symbol graph): all changed non-test Python under lambda/,
    # built independently of the heuristic so shared/*.py and non-handler
    # modules are covered (ADR-054/ADR-055). A PR that touches ONLY shared/ has
    # an empty heuristic scope but a non-empty plane-1 scope -- the guard below
    # must not short-circuit it to PASS.
    plane1_files = _plane1_file_scope(changed_files)

    if not relevant_files and not plane1_files:
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
    # is_global workspaces. If scope cannot be resolved we fail CLOSED --
    # query_vector_search skips the query and reports no consumers rather
    # than issue an unscoped (cross-workspace) search. Never workspace-blind.
    scope_workspace_ids: Optional[List[str]] = None
    try:
        from shared.pr_check_scope import resolve_pr_check_workspace_scope
        scope_workspace_ids = resolve_pr_check_workspace_scope(
            check_type=check_type, repo_name=repository,
        )
    except Exception as scope_exc:
        logger.warning(
            f"Could not resolve workspace scope for BREAKING_CHANGES: "
            f"{scope_exc}. Failing closed -- consumer detection skipped."
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

        if relevant_files:
            details.append(f"Modified {len(relevant_files)} file(s): {change_types_list} changes")
            details.append("")
            details.append("No direct consumers detected in knowledge base.")
            details.append("These may be new handlers, internal-only, or not yet documented.")
            details.append("Changes appear safe to merge.")
        else:
            # Plane-1-only PR (e.g. shared/*.py): the handler heuristic has
            # nothing to say; the symbol-graph section below carries the result.
            details.append(
                f"Scanned {len(plane1_files)} code module(s) for symbol-graph consumers."
            )

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
            # Only files that are in the PR diff with a resolvable added line
            # can be anchored; others stay in the summary.
            anchorable_files = [f for f in handler_files if f in anchors]
            # The heuristic has no per-symbol line, so every consumer of a
            # handler would anchor to the same first-added line. Emit ONE
            # finding per (file, handler) that enumerates the named consumers
            # (ADR-032: name them, never an aggregate count) rather than
            # fanning N identical-line findings.
            consumer_names = sorted({dep.source for dep in deps})
            consumers_phrase = ", ".join(f"'{c}'" for c in consumer_names)
            for path in anchorable_files:
                message = (
                    f"Potential breaking change in '{handler_name}': direct "
                    f"consumer(s) {consumers_phrase} depend on this and may be "
                    f"affected."
                )
                findings.append(
                    Finding(
                        path=path,
                        line=anchors[path][0],
                        side="RIGHT",
                        severity="warning",
                        message=message,
                        enclosing_symbol=None,
                    ).model_dump()
                )

    # Plane 1 (ADR-054): precise code-symbol consumers via the symbol graph.
    # Additive to the summary heuristic above (that heuristic is retired in a
    # later chunk): plane 1 can upgrade PASS -> WARN but never the reverse.
    # `anchors` gives us the changed lines to resolve the enclosing symbols;
    # with no anchors we cannot anchor a Finding (ADR-031), so plane 1 is
    # skipped. Fail CLOSED: a file whose consumers cannot be determined becomes
    # a manual-review WARN, never a silent "safe".
    #
    # We thread the FULL changed-line set per file (anchors is now the complete
    # added-line list), so multiple changed symbols in one file are each
    # resolved and each finding anchors to its own symbol's line.
    if anchors and scope_workspace_ids:
        # Symbol graph keys on the BARE repo name (reference_app_vs_repo_naming),
        # not the owner/repo (or org/project/repo) slug.
        bare_repo = repository.split("/")[-1]
        # Plane-1 scans its OWN widened file set (shared/*.py + non-handler
        # modules), not the handler-scoped `relevant_files`. `anchors` already
        # carries added lines for every changed file, so the lines are present.
        changed_lines_by_file = {
            f: anchors[f] for f in plane1_files if f in anchors
        }
        plane1_consumers, plane1_undetermined = symbol_graph_consumers(
            scope_workspace_ids, bare_repo, changed_lines_by_file, reader=reader
        )

        if plane1_consumers:
            status = "WARN"
            if "safe to modify" in message:
                message = "Direct consumers detected (symbol graph)"
            details.append("")
            details.append("**Code-symbol consumers (symbol graph):**")
            for c in plane1_consumers:
                details.append(
                    f"- {c['relation']}: {c['consumer_repo']}/"
                    f"{c['consumer_file']}:{c['consumer_line']} uses "
                    f"`{c['changed_symbol_id']}`"
                )
                findings.append(
                    Finding(
                        path=c["changed_file"],
                        line=c["changed_line"],
                        side="RIGHT",
                        severity="warning",
                        message=(
                            f"Potential breaking change to "
                            f"`{c['changed_symbol_id']}`: {c['relation']} "
                            f"{c['consumer_repo']}/{c['consumer_file']}:"
                            f"{c['consumer_line']} depends on it."
                        ),
                        enclosing_symbol=None,
                    ).model_dump()
                )

        if plane1_undetermined:
            status = "WARN"
            if "safe to modify" in message:
                message = "Consumer analysis incomplete - manual review required"
            details.append("")
            details.append(
                "**Could not determine code-symbol consumers for "
                f"{', '.join(sorted(set(plane1_undetermined)))}** "
                "(symbol graph unavailable) - manual review required."
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
