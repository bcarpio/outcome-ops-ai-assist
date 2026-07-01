"""shared.model_client — single dispatch point for Bedrock LLM completions.

Routes by model-ID prefix to one of two backends:

- Converse backend (bedrock-runtime.converse / converse_stream) for
  anthropic.*, meta.*, amazon.nova.*, openai.gpt-oss-*.
- Responses backend (bedrock-mantle SigV4-signed HTTPS) for
  openai.gpt-5-*, openai.codex-*.

Per ADR-035, the dispatch table lives in code (not config) and raises
loudly on unknown prefixes -- silent fallback would invalidate the
structural-attribution claims of ADR-031 / ADR-033.

Public surface:
    invoke(*, model_id, messages, system=None, max_tokens=4096,
           temperature=0.0, top_p=None, stop_sequences=None, tools=None)
        -> Response

    invoke_stream(*, ...same kwargs)
        -> Iterator[StreamEvent]
"""

import os
from typing import Iterator, Optional

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


def invoke(
    *,
    model_id: str,
    messages: list[dict],
    system: Optional[str | list[str]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    top_p: Optional[float] = None,
    stop_sequences: Optional[list[str]] = None,
    tools: Optional[list[ToolSpec]] = None,
) -> Response:
    req = Request(
        model_id=model_id,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        tools=tools,
    )
    name = _resolve_backend(model_id)
    if name == "converse":
        from . import _converse_backend
        return _converse_backend.invoke(req)
    if name == "responses":
        from . import _responses_backend
        return _responses_backend.invoke(req)
    raise BackendConfigError(f"Backend {name!r} resolved but not wired.")


def invoke_stream(
    *,
    model_id: str,
    messages: list[dict],
    system: Optional[str | list[str]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    top_p: Optional[float] = None,
    stop_sequences: Optional[list[str]] = None,
    tools: Optional[list[ToolSpec]] = None,
) -> Iterator[StreamEvent]:
    req = Request(
        model_id=model_id,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        tools=tools,
    )
    name = _resolve_backend(model_id)
    if name == "converse":
        from . import _converse_backend
        yield from _converse_backend.invoke_stream(req)
        return
    if name == "responses":
        from . import _responses_backend
        yield from _responses_backend.invoke_stream(req)
        return
    raise BackendConfigError(f"Backend {name!r} resolved but not wired.")
