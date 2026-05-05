"""
Shared PII redaction helper for ingest-time scrubbing.

A single entry point - ``redact()`` - that every ingest Lambda calls
right before embedding text into the workspace's vector index. Honors
two levels of gating:

  1. Org-level (tfvar ``pii_redaction_enabled``, threaded as the
     ``org_enabled`` kwarg). When false, no Comprehend call happens
     anywhere; the helper returns the raw text and a
     ``skipped_reason="org_disabled"``.
  2. Workspace-level (the workspace's ``privacy_mode`` attribute,
     threaded as the ``mode`` kwarg). When ``"NONE"``, no Comprehend
     call happens; ``skipped_reason="workspace_none"``.

Effective behavior: redact iff ``org_enabled AND mode == STRICT``.

Replacement format is ``[REDACTED:<TYPE>]`` where ``<TYPE>`` is the
Comprehend entity type (e.g., ``[REDACTED:EMAIL]``,
``[REDACTED:PHONE]``). Preserves type info so RAG citations stay
readable while keeping the actual PII out of the index.

Non-fatal: ClientError is swallowed (raw text returned with
``skipped_reason="comprehend_error"``) so a transient Comprehend
hiccup never breaks an ingest. Per ADR-018 / shared.audit's
established convention.

The caller writes the audit row that records ``char_count`` +
``summary``; this helper does not write to DynamoDB.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Lazy-init boto3 Comprehend client (one per container, mirrors
# shared/audit.py's _get_dynamodb_table pattern).
_comprehend = None


def _get_client():
    global _comprehend
    if _comprehend is None:
        _comprehend = boto3.client("comprehend")
    return _comprehend


# Modes - kept as plain strings for DDB / API round-trip simplicity.
MODE_STRICT = "STRICT"
MODE_NONE = "NONE"
VALID_MODES = frozenset({MODE_STRICT, MODE_NONE})

# Comprehend's DetectPiiEntities cap is 100,000 UTF-8 bytes per call.
# Chunk above that ceiling so a 200KB email thread doesn't blow up.
COMPREHEND_MAX_BYTES = 100_000


@dataclass
class RedactionResult:
    """The output of redact(). Always returned even when redaction is
    disabled / skipped, so callers can branch on ``.text`` uniformly."""

    text: str
    char_count: int
    # {"EMAIL": 3, "PHONE": 1} - empty when nothing was redacted.
    summary: Dict[str, int] = field(default_factory=dict)
    # Why we didn't redact, if applicable. One of:
    #   ""                    (we did redact)
    #   "org_disabled"        (tfvar pii_redaction_enabled = false)
    #   "workspace_none"      (privacy_mode = NONE)
    #   "empty_text"          (nothing to do)
    #   "comprehend_error"    (we tried, AWS hiccupped; raw text returned)
    skipped_reason: str = ""


def replacement_for(entity_type: str) -> str:
    """Format helper. ``EMAIL`` -> ``[REDACTED:EMAIL]``. Public so
    tests + cost-tracking sums can reference the exact format."""
    return f"[REDACTED:{entity_type}]"


def read_workspace_mode(table, workspace_id: str) -> str:
    """Read the workspace's ``privacy_mode`` from its METADATA row.

    Returns ``"STRICT"`` for any workspace whose row lacks the
    attribute (defensible default - workspaces created before
    plan/ chunk 03 shipped do, and the safe default matches the
    new-create behavior).

    Lives here, alongside ``redact()``, so ingest Lambdas don't have
    to import the workspace-management package. Coupling pii.py to
    the workspace-table schema is acceptable for v1; if a second
    feature needs the same lookup we'll factor out to
    ``shared/workspaces.py`` (plan/ chunk 05 spec).
    """
    try:
        resp = table.get_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"}
        )
    except ClientError as e:
        # Non-fatal: a transient DDB read shouldn't break ingest.
        # Default to STRICT so we err on the safe side.
        logger.warning(
            f"[pii] read_workspace_mode failed for {workspace_id}: {e}"
        )
        return MODE_STRICT
    item = resp.get("Item") or {}
    return item.get("privacy_mode", MODE_STRICT)


def _split_for_comprehend(text: str) -> List[str]:
    """Split text into <100KB UTF-8 chunks at paragraph boundaries.

    Comprehend's DetectPiiEntities caps inputs at ~100KB. We split on
    double-newline (paragraph) boundaries so we don't bisect a sentence
    or a PII token. Single chunks larger than the cap are split on
    single newline; truly-pathological single-line giant inputs are
    sliced at the byte boundary as a last resort.
    """
    raw = text.encode("utf-8")
    if len(raw) <= COMPREHEND_MAX_BYTES:
        return [text]

    chunks: List[str] = []
    paragraphs = text.split("\n\n")
    buf = ""
    for para in paragraphs:
        candidate = (buf + ("\n\n" if buf else "") + para)
        if len(candidate.encode("utf-8")) <= COMPREHEND_MAX_BYTES:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        # The single paragraph is itself too big; fall back to lines.
        if len(para.encode("utf-8")) <= COMPREHEND_MAX_BYTES:
            chunks.append(para)
            continue
        line_buf = ""
        for line in para.split("\n"):
            cand = line_buf + ("\n" if line_buf else "") + line
            if len(cand.encode("utf-8")) <= COMPREHEND_MAX_BYTES:
                line_buf = cand
            else:
                if line_buf:
                    chunks.append(line_buf)
                # Truly-pathological single-line giant - byte slice.
                line_bytes = line.encode("utf-8")
                while len(line_bytes) > COMPREHEND_MAX_BYTES:
                    chunks.append(
                        line_bytes[:COMPREHEND_MAX_BYTES].decode(
                            "utf-8", errors="ignore"
                        )
                    )
                    line_bytes = line_bytes[COMPREHEND_MAX_BYTES:]
                line_buf = line_bytes.decode("utf-8", errors="ignore")
        if line_buf:
            buf = line_buf
    if buf:
        chunks.append(buf)
    return chunks


def _redact_chunk(
    chunk: str, language_code: str
) -> "tuple[str, Dict[str, int]]":
    """Call Comprehend on one <100KB chunk and apply replacements
    back-to-front so offsets stay valid."""
    client = _get_client()
    response = client.detect_pii_entities(
        Text=chunk, LanguageCode=language_code
    )
    entities = response.get("Entities", []) or []
    if not entities:
        return chunk, {}

    # Apply replacements back-to-front so each replacement's offsets
    # remain valid relative to the still-unprocessed prefix.
    entities_sorted = sorted(
        entities, key=lambda e: e.get("BeginOffset", 0), reverse=True
    )
    summary: Dict[str, int] = {}
    out = chunk
    for ent in entities_sorted:
        begin = ent.get("BeginOffset")
        end = ent.get("EndOffset")
        ent_type = ent.get("Type", "PII")
        if begin is None or end is None:
            continue
        out = out[:begin] + replacement_for(ent_type) + out[end:]
        summary[ent_type] = summary.get(ent_type, 0) + 1
    return out, summary


def redact(
    text: str,
    *,
    mode: str = MODE_NONE,
    org_enabled: bool = True,
    language_code: str = "en",
) -> RedactionResult:
    """Detect and replace PII in ``text``.

    Effective behavior: redact iff ``org_enabled AND mode == STRICT``.
    Anything else returns the raw text with the appropriate
    ``skipped_reason``. Never raises; on Comprehend ClientError the
    raw text is returned with ``skipped_reason="comprehend_error"``
    so the ingest pipeline doesn't break on a transient AWS hiccup.

    The cost of the call is ``char_count * COMPREHEND_PRICE_PER_CHAR``
    where COMPREHEND_PRICE_PER_CHAR comes from MODEL_PRICING (chunk 02).
    The caller is responsible for emitting the audit row that records
    char_count + summary; this helper does not write to DynamoDB.

    Args:
        text: Raw text to scan.
        mode: ``"STRICT"`` or ``"NONE"`` from the workspace setting.
        org_enabled: True iff the tfvar ``pii_redaction_enabled`` is
            on (threaded by the caller from the env var).
        language_code: ISO 639-1; defaults to English. Comprehend
            supports ``en``, ``es``, ``fr``, ``de``, ``it``, ``pt``,
            ``ar``, ``hi``, ``ja``, ``ko``, ``zh``, ``zh-TW``.

    Returns:
        RedactionResult.
    """
    char_count = len(text) if text else 0

    if not text or not text.strip():
        return RedactionResult(
            text=text or "",
            char_count=char_count,
            skipped_reason="empty_text",
        )

    if not org_enabled:
        return RedactionResult(
            text=text,
            char_count=char_count,
            skipped_reason="org_disabled",
        )

    if mode != MODE_STRICT:
        return RedactionResult(
            text=text,
            char_count=char_count,
            skipped_reason="workspace_none",
        )

    try:
        chunks = _split_for_comprehend(text)
        merged_text_parts: List[str] = []
        merged_summary: Dict[str, int] = {}
        for chunk in chunks:
            redacted_chunk, chunk_summary = _redact_chunk(
                chunk, language_code
            )
            merged_text_parts.append(redacted_chunk)
            for k, v in chunk_summary.items():
                merged_summary[k] = merged_summary.get(k, 0) + v
        # If we split on paragraph boundaries, the original "\n\n"
        # separators were absorbed into chunks - rejoin in the same
        # shape we split on. _split_for_comprehend keeps the
        # paragraph contents but drops the joiner; restore it.
        # (Single-chunk inputs round-trip identity-equal.)
        joined = (
            "\n\n".join(merged_text_parts)
            if len(chunks) > 1
            else merged_text_parts[0]
        )
        logger.info(
            f"[pii] redacted {merged_summary} ({char_count} chars, "
            f"{len(chunks)} chunk(s))"
        )
        return RedactionResult(
            text=joined,
            char_count=char_count,
            summary=merged_summary,
        )
    except ClientError as e:
        # Non-fatal: never break ingest because Comprehend hiccupped.
        # Caller's audit row records skipped_reason for visibility.
        logger.warning(f"[pii] Comprehend ClientError: {e}")
        return RedactionResult(
            text=text,
            char_count=char_count,
            skipped_reason="comprehend_error",
        )
    except Exception as e:
        # Unexpected failure mode (parsing, encoding). Same posture.
        logger.warning(f"[pii] Unexpected error: {e}")
        return RedactionResult(
            text=text,
            char_count=char_count,
            skipped_reason="comprehend_error",
        )
