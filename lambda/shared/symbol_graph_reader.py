"""Symbol-graph DynamoDB query API.

Reads the structural Fact stream persisted by ``symbol_graph_writer``.
The public surface is intentionally narrow — four query shapes that
cover the proven structural failure cases:

- ``callers(X)``        : "Which Lambdas call ``report_repos_snapshot``?"
- ``importers(X)``      : "Which apps use ``@hyatt-hpe/hpe-utils/http2``?"
- ``implementers(I)``   : "What classes implement ``IBookingClient``?"
- ``definitions(X)``    : "Where is ``report_repos_snapshot`` defined?"

Multi-hop, transitive closure, and ``callees(X)`` are deliberately
deferred to a later chunk if traffic justifies them.

Consumers (``chat``, ``analyze-pr``, ``generate-code``) own
authorization — this module trusts ``workspace_id`` and filters by it
via the GSI partition key. The reader does NOT enforce auth.

Test-isolation hooks (``_set_resource_for_tests`` /
``_reset_clients_for_tests``) mirror the writer pattern in
``symbol_graph_writer.py`` to defend against the cross-suite
sys.modules contamination documented there.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import symbol_graph_pointer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    """One structural edge surfaced to a consumer.

    ``source_symbol_id`` is the caller's canonical ID — the "who called
    X" identity. ``file_path`` / ``line`` anchor the edge in source so
    consumers can deep-link.

    ``signature`` is populated only for ``defines`` rows of callables
    (functions / methods) where the extractor captured a parameter
    annotation set. Other edges and pre-signature rows leave it ``None``.
    Shape: ``{"params": [{"name", "type"?}], "returns"?}`` -- the same
    JSON the writer persists, kept as a dict so the consumer doesn't
    take a dependency on the extractor's ``Signature`` dataclass.
    """

    file_path: str
    line: int
    repo: str
    source_symbol_id: str
    language: str
    signature: Optional[dict] = None


@dataclass(frozen=True)
class EnclosingSymbol:
    """The defining symbol that contains a given (file, line).

    Returned by ``symbol_at_line``. ``target_symbol_id`` is the
    canonical ID of the defined symbol (the function / method / class
    whose body contains the queried line) -- NOT the parent scope.
    Use this when grounding a chat citation of the form
    "name (file:line)" so the cited name is graph-resolved rather
    than inferred by the LLM from surrounding text.
    """

    target_symbol_id: str
    file_path: str
    line: int
    repo: str
    language: str


@dataclass(frozen=True)
class QueryResult:
    """Wrapped result of one query.

    Empty results are first-class: ``QueryResult(sites=[],
    truncated=False, graph_sha=..., staleness_seconds=...)``. Consumers
    rely on this to honestly report "no callers in this workspace"
    instead of fabricating a list.

    ``graph_sha`` is the SHA the answer was anchored on (None for
    workspace-wide queries that span multiple repos — workspace-wide
    queries deliberately do not pin a single SHA).
    ``staleness_seconds`` is "seconds since that SHA was committed";
    for workspace-wide queries it's the worst-case across repos in the
    workspace.
    """

    sites: List[CallSite]
    truncated: bool
    graph_sha: Optional[str]
    staleness_seconds: Optional[int]


class SymbolGraphUnavailable(Exception):
    """Raised after retry exhaustion when DynamoDB transient errors
    persist. Consumers (chat, analyze-pr, generate-code) decide whether
    to degrade gracefully (RAG-only fallback) or refuse to proceed.
    """


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# 3 retries with exponential backoff, max ~2s cumulative. Targets DDB
# transient errors only (throttling, 5xx); everything else propagates.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 0.1
_RETRYABLE_ERROR_CODES = {
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
    "InternalServerError",
    "ServiceUnavailable",
}


def _is_retryable(exc: ClientError) -> bool:
    """Decide whether a ``ClientError`` warrants a retry.

    Inspects the error code first; falls back to the HTTP status (any
    5xx is treated as transient). 4xx codes that aren't on the
    retry-allowlist propagate immediately so the caller sees real
    misconfigurations.
    """

    code = exc.response.get("Error", {}).get("Code", "")
    if code in _RETRYABLE_ERROR_CODES:
        return True
    status = (
        exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if exc.response
        else None
    )
    return isinstance(status, int) and status >= 500


def _query_with_retry(table, **query_kwargs):
    """Wrap ``table.query`` with the retry policy. Raises
    ``SymbolGraphUnavailable`` on exhaustion."""

    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt <= _MAX_RETRIES:
        try:
            return table.query(**query_kwargs)
        except ClientError as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            attempt += 1
            if attempt > _MAX_RETRIES:
                break
            # Exponential backoff: 0.1, 0.2, 0.4 — total < 1s plus the
            # query latencies themselves, well under the 2s cumulative
            # ceiling in the plan.
            time.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise SymbolGraphUnavailable(
        f"DynamoDB transient errors persisted after {_MAX_RETRIES} retries: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Lazy clients (one per container) - same pattern as
# shared.symbol_graph_writer / shared.symbol_graph_pointer.
# ---------------------------------------------------------------------------

_dynamodb_resource = None
_test_resource_override = None

# CloudWatch client for the chunk-08 ``QueryLatency`` metric emission.
# Lazy + best-effort: a publish failure must not break a query that
# already succeeded. Matches the writer's lazy-client pattern.
_cloudwatch_client = None
_test_cloudwatch_override = None


def _cloudwatch():
    """Return a cached CloudWatch client (or the test override).

    Emits ``OutcomeOps/SymbolGraph::QueryLatency`` so the latency alarm
    in terraform/cloudwatch_symbol_graph.tf has something to compare
    against. We emit lazily and best-effort -- the alarm is
    correct-by-construction (no metric = no breach), and a publish
    error must never bubble out of a query.
    """

    global _cloudwatch_client
    if _test_cloudwatch_override is not None:
        return _test_cloudwatch_override
    if _cloudwatch_client is None:
        try:
            _cloudwatch_client = boto3.client("cloudwatch")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[symbol_graph_reader] cloudwatch init failed: %s", exc
            )
            return None
    return _cloudwatch_client


def _set_cloudwatch_for_tests(client) -> None:
    """Test hook -- inject a stub CloudWatch client (or None to
    no-op the metric emission entirely)."""

    global _test_cloudwatch_override
    _test_cloudwatch_override = client


def _emit_query_latency_metric(
    *, index_name: str, latency_ms: float, item_count: int
) -> None:
    """Publish ``QueryLatency`` to ``OutcomeOps/SymbolGraph``.

    The chunk-08 alarm watches the p99 across all dimensions; we attach
    ``Index`` as a dimension so operators can drill into target-kind vs
    target-prefix latency separately if the alarm fires. Best-effort:
    any error is swallowed and logged at WARN. The metric is the
    *graph* query latency, not Lambda-end-to-end -- that's covered by
    the per-Lambda feature-agnostic duration alarms.
    """

    client = _cloudwatch()
    if client is None:
        return
    try:
        client.put_metric_data(
            Namespace="OutcomeOps/SymbolGraph",
            MetricData=[
                {
                    "MetricName": "QueryLatency",
                    "Unit": "Milliseconds",
                    "Value": latency_ms,
                    "Dimensions": [
                        {"Name": "Index", "Value": index_name},
                    ],
                },
                {
                    "MetricName": "QueryItemCount",
                    "Unit": "Count",
                    "Value": float(item_count),
                    "Dimensions": [
                        {"Name": "Index", "Value": index_name},
                    ],
                },
            ],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[symbol_graph_reader] QueryLatency emit failed: %s", exc
        )


def _resource():
    """Return a cached DynamoDB resource.

    Tests inject a moto-backed resource via ``_set_resource_for_tests``
    to bypass the cross-suite sys.modules['boto3'] MagicMock
    contamination documented in ``symbol_graph_writer._resource``.
    """

    global _dynamodb_resource
    if _test_resource_override is not None:
        return _test_resource_override
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def _set_resource_for_tests(resource) -> None:
    """Test hook — inject a DynamoDB resource (typically moto-backed)."""

    global _test_resource_override
    _test_resource_override = resource


def _reset_clients_for_tests() -> None:
    """Test hook — clear cached resource + override."""

    global _dynamodb_resource, _test_resource_override
    global _cloudwatch_client, _test_cloudwatch_override
    _dynamodb_resource = None
    _test_resource_override = None
    _cloudwatch_client = None
    _test_cloudwatch_override = None


# ---------------------------------------------------------------------------
# Table-name resolution
# ---------------------------------------------------------------------------


def _resolve_table_name(
    symbol_graph_table_name: Optional[str],
) -> str:
    name = symbol_graph_table_name or os.environ.get(
        "SYMBOL_GRAPH_TABLE", ""
    )
    if not name:
        raise ValueError(
            "symbol_graph_table_name is required (set SYMBOL_GRAPH_TABLE "
            "env or pass explicitly)"
        )
    return name


# ---------------------------------------------------------------------------
# Subpath heuristic for importers()
# ---------------------------------------------------------------------------


def _is_prefix_query(target: str) -> bool:
    """Distinguish prefix queries from exact-match queries for importers().

    Rules:

    - ``"@hyatt-hpe/hpe-utils"``         : prefix (no slash AFTER the
      package boundary) — returns every consumer of any subpath. The
      writer's GSI2PK encodes the package portion as the prefix, so
      any input WITHOUT a trailing slash and WITHOUT an additional
      path segment after the package is a prefix query.
    - ``"@hyatt-hpe/hpe-utils/*"``       : explicit wildcard — prefix.
    - ``"@hyatt-hpe/hpe-utils/http2"``   : exact subpath match.

    The writer's ``_split_target_prefix`` splits on the LAST ``/`` or
    ``::``. An exact-subpath consumer's GSI2PK is the prefix WITHOUT
    the trailing ``/<subpath>``, while a query for the whole package
    needs to hit that same GSI2PK with no SK condition.

    Heuristic:
    - trailing ``/*``    -> prefix
    - language-prefixed (``ts:@hyatt-hpe/hpe-utils``) and contains no
      additional ``/`` after the package boundary -> prefix
    - otherwise -> exact
    """

    if target.endswith("/*"):
        return True
    # Strip the optional language prefix (``ts:``, ``typescript:``,
    # ``python:``, etc.) before counting slashes. The writer treats
    # ``typescript:@hyatt-hpe/hpe-utils`` as a prefix when used as
    # GSI2PK, so the same shape on the read side is the prefix form.
    body = target
    if ":" in body:
        body = body.split(":", 1)[1]
    # No slash at all -> not a package/subpath name; treat as exact (the
    # caller is asking about a bare symbol, not a subpath wildcard).
    if "/" not in body:
        return False
    # ``@scope/pkg`` (single slash, scoped npm package) -> prefix.
    if body.startswith("@"):
        # ``@scope/pkg`` is exactly one slash; ``@scope/pkg/sub`` is two
        # slashes. We treat the one-slash case as the package-prefix
        # query and the two-or-more-slash case as exact subpath.
        return body.count("/") == 1
    # Non-scoped: ``pkg/subpath`` is exact; ``pkg`` alone (no slash)
    # is already handled above. So non-scoped with at least one slash
    # is exact.
    return False


# ---------------------------------------------------------------------------
# Pointer integration (workspace-wide aggregation)
# ---------------------------------------------------------------------------


def _pointer_for_workspace(
    workspace_id: str,
    repos: Iterable[str],
    code_maps_table_name: Optional[str],
) -> Tuple[Optional[str], Optional[int]]:
    """Aggregate per-repo pointer state into a single workspace-wide answer.

    Design choice (chunk-04 open issue): queries are workspace-wide but
    the pointer is per-(workspace, repo). We surface the OLDEST
    ``graph_sha_committed_at`` across the repos in the result set so
    ``staleness_seconds`` reflects the WORST-case freshness across the
    underlying slices. ``graph_sha`` is set only when exactly one repo
    contributes to the answer — otherwise it's None (no single SHA
    describes a multi-repo answer).

    Returns ``(graph_sha_or_None, staleness_seconds_or_None)``.

    If ``repos`` is empty (the query matched no facts) both fields are
    None — there's nothing to anchor on, and the caller's QueryResult
    already conveys "no results" via ``sites=[]``.
    """

    repos = list(dict.fromkeys(repos))  # de-dupe, preserve order
    if not repos:
        return (None, None)

    if not code_maps_table_name:
        # Pointer integration is best-effort. If the consumer Lambda
        # hasn't configured the code-maps table we still answer the
        # query honestly; we just can't report freshness.
        return (None, None)

    oldest_committed_at: Optional[int] = None
    single_repo_sha: Optional[str] = None
    for repo in repos:
        try:
            state = symbol_graph_pointer.read_pointer(
                workspace_id,
                repo,
                code_maps_table_name=code_maps_table_name,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[symbol_graph_reader] read_pointer failed for %s/%s: %s",
                workspace_id,
                repo,
                exc,
            )
            continue

        if (
            state.graph_sha_committed_at is not None
            and (
                oldest_committed_at is None
                or state.graph_sha_committed_at < oldest_committed_at
            )
        ):
            oldest_committed_at = state.graph_sha_committed_at

        if len(repos) == 1 and state.graph_sha is not None:
            single_repo_sha = state.graph_sha

    staleness: Optional[int]
    if oldest_committed_at is None:
        staleness = None
    else:
        staleness = max(0, int(time.time()) - oldest_committed_at)

    return (single_repo_sha, staleness)


# ---------------------------------------------------------------------------
# Row -> CallSite
# ---------------------------------------------------------------------------


def _row_to_call_site(item: dict) -> CallSite:
    """Project a DynamoDB row onto a ``CallSite``."""

    raw_sig = item.get("signature")
    signature: Optional[dict] = None
    if isinstance(raw_sig, dict):
        # Preserve as-stored. Convert any DDB Decimal-shaped values would
        # go here if signatures ever carried numbers, but today the
        # payload is all strings + a string list.
        signature = {
            "params": [
                {k: v for k, v in p.items()} for p in raw_sig.get("params", [])
            ],
        }
        if "returns" in raw_sig:
            signature["returns"] = raw_sig["returns"]

    return CallSite(
        file_path=str(item.get("file_path", "")),
        line=int(item.get("line", 0)),
        repo=str(item.get("repo", "")),
        source_symbol_id=str(item.get("source_symbol_id", "")),
        language=str(item.get("language", "")),
        signature=signature,
    )


# ---------------------------------------------------------------------------
# Generic GSI query helper
# ---------------------------------------------------------------------------


def _query_gsi(
    table_name: str,
    *,
    index_name: str,
    key_condition,
    limit: int,
) -> Tuple[List[dict], bool]:
    """Run a GSI query bounded by ``limit``; return (items, truncated).

    Pagination via ``LastEvaluatedKey`` continues only while the
    accumulated item count is below ``limit``. ``truncated`` is True
    when we stopped early because the cap was reached AND more items
    exist server-side.
    """

    table = _resource().Table(table_name)
    items: List[dict] = []
    last_key: Optional[dict] = None
    truncated = False

    # Time the whole GSI walk (including pagination) and emit
    # ``OutcomeOps/SymbolGraph::QueryLatency`` so the
    # cloudwatch_symbol_graph.tf p99 alarm has a signal. Use a wall-clock
    # timer because that's what the alarm threshold is calibrated
    # against (200ms ceiling, 50ms target -- both in real time).
    _t_start = time.perf_counter()
    try:
        while True:
            remaining = limit - len(items)
            if remaining <= 0:
                # The previous page already filled the cap. If the
                # previous response carried a LastEvaluatedKey there is
                # at least one more item server-side, so the result is
                # truncated.
                truncated = last_key is not None
                break

            kwargs = {
                "IndexName": index_name,
                "KeyConditionExpression": key_condition,
                "Limit": remaining,
            }
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key

            response = _query_with_retry(table, **kwargs)
            page_items = response.get("Items", []) or []
            items.extend(page_items[:remaining])
            last_key = response.get("LastEvaluatedKey")

            if len(items) >= limit:
                # If there's more server-side OR the page itself
                # overflowed the remaining slot, mark truncated.
                if last_key is not None or len(page_items) > remaining:
                    truncated = True
                break
            if not last_key:
                break
    finally:
        _latency_ms = (time.perf_counter() - _t_start) * 1000.0
        _emit_query_latency_metric(
            index_name=index_name,
            latency_ms=_latency_ms,
            item_count=len(items),
        )

    return items, truncated


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------


def callers(
    workspace_id: str,
    target_symbol_id: str,
    *,
    limit: int = 200,
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> QueryResult:
    """Return every call-site of ``target_symbol_id`` in the workspace.

    Uses ``target-kind-index`` (GSI1) with the partition key
    ``"{workspace_id}#{target_symbol_id}"`` and the sort-key prefix
    ``"calls#"``. Results are returned newest-page-first as DynamoDB
    orders them; consumers that want a stable order should sort by
    ``(repo, file_path, line)`` themselves.
    """

    if not workspace_id or not target_symbol_id:
        raise ValueError("workspace_id and target_symbol_id are required")

    table_name = _resolve_table_name(symbol_graph_table_name)

    key_condition = (
        Key("GSI1PK").eq(f"{workspace_id}#{target_symbol_id}")
        & Key("GSI1SK").begins_with("calls#")
    )

    items, truncated = _query_gsi(
        table_name,
        index_name="target-kind-index",
        key_condition=key_condition,
        limit=limit,
    )
    sites = [_row_to_call_site(it) for it in items]
    sha, staleness = _pointer_for_workspace(
        workspace_id, (s.repo for s in sites), code_maps_table_name
    )
    return QueryResult(
        sites=sites,
        truncated=truncated,
        graph_sha=sha,
        staleness_seconds=staleness,
    )


def importers(
    workspace_id: str,
    target_symbol_id_or_prefix: str,
    *,
    limit: int = 500,
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> QueryResult:
    """Return every importer of ``target_symbol_id_or_prefix``.

    Two modes (see ``_is_prefix_query``):

    - **Prefix** (``"@hyatt-hpe/hpe-utils"`` or ``".../*"``): uses
      ``target-prefix-index`` (GSI2) keyed on the package prefix.
      Returns consumers of any subpath under that prefix.
    - **Exact** (``"@hyatt-hpe/hpe-utils/http2"``): uses
      ``target-kind-index`` (GSI1) with the ``imports#`` SK prefix.
      Returns only consumers of that exact subpath.

    The choice is automatic. Pass ``".../*"`` to force prefix even when
    the heuristic might pick exact.
    """

    if not workspace_id or not target_symbol_id_or_prefix:
        raise ValueError(
            "workspace_id and target_symbol_id_or_prefix are required"
        )

    table_name = _resolve_table_name(symbol_graph_table_name)

    # Strip a literal ``/*`` so the user's wildcard suffix doesn't leak
    # into the GSI2PK lookup (the writer doesn't store the ``*``).
    target = target_symbol_id_or_prefix
    if target.endswith("/*"):
        target = target[:-2]

    if _is_prefix_query(target_symbol_id_or_prefix):
        key_condition = Key("GSI2PK").eq(f"{workspace_id}#{target}")
        items, truncated = _query_gsi(
            table_name,
            index_name="target-prefix-index",
            key_condition=key_condition,
            limit=limit,
        )
    else:
        key_condition = (
            Key("GSI1PK").eq(f"{workspace_id}#{target}")
            & Key("GSI1SK").begins_with("imports#")
        )
        items, truncated = _query_gsi(
            table_name,
            index_name="target-kind-index",
            key_condition=key_condition,
            limit=limit,
        )

    sites = [_row_to_call_site(it) for it in items]
    sha, staleness = _pointer_for_workspace(
        workspace_id, (s.repo for s in sites), code_maps_table_name
    )
    return QueryResult(
        sites=sites,
        truncated=truncated,
        graph_sha=sha,
        staleness_seconds=staleness,
    )


def implementers(
    workspace_id: str,
    target_symbol_id: str,
    *,
    limit: int = 100,
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> QueryResult:
    """Return every implementer of ``target_symbol_id`` (interfaces /
    abstract classes / protocols).

    Uses ``target-kind-index`` (GSI1) with the sort-key prefix
    ``"implements#"``. Same shape as ``callers`` but a different SK
    prefix.
    """

    if not workspace_id or not target_symbol_id:
        raise ValueError("workspace_id and target_symbol_id are required")

    table_name = _resolve_table_name(symbol_graph_table_name)

    key_condition = (
        Key("GSI1PK").eq(f"{workspace_id}#{target_symbol_id}")
        & Key("GSI1SK").begins_with("implements#")
    )
    items, truncated = _query_gsi(
        table_name,
        index_name="target-kind-index",
        key_condition=key_condition,
        limit=limit,
    )
    sites = [_row_to_call_site(it) for it in items]
    sha, staleness = _pointer_for_workspace(
        workspace_id, (s.repo for s in sites), code_maps_table_name
    )
    return QueryResult(
        sites=sites,
        truncated=truncated,
        graph_sha=sha,
        staleness_seconds=staleness,
    )


def definitions(
    workspace_id: str,
    symbol_id: str,
    *,
    limit: int = 50,
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> List[CallSite]:
    """Return every definition of ``symbol_id`` in the workspace.

    The signature is ``list[CallSite]`` rather than ``Optional[CallSite]``
    so cross-repo duplicates (interface re-declarations, code
    duplication) all surface. An empty list means "not found" — never
    None.

    Uses ``target-kind-index`` (GSI1) with the sort-key prefix
    ``"defines#"``. Definitions are stored as edges with kind=defines
    targeting the symbol they define (per the chunk-01 extractor's
    Fact stream).
    """

    if not workspace_id or not symbol_id:
        raise ValueError("workspace_id and symbol_id are required")

    table_name = _resolve_table_name(symbol_graph_table_name)

    key_condition = (
        Key("GSI1PK").eq(f"{workspace_id}#{symbol_id}")
        & Key("GSI1SK").begins_with("defines#")
    )
    items, _truncated = _query_gsi(
        table_name,
        index_name="target-kind-index",
        key_condition=key_condition,
        limit=limit,
    )
    return [_row_to_call_site(it) for it in items]


def symbol_at_line(
    workspace_id: str,
    repo: str,
    file_path: str,
    line: int,
    *,
    symbol_graph_table_name: Optional[str] = None,
) -> Optional[EnclosingSymbol]:
    """Resolve a (file, line) to the innermost defining symbol.

    Returns the symbol whose ``start_line`` is the largest value
    ``<= line`` among defines rows in the same file. v1 uses
    start-line only -- ``end_line`` is a v2 follow-up that requires
    extractor changes across five languages. Until then, a query for
    a line that falls BETWEEN two functions (module-level code, blank
    lines after a function ends) will return the previous function;
    the line-citation grounding contract (ADR-031) tells synthesis to
    prefer dropping the enclosing-name claim in that case rather than
    citing a function that almost contains the line.

    When multiple defines share the same start_line (rare: a class
    whose first member is on the same line, e.g. one-liner C# records),
    prefer the longer ``target_symbol_id`` -- the inner member sorts
    after the outer container by string length, which is a decent
    "innermost" tiebreaker without a kind discriminator.

    ``repo`` is normalized to the bare name for the PK lookup; callers
    may pass either ``owner/name`` or bare. Returns None when no
    defines row in the file has ``start_line <= line``.
    """

    if not workspace_id or not repo or not file_path or line is None:
        raise ValueError(
            "workspace_id, repo, file_path, and line are required"
        )

    bare_repo = repo.rsplit("/", 1)[-1] if "/" in repo else repo
    pk = f"{workspace_id}#{bare_repo}"
    table_name = _resolve_table_name(symbol_graph_table_name)
    table = _resource().Table(table_name)

    items: List[dict] = []
    last_key: Optional[dict] = None
    # Walk every defines page for the repo partition; filter to the
    # requested file server-side. The partition is repo-scoped so this
    # is bounded by defines-per-repo, not workspace-wide. For a chat
    # answer citing N distinct files, the per-call cost is one
    # partition walk per file -- the chat prompt batches citations to
    # keep this bounded.
    while True:
        kwargs = {
            "KeyConditionExpression": (
                Key("PK").eq(pk) & Key("SK").begins_with("defines#")
            ),
            "FilterExpression": Attr("file_path").eq(file_path),
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        response = _query_with_retry(table, **kwargs)
        items.extend(response.get("Items", []) or [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break

    candidates = [it for it in items if int(it.get("line", 0)) <= int(line)]
    if not candidates:
        return None

    # Innermost = largest start_line; tiebreak by longer target_symbol_id
    # (inner member is the longer canonical when both share start line).
    def _rank(it: dict) -> tuple:
        return (int(it.get("line", 0)), len(str(it.get("target_symbol_id", ""))))

    candidates.sort(key=_rank, reverse=True)
    top = candidates[0]
    return EnclosingSymbol(
        target_symbol_id=str(top.get("target_symbol_id", "")),
        file_path=str(top.get("file_path", "")),
        line=int(top.get("line", 0)),
        repo=str(top.get("repo", "")),
        language=str(top.get("language", "")),
    )


def fuzzy_resolve_by_name(
    workspace_id: str,
    bare_name: str,
    *,
    limit: int = 25,
    symbol_graph_table_name: Optional[str] = None,
) -> List[str]:
    """Return distinct canonical ``target_symbol_id`` values in the
    workspace whose tail matches ``bare_name``.

    The chat handler's symbol resolver uses this to map a user-typed
    bare name ("require_auth") back to the canonical IDs the graph
    actually stores ("python:lambda.auth.handler::require_auth"). The
    GSI is partitioned by ``(workspace_id, bare_name)`` so each call
    is a single-partition Query — bounded by actual matches, not by
    workspace size. Replaces the pre-GSI Scan-based fuzzy resolver
    that did not scale past a single repo per workspace.

    Empty list when nothing matches; raises ``SymbolGraphUnavailable``
    on a non-retryable DynamoDB error so the chat augment path can
    degrade to RAG-only the same way every other reader function in
    this module does.

    ``limit`` caps the number of distinct canonicals returned;
    chat-side disambiguation only needs the first handful. Over-fetch
    is applied internally to account for one canonical having many
    edges pointing at it.
    """

    if not workspace_id or not bare_name:
        return []
    table_name = _resolve_table_name(symbol_graph_table_name)

    # Over-fetch by 4x so one heavily-edged canonical doesn't drown
    # out the others before dedupe. Floor at 100 so the index does
    # one decent partition read on small workspaces.
    fetch = max(limit * 4, 100)

    key_condition = Key("GSI3PK").eq(f"{workspace_id}#{bare_name}")
    items, _truncated = _query_gsi(
        table_name,
        index_name="target-name-index",
        key_condition=key_condition,
        limit=fetch,
    )

    seen: set = set()
    out: List[str] = []
    for item in items:
        tsi = item.get("target_symbol_id")
        if not tsi or tsi in seen:
            continue
        seen.add(tsi)
        out.append(tsi)
        if len(out) >= limit:
            break
    return out


__all__ = (
    "CallSite",
    "EnclosingSymbol",
    "QueryResult",
    "SymbolGraphUnavailable",
    "callers",
    "importers",
    "implementers",
    "definitions",
    "symbol_at_line",
    "fuzzy_resolve_by_name",
)
