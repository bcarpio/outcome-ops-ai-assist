# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Query intent router (shared retrieval seam).

Classifies a user's question into one of five intents so a retrieval
surface (chat, query-kb, mcp-server) can decide which indexes fire:

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

# Module-level alias so tests can patch shared.router.invoke_model.
from shared.model_client import invoke as invoke_model

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
    the question (e.g. ``log_audit_entry``,
    ``@acme-corp/shared-utils/http2``). May be ``None`` for pure
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


# v1 — the original taxonomy. Kept for instant rollback via
# CHAT_ROUTER_PROMPT_VERSION=v1 if v2's HYBRID reclassification
# over-fires. Note v1 lumps "compliance, gap analysis" under PATTERN,
# which deterministically skips the graph for security-audit questions
# (the bug ADR-024 fixes).
_ROUTER_SYSTEM_PROMPT_V1 = """You classify a single user question into one of \
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
  IBookingClient?", "Which apps import @acme-corp/shared-utils/http2?"

HYBRID: requires more than one of the above
  Examples: "Which functions access PII and need ISO 27018
  controls?", "Sev2 blast radius and remediation steps", "CVE
  response with subpath import detail across our portfolio"

UNKNOWN: ambiguous or doesn't fit cleanly

Also extract the single symbol the question is about (a function name,
class name, interface, package, or subpath like
"@acme-corp/shared-utils/http2"). If the question is not about a specific
symbol, set "symbol" to null.

Respond with strict JSON in exactly this shape (no prose, no markdown):
{"intent": "pattern|dependency|structural|hybrid|unknown", "symbol": \
"<name or null>"}"""


# v2 (default) — splits verification/audit/gap-analysis OUT of PATTERN
# and into HYBRID. The verification shape ("does X meet standard Y?")
# needs BOTH the standard (RAG) and the implementation (graph); under v1
# it classified as PATTERN and the graph was skipped — the exact case
# the structural-retrieval moat is supposed to win. See ADR-024.
_ROUTER_SYSTEM_PROMPT_V2 = """You classify a single user question into one of \
five query intents so a downstream retrieval system knows which indexes to \
search.

Classify the user's question into one of these intents.

PATTERN: a request to DEFINE, EXPLAIN, or SUMMARIZE a concept,
  convention, architecture, standard, or ADR — answerable from
  documentation alone, with no need to inspect the actual code.
  Examples: "What are our Python standards?", "Explain how our
  analytics platform works", "Summarize the auth ADR", "What ISO
  controls apply to hybrid SAP?"

DEPENDENCY: about packages, versions, vulnerabilities, manifests
  Examples: "Which apps have fast-xml-parser installed?", "Is
  CVE-2023-XXXX patched in our portfolio?", "What version of
  react-router is in production?"

STRUCTURAL: about code relationships - callers, references,
  implementations, subpath imports of a specific symbol
  Examples: "Who calls validate_input?", "What implements
  IBookingClient?", "Which apps import @acme-corp/shared-utils/http2?"

HYBRID: requires more than one of the above. In particular, any
  question that VERIFIES, AUDITS, or GAP-ANALYZES an implementation
  against a standard, ADR, or industry practice is HYBRID — answering
  it requires reading BOTH the standard (documentation) AND the actual
  implementation (code).
  Examples: "Is the magic_link flow secure?", "Does our auth flow meet
  OWASP ASVS L1?", "Are there gaps between our audit logging and SOC 2
  CC7.2?", "Does the file-ingestion path enforce the PII redaction
  ADR?", "Which functions access PII and need ISO 27018 controls?"

UNKNOWN: ambiguous or doesn't fit cleanly

Also extract the single symbol the question is about (a function name,
class name, interface, package, subsystem, or subpath like
"@acme-corp/shared-utils/http2"). Verification/audit questions usually name
a subsystem ("magic_link", "auth_flow", "audit_logging") — extract it.
If the question is not about a specific symbol, set "symbol" to null.

Respond with strict JSON in exactly this shape (no prose, no markdown):
{"intent": "pattern|dependency|structural|hybrid|unknown", "symbol": \
"<name or null>"}"""


