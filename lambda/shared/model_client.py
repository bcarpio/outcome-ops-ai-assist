"""shared.model_client — single dispatch point for Bedrock LLM completions.

Routes by model-ID prefix to one of two backends:

- Converse backend (bedrock-runtime.converse / converse_stream) for
  anthropic.*, meta.*, amazon.nova.*, openai.gpt-oss-*.
- Responses backend (bedrock-mantle SigV4-signed HTTPS) for
  openai.gpt-5-*, openai.codex-*.

Per ADR-035, the dispatch table lives in code (not config) and raises
loudly on unknown prefixes -- silent fallback would invalidate the
structural-attribution claims of ADR-031 / ADR-033.

ADR-016 v1.2 (Bedrock application inference profiles for cost
attribution) adds a per-workload dispatch overlay: callers can supply
``workload`` + ``tier`` instead of ``model_id`` and this module resolves
the correct profile ARN from an env var of the form
``BEDROCK_<WORKLOAD>_<TIER>_PROFILE_ARN``. The resolved ARN is then
passed as the Bedrock ``modelId`` -- Converse and InvokeModel both
accept a profile ARN in place of a model ID -- so Bedrock bills against
the tagged profile and Cost Explorer can split spend by workload.

If the env var isn't wired yet (chunk 04 rollout in flight), the module
falls back to the legacy ``BEDROCK_<TIER>_MODEL_ID`` env var with a
WARN log so any post-rollout attribution gap is diagnosable in
CloudWatch.

Public surface:
    invoke(*, model_id=None, workload=None, tier=None, messages, ...)
        -> Response

    invoke_stream(*, ...same kwargs)
        -> Iterator[StreamEvent]

    resolve_profile_arn(workload, tier) -> Optional[str]
        -- exposed for retrieval.rerank + shared.embed to share the
        same lookup convention.
"""

import logging
import os
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

from .model_client_types import (
    BackendConfigError,
    ModelClientError,
    Request,
    Response,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    ToolUseArgumentsDelta,
    ToolUseStart,
    ToolUseStop,
    Stop,
    Metadata,
    TranslationError,
    UnsupportedModelError,
    Usage,
)

__all__ = [
    "invoke",
    "invoke_stream",
    "resolve_profile_arn",
    "Request",
    "Response",
    "StopReason",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolSpec",
    "ToolUseArgumentsDelta",
    "ToolUseStart",
    "ToolUseStop",
    "Stop",
    "Metadata",
    "Usage",
    "ModelClientError",
    "UnsupportedModelError",
    "BackendConfigError",
    "TranslationError",
]


# Dispatch table: (prefix, backend_name). Longest prefix wins.
# Adding a new family is a code change plus tests, intentionally --
# silent fallback for unknown prefixes is the failure mode ADR-035
# specifically protects against.
_DISPATCH: list[tuple[str, str]] = [
    ("us.openai.gpt-5", "responses"),
    ("openai.gpt-5",    "responses"),
    ("us.openai.codex-", "responses"),
    ("openai.codex-",   "responses"),
    ("us.xai.grok-",    "responses"),
    ("xai.grok-",       "responses"),
    ("us.anthropic.",   "converse"),
    ("anthropic.",      "converse"),
    ("us.meta.",        "converse"),
    ("meta.",           "converse"),
    ("us.amazon.nova-", "converse"),
    ("amazon.nova-",    "converse"),
    ("openai.gpt-oss-", "converse"),
]

# Per-deploy fallback for prefixes not in _DISPATCH. The Mantle catalog
# is large + churns -- qwen/deepseek/mistral/moonshotai/zai/minimax
# etc. all live behind the same Responses-API endpoint as gpt-5.*, so
# the explicit-prefix-per-family approach in _DISPATCH became dead
# weight as soon as we started exercising Mantle with non-OpenAI
# models. A per-deploy default lets the openai workspace opt into
# "any unknown family rides Mantle Responses" with one env var,
# while prd's "no silent fallback" safety (ADR-035 §rationale) stays
# unchanged because prd leaves the var unset and unknown families
# still raise.
_VALID_BACKENDS = ("converse", "responses")


