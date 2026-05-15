"""Chat-side query intent router.

Classifies a user's chat turn into one of five intents so the chat
Lambda can decide which retrieval surfaces fire:

- ``pattern``     — prose / architecture / standards / ADR / compliance.
                    RAG only.
- ``dependency``  — package / version / lockfile / CVE.
                    Manifest layer only.
- ``structural``  — callers, importers, implementers, subpath imports
                    of a specific symbol. Symbol graph primary.
- ``hybrid``      — needs more than one of the above. All retrievals
                    fan out in parallel.
- ``unknown``     — ambiguous or out-of-band. Defaults to fan-out
                    across all surfaces (false-negatives are more
                    harmful than wasted lookups).

The router also extracts a single symbol candidate (the
``symbol_candidate`` field on ``RouterDecision``) so the context
builder can resolve it against the graph's ``defines`` records. v1
extracts one symbol per turn; multi-symbol questions can still be
answered because the model reasons over whatever results land in the
context block.

v1 implementation is a small Bedrock Haiku call (one Bedrock invocation
per chat turn — ~$0.0001, ~$1 per workspace per 10K turns/month).
Every invocation is audited via ``log_audit_entry()`` with
``lambda_name="chat:router"`` so router spend appears as its own line
in per-workspace analytics, not buried inside chat total.

Question text is **never** stored in the audit stream — only a SHA256
hash of the question lands in ``flagged_reason``, matching the
platform's existing PII discipline.

Test-isolation hooks (``_set_bedrock_client_for_tests`` /
``_reset_clients_for_tests``) mirror ``shared.symbol_graph_reader`` so
tests can inject a stub Bedrock client and bypass the cross-suite
``sys.modules['boto3']`` MagicMock contamination documented in
``shared.symbol_graph_writer._resource``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


VALID_INTENTS = ("pattern", "dependency", "structural", "hybrid", "unknown")


@dataclass(frozen=True)
class RouterDecision:
    """The router's output for one chat turn.

    ``intent`` is one of ``VALID_INTENTS``.

    ``symbol_candidate`` is the user-visible symbol name pulled from
    the question (e.g. ``report_repos_snapshot``,
    ``@hyatt-hpe/hpe-utils/http2``). May be ``None`` for pure
    pattern / dependency / unknown classifications, or for structural
    queries where the model could not isolate a symbol — the context
    builder treats both as "no graph lookup possible".

    ``model_id`` and ``duration_ms`` are surfaced so the caller can
    attribute the router's Bedrock spend per workspace + user.
    """

    intent: str
    symbol_candidate: Optional[str]
    model_id: str
    duration_ms: int


# ---------------------------------------------------------------------------
# Router prompt — response-format clarification and the symbol-extraction
# sub-prompt are appended on top of the intent-classification core.
# ---------------------------------------------------------------------------


_ROUTER_SYSTEM_PROMPT = """You classify a single user question into one of \
five query intents so a downstream retrieval system knows which indexes to \
search.

Classify the user's question into one of these intents.

PATTERN: about concepts, conventions, architecture, standards,
  compliance, gap analysis, ADR synthesis
  Examples: "How does our analytics platform work?", "What ISO
  controls apply to hybrid SAP?", "What are our Python standards?"

DEPENDENCY: about packages, versions, vulnerabilities, manifests
  Examples: "Which apps have fast-xml-parser installed?", "Is
  CVE-2023-XXXX patched in our portfolio?", "What version of
  react-router is in production?"

STRUCTURAL: about code relationships - callers, references,
  implementations, subpath imports of a specific symbol
  Examples: "Who calls validate_input?", "What implements
  IBookingClient?", "Which apps import @hyatt-hpe/hpe-utils/http2?"

HYBRID: requires more than one of the above
  Examples: "Which functions access PII and need ISO 27018
  controls?", "Sev2 blast radius and remediation steps", "CVE
  response with subpath import detail across our portfolio"

UNKNOWN: ambiguous or doesn't fit cleanly

Also extract the single symbol the question is about (a function name,
class name, interface, package, or subpath like
"@hyatt-hpe/hpe-utils/http2"). If the question is not about a specific
symbol, set "symbol" to null.