def _router_system_prompt() -> str:
    """Select the router prompt by ``CHAT_ROUTER_PROMPT_VERSION`` (default
    v2). Read at call time, not import time, so an operator can flip the
    flag (or a test can monkeypatch it) without a redeploy."""
    version = os.environ.get("CHAT_ROUTER_PROMPT_VERSION", "v2").strip().lower()
    return _ROUTER_SYSTEM_PROMPT_V1 if version == "v1" else _ROUTER_SYSTEM_PROMPT_V2


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


# NOTE (ADR-035 chunk 12): the legacy boto3 bedrock-runtime client
# construction + test-injection hooks (_bedrock / _bedrock_client /
# _test_bedrock_override / _set_bedrock_client_for_tests /
# _reset_clients_for_tests) were removed here. The router now calls
# shared.model_client.invoke (aliased as the module-level `invoke_model`
# above) which has its own backend dispatch and its own test patch
# surface (tests do `patch.object(shared.router, "invoke_model", ...)`).


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
    "pattern", symbol_candidate=None, ...)`` — the narrowest safe
    default (RAG-only, no symbol-graph fan-out). fable-scale chunk 05
    flipped this from ``unknown`` (which triggered fan-out to every
    retrieval surface) so a router throttle doesn't pile MORE Bedrock
    calls onto an already-saturated quota. Trade-off: pattern-only
    retrieval yields less specific answers to structural questions
    under throttle, but that's preferable to compounding the throttle
    with additional graph queries.

    Every successful call is logged via ``log_audit_entry()`` with
    ``lambda_name="chat:router"`` so router spend is attributable to
    the workspace + user that incurred it. Question text is never
    stored — only the SHA256 hash lands in ``flagged_reason`` for
    later correlation.
    """

    # Defensive import — avoid making the chat-handler-side
    # ``from shared.router import classify_intent`` pull in the audit
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

    started = time.time()
    try:
        # Route via the module-level invoke_model alias so tests can
        # patch shared.router.invoke_model without needing the legacy
        # boto3-client injection hook (which is now retained only as a
        # no-op for backward compatibility with other suites).
        # ADR-016 v1.2 -- router uses Haiku ("basic" tier) under the
        # chat workload. model_id is kept for the audit log +
        # RouterDecision return; shared.model_client resolves the
        # profile ARN from BEDROCK_CHAT_BASIC_PROFILE_ARN.
        response = invoke_model(
            workload="chat",
            tier="basic",
            system=_router_system_prompt(),
            messages=[{"role": "user", "content": [{"text": question.strip()}]}],
            # Tight cap -- the router output is < 60 tokens.
            max_tokens=128,
            temperature=0.0,
        )
    except Exception as exc:
        duration_ms = int((time.time() - started) * 1000)
        # fable-scale chunk 05: degrade to 'pattern' (narrow, RAG-only) so a
        # router throttle doesn't fan out MORE Bedrock calls. Prior behavior
        # ('unknown') triggered structural/graph retrieval on top of RAG,
        # compounding quota pressure at exactly the wrong moment.
        #
        # ADR-059: catch EVERY exception, not just ClientError (Bedrock
        # throttle/API). A model-config error also lands here -- a surface
        # wired without BEDROCK_BASIC_MODEL_ID raises BackendConfigError,
        # not ClientError -- and must degrade to 'pattern' rather than
        # propagate. The hybrid_augment DDB-only backstop then still
        # resolves a named symbol with zero Bedrock, so a pinpoint
        # structural question keeps working and the misconfig surfaces as
        # this WARNING (loud to ops) instead of a dropped graph lane
        # (silent to the user). Live-burned 2026-11-16 on query-kb + MCP.
        logger.warning(
            "[shared.router] classify failed (%s); defaulting to 'pattern' (narrow)",
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
            logger.exception("[shared.router] audit emission failed")
        return RouterDecision(
            intent="pattern",
            symbol_candidate=None,
            model_id=model_id,
            duration_ms=duration_ms,
        )
    duration_ms = int((time.time() - started) * 1000)

    # Pull the assistant text from the unified Response.
    text = response.text or ""

    intent, symbol = _parse_router_output(text)
    input_tokens = int(response.usage.input_tokens or 0)
    output_tokens = int(response.usage.output_tokens or 0)

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
        logger.exception("[shared.router] audit emission failed")

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