def _default_backend() -> Optional[str]:
    """Resolve BEDROCK_DEFAULT_BACKEND from env. Returns None when unset
    so _resolve_backend keeps raising on unknown prefixes."""
    value = (os.environ.get("BEDROCK_DEFAULT_BACKEND") or "").strip()
    if not value:
        return None
    if value not in _VALID_BACKENDS:
        raise BackendConfigError(
            f"BEDROCK_DEFAULT_BACKEND={value!r} is not one of {_VALID_BACKENDS}",
        )
    return value


def _resolve_backend(model_id: str) -> str:
    if not model_id:
        raise UnsupportedModelError(
            "model_id is required; cannot dispatch on empty string."
        )
    # ADR-016 v1.2: application inference profile ARNs route to converse
    # because every profile we wrap today is an Anthropic Claude model
    # (haiku/sonnet). If we ever wrap a Mantle/Responses model behind a
    # profile ARN, extend this to look at the underlying model via
    # bedrock.get-inference-profile (or add a caller-side backend hint).
    # ARN shape: arn:aws:bedrock:REGION:ACCT:inference-profile/ID -- note
    # the segment separator before "inference-profile" is ":", not "/".
    if model_id.startswith("arn:aws:bedrock:") and ":inference-profile/" in model_id:
        return "converse"
    # Sort by length so longest prefix wins regardless of list order.
    for prefix, name in sorted(_DISPATCH, key=lambda t: -len(t[0])):
        if model_id.startswith(prefix):
            return name
    fallback = _default_backend()
    if fallback is not None:
        return fallback
    raise UnsupportedModelError(
        f"No backend registered for model_id={model_id!r}. "
        f"Add a row to _DISPATCH in shared.model_client, or set "
        f"BEDROCK_DEFAULT_BACKEND on the deploy to opt into a "
        f"per-deploy fallback (typically 'responses' on Mantle-only "
        f"deploys like the openai workspace)."
    )


# ---------------------------------------------------------------------------
# ADR-016 v1.2 -- workload-aware profile ARN resolution
# ---------------------------------------------------------------------------

_VALID_WORKLOADS = frozenset({"chat", "code-maps", "code-gen", "pr-analyzer"})

# Tiers cover every model class the caller might request. Only Claude
# tiers dispatch through this module (embed + rerank live in
# shared.embed / shared.retrieval respectively). Kept as a single
# vocabulary so misspellings raise instead of silently falling back.
_VALID_TIERS = frozenset({"advanced", "basic", "titan-embed", "cohere-rerank"})

# Tier -> legacy env var name used by callers today. When a workload
# is passed but the profile env var isn't wired yet, we fall back to
# this so the Lambda keeps working (with a WARN log) rather than
# hard-crashing during chunk 04 rollout.
_LEGACY_MODEL_ID_ENV = {
    "advanced": "BEDROCK_ADVANCED_MODEL_ID",
    "basic": "BEDROCK_BASIC_MODEL_ID",
    "titan-embed": "BEDROCK_EMBEDDING_MODEL_ID",
    "cohere-rerank": "BEDROCK_RERANK_MODEL_ID",
}


def _profile_env_var_name(workload: str, tier: str) -> str:
    """Build the env var name for a (workload, tier) pair.

    Vocabulary matches terraform/bedrock-inference-profiles.tf:
        BEDROCK_CHAT_ADVANCED_PROFILE_ARN
        BEDROCK_CODE_MAPS_BASIC_PROFILE_ARN
        BEDROCK_CHAT_TITAN_EMBED_PROFILE_ARN
        BEDROCK_CHAT_COHERE_RERANK_PROFILE_ARN
    """
    return (
        f"BEDROCK_"
        f"{workload.upper().replace('-', '_')}_"
        f"{tier.upper().replace('-', '_')}_PROFILE_ARN"
    )


def resolve_profile_arn(workload: str, tier: str) -> Optional[str]:
    """Return the profile ARN env var value for (workload, tier), or None.

    ``None`` means the env var isn't set -- caller should fall back to
    the legacy path and log a WARN. Exposed for shared.retrieval.rerank
    and shared.embed to share the same lookup shape.

    Raises ValueError on unknown workload/tier -- prevents silent
    misattribution from typos.
    """
    if workload not in _VALID_WORKLOADS:
        raise ValueError(
            f"Unknown workload {workload!r}. Valid: {sorted(_VALID_WORKLOADS)}"
        )
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"Unknown tier {tier!r}. Valid: {sorted(_VALID_TIERS)}"
        )
    return os.environ.get(_profile_env_var_name(workload, tier)) or None