Respond with strict JSON in exactly this shape (no prose, no markdown):
{"intent": "pattern|dependency|structural|hybrid|unknown", "symbol": \
"<name or null>"}"""


# ---------------------------------------------------------------------------
# Bedrock model config — Haiku per the plan
# ---------------------------------------------------------------------------


# Default to the basic model env var so an operator who overrides
# ``BEDROCK_BASIC_MODEL_ID`` to point at a different Haiku revision
# automatically picks it up for routing too. Falls back to the chunk's
# stated default if the env var is unset (test environments).
def _router_model_id() -> str:
    return os.environ.get(
        "BEDROCK_BASIC_MODEL_ID",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )


# Bedrock client cache + injection hooks. Same pattern as
# shared.symbol_graph_reader: lazy creation, test injection bypasses the
# cross-suite sys.modules contamination.
_bedrock_client = None
_test_bedrock_override = None


def _bedrock():
    global _bedrock_client
    if _test_bedrock_override is not None:
        return _test_bedrock_override
    if _bedrock_client is None:
        # Match the chat handler's bedrock config (300s read for
        # streaming use) so cold-start path doesn't allocate a second
        # config object. Router calls themselves are tiny (~<1s) but
        # the client is shared with the chat handler at runtime.
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            config=Config(
                read_timeout=30,
                connect_timeout=5,
                retries={"max_attempts": 3},
            ),
        )
    return _bedrock_client


def _set_bedrock_client_for_tests(client) -> None:
    """Test hook — inject a stub Bedrock client."""

    global _test_bedrock_override
    _test_bedrock_override = client


def _reset_clients_for_tests() -> None:
    """Test hook — clear cached client + override."""

    global _bedrock_client, _test_bedrock_override
    _bedrock_client = None
    _test_bedrock_override = None


# ---------------------------------------------------------------------------
# Parse Haiku output -> RouterDecision
# ---------------------------------------------------------------------------


def _normalize_intent(raw: str) -> str:
    """Map a free-form intent token to one of ``VALID_INTENTS``.

    Defensive against case, whitespace, and the model emitting a
    sentence rather than the literal token. Anything we can't map
    cleanly becomes ``unknown`` per the plan's fan-out fallback.
    """

    if not raw:
        return "unknown"
    token = raw.strip().lower()
    # Strip surrounding punctuation/quotes the model sometimes emits.
    token = token.strip(".,:;\"'`")
    # Direct match on a valid intent token.
    if token in VALID_INTENTS:
        return token
    # Substring fallback — covers "this is a structural query." etc.
    for intent in VALID_INTENTS:
        if intent in token:
            return intent
    return "unknown"


def _parse_router_output(text: str) -> tuple[str, Optional[str]]:
    """Extract (intent, symbol_candidate) from the model's response.

    Accepts strict JSON ``{"intent": "...", "symbol": "..."}`` first,
    then falls back to a tolerant single-word match for older / drifted
    Haiku revisions. Symbols come back ``None`` rather than the literal
    string ``"null"`` so the context builder can do a simple None
    check.
    """

    if not text:
        return ("unknown", None)

    stripped = text.strip()

    # Strip Markdown fences the model occasionally emits.
    if stripped.startswith("```"):
        # Drop the opening fence and optional language tag, then trim
        # the closing fence if present.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    # Strict JSON path.
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if isinstance(parsed, dict):
        intent = _normalize_intent(str(parsed.get("intent", "")))
        symbol_raw = parsed.get("symbol")
        if symbol_raw is None or (
            isinstance(symbol_raw, str)
            and symbol_raw.strip().lower() in ("null", "none", "")
        ):
            symbol = None
        else:
            symbol = str(symbol_raw).strip()
        return (intent, symbol)

    # Tolerant fallback — treat the response as a single intent token.
    return (_normalize_intent(stripped), None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_intent(
    question: str,
    *,
    audit_table_name: str = "",
    user_email: str = "",
    workspace_id: str = "",
    source_ip: str = "",
    user_agent: str = "",
) -> RouterDecision:
    """Classify ``question`` and return the routing decision.

    Bedrock-failures degrade gracefully to ``RouterDecision(intent=
    "unknown", symbol_candidate=None, ...)`` so the chat handler falls
    back to fan-out retrieval — the routing-decision default for
    ``unknown``.

    Every successful call is logged via ``log_audit_entry()`` with
    ``lambda_name="chat:router"`` so router spend is attributable to
    the workspace + user that incurred it. Question text is never
    stored — only the SHA256 hash lands in ``flagged_reason`` for
    later correlation.
    """

    # Defensive import — avoid making the chat-handler-side
    # ``from chat.router import classify_intent`` pull in the audit
    # module's transitive boto3 graph at module import time.
    from shared.audit import log_audit_entry

    model_id = _router_model_id()

    if not question or not question.strip():
        return RouterDecision(
            intent="unknown",
            symbol_candidate=None,
            model_id=model_id,
            duration_ms=0,
        )

    bedrock = _bedrock()
    started = time.time()
    try:
        response = bedrock.converse(
            modelId=model_id,
            system=[{"text": _ROUTER_SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": question.strip()}],
                }
            ],
            inferenceConfig={
                # Tight cap — the router output is < 60 tokens.
                "maxTokens": 128,
                "temperature": 0.0,
            },
        )
    except ClientError as exc:
        duration_ms = int((time.time() - started) * 1000)
        logger.warning(
            "[chat.router] Bedrock classify failed (%s); defaulting to 'unknown'",
            exc,
        )
        # Audit the failure so the cost-and-error dashboard still sees
        # the router attempt — we never want a silent router outage.
        try:
            log_audit_entry(
                audit_table_name=audit_table_name,
                lambda_name="chat:router",
                model_id=model_id,
                input_text="",
                output_text="",
                duration_ms=duration_ms,
                status="error",
                flagged_reason=f"q_sha256={hashlib.sha256(question.encode()).hexdigest()}",
                user_email=user_email,
                workspace_id=workspace_id,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.exception("[chat.router] audit emission failed")
        return RouterDecision(
            intent="unknown",
            symbol_candidate=None,
            model_id=model_id,
            duration_ms=duration_ms,
        )
    duration_ms = int((time.time() - started) * 1000)

    # Pull the assistant text out of converse() output.
    text = ""
    try:
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                text += block["text"]
    except (KeyError, TypeError):
        text = ""

    intent, symbol = _parse_router_output(text)
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)

    # Audit the spend. Question text never leaves this function —
    # only its SHA256 hash for correlation with the routing-decision
    # audit stream.
    try:
        log_audit_entry(
            audit_table_name=audit_table_name,
            lambda_name="chat:router",
            model_id=model_id,
            input_text="",
            output_text=intent,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            user_email=user_email,
            workspace_id=workspace_id,
            source_ip=source_ip,
            user_agent=user_agent,
            flagged_reason=f"q_sha256={hashlib.sha256(question.encode()).hexdigest()}",
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.exception("[chat.router] audit emission failed")

    return RouterDecision(
        intent=intent,
        symbol_candidate=symbol,
        model_id=model_id,
        duration_ms=duration_ms,
    )


__all__ = (
    "RouterDecision",
    "VALID_INTENTS",
    "classify_intent",
)
