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
    ("us.anthropic.",   "converse"),
    ("anthropic.",      "converse"),
    ("us.meta.",        "converse"),
    ("meta.",           "converse"),
    ("us.amazon.nova-", "converse"),
    ("amazon.nova-",    "converse"),
    ("openai.gpt-oss-", "converse"),
    # Bedrock Mantle non-OpenAI families. All hit the same Responses-
    # API endpoint at bedrock-mantle.{region}.api.aws/openai/v1 --
    # the API key auth + the openai-SDK transport are identical to
    # the gpt-5.* path. Added 2026-06-06 to exercise the Mantle
    # dispatch end-to-end against qwen3-coder-480b on the openai
    # workspace while waiting on AWS Sales to clear gpt-5.5
    # entitlement. If qwen flows clean, gpt-5.5 will too -- only
    # the model ID changes at the wire.
    ("qwen.qwen3-coder-", "responses"),
    ("qwen.qwen3-vl-",   "responses"),
    ("deepseek.",        "responses"),
    ("mistral.devstral-", "responses"),
    ("mistral.mistral-large-", "responses"),
    ("moonshotai.",      "responses"),
    ("zai.glm-",         "responses"),
    ("minimax.",         "responses"),
]


def _resolve_backend(model_id: str) -> str:
    if not model_id:
        raise UnsupportedModelError(
            "model_id is required; cannot dispatch on empty string."
        )
    # Sort by length so longest prefix wins regardless of list order.
    for prefix, name in sorted(_DISPATCH, key=lambda t: -len(t[0])):
        if model_id.startswith(prefix):
            return name
    raise UnsupportedModelError(
        f"No backend registered for model_id={model_id!r}. "
        f"Add a row to _DISPATCH in shared.model_client."
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