def _resolve_workload_model_id(workload: str, tier: str) -> str:
    """Resolve (workload, tier) to a Bedrock modelId.

    Preferred: the ADR-016 v1.2 profile ARN from
    ``BEDROCK_<WORKLOAD>_<TIER>_PROFILE_ARN``.

    Fallback: the legacy per-tier model id env var
    (``BEDROCK_ADVANCED_MODEL_ID`` etc.) with a WARN log so any
    unwired Lambda in Cost Explorer surfaces as "untagged Bedrock
    spend" that can be traced back to which caller/workload.
    """
    profile_arn = resolve_profile_arn(workload, tier)
    if profile_arn:
        return profile_arn

    legacy_env = _LEGACY_MODEL_ID_ENV.get(tier)
    legacy_value = os.environ.get(legacy_env) if legacy_env else None
    if not legacy_value:
        raise BackendConfigError(
            f"workload={workload!r} tier={tier!r}: neither "
            f"{_profile_env_var_name(workload, tier)} nor {legacy_env} "
            f"is set. Wire the profile ARN env var in this Lambda's "
            f"terraform module (chunk 04) or set the legacy fallback."
        )
    logger.warning(
        "workload=%r tier=%r falling back to untagged model_id=%r "
        "(env var %s unset). Cost Explorer attribution disabled for "
        "this call -- check terraform env-var wiring.",
        workload, tier, legacy_value, _profile_env_var_name(workload, tier),
    )
    return legacy_value


def _resolve_model_id(
    model_id: Optional[str],
    workload: Optional[str],
    tier: Optional[str],
) -> str:
    """Reconcile the two ways a caller can specify the model.

    Precedence: workload+tier > model_id. Callers that pass both get
    the workload-scoped profile ARN (chunk 04 wiring), keeping the
    legacy ``model_id`` value effectively as a documentation crumb
    for reviewers. Callers that pass neither raise -- silent defaults
    would collapse attribution.
    """
    if workload or tier:
        if not (workload and tier):
            raise ValueError(
                "workload and tier must be provided together "
                f"(got workload={workload!r}, tier={tier!r})"
            )
        return _resolve_workload_model_id(workload, tier)
    if not model_id:
        raise ValueError(
            "Either model_id, or (workload, tier), is required."
        )
    return model_id


def invoke(
    *,
    model_id: Optional[str] = None,
    workload: Optional[str] = None,
    tier: Optional[str] = None,
    messages: list[dict],
    system: Optional[str | list[str]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    top_p: Optional[float] = None,
    stop_sequences: Optional[list[str]] = None,
    tools: Optional[list[ToolSpec]] = None,
) -> Response:
    resolved_id = _resolve_model_id(model_id, workload, tier)
    req = Request(
        model_id=resolved_id,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        tools=tools,
    )
    name = _resolve_backend(resolved_id)
    if name == "converse":
        from . import _converse_backend
        return _converse_backend.invoke(req)
    if name == "responses":
        from . import _responses_backend
        return _responses_backend.invoke(req)
    raise BackendConfigError(f"Backend {name!r} resolved but not wired.")


def invoke_stream(
    *,
    model_id: Optional[str] = None,
    workload: Optional[str] = None,
    tier: Optional[str] = None,
    messages: list[dict],
    system: Optional[str | list[str]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    top_p: Optional[float] = None,
    stop_sequences: Optional[list[str]] = None,
    tools: Optional[list[ToolSpec]] = None,
) -> Iterator[StreamEvent]:
    resolved_id = _resolve_model_id(model_id, workload, tier)
    req = Request(
        model_id=resolved_id,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        tools=tools,
    )
    name = _resolve_backend(resolved_id)
    if name == "converse":
        from . import _converse_backend
        yield from _converse_backend.invoke_stream(req)
        return
    if name == "responses":
        from . import _responses_backend
        yield from _responses_backend.invoke_stream(req)
        return
    raise BackendConfigError(f"Backend {name!r} resolved but not wired.")
