"""shared._converse_backend — Bedrock Converse API backend.

Implements both `invoke()` (bedrock-runtime.converse) and `invoke_stream()`
(bedrock-runtime.converse_stream). Covers Claude, Llama, Nova, gpt-oss.

The unified Request shape is Converse-flavored on input (messages already
carry Converse content blocks), so request translation is near pass-through.
Response translation extracts text, usage, stop_reason, and any toolUse
blocks into the unified Response.

fable-scale chunk 05: retry semantics are app-side, not botocore-side. The
boto client is configured with `max_attempts=1` so this module's own loop
owns backoff, jitter, and observability. Rationale: layering botocore
retries under an app-side loop makes total-attempt count confusing (3x3=9)
and hides which layer absorbed a throttle. See `invoke_with_retry` and
`open_stream_with_retry` below.
"""

import json
import logging
import os
import random
import time
from typing import Any, Callable, Iterator, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .model_client_types import (
    Metadata,
    Request,
    Response,
    Stop,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolUseArgumentsDelta,
    ToolUseStart,
    ToolUseStop,
    TranslationError,
    Usage,
)

logger = logging.getLogger(__name__)

# fable-scale chunk 05: app-side retries — botocore does exactly one attempt.
_BEDROCK_CONFIG = Config(
    read_timeout=300,
    connect_timeout=10,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Bedrock error codes we retry with backoff. Everything else bubbles.
# ModelStreamErrorException covers mid-init stream failures that Bedrock
# treats as transient. ModelErrorException is deliberately excluded — it's
# usually a client-side problem (bad tool schema) that retrying won't fix.
_RETRYABLE_ERROR_CODES = frozenset((
    "ThrottlingException",
    "ModelStreamErrorException",
    "ServiceUnavailableException",
))


def _is_retryable(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code", "") in _RETRYABLE_ERROR_CODES


def _next_backoff_ms(attempt: int, base_ms: int = 500, cap_ms: int = 8000) -> int:
    """Full-jitter exponential backoff. AWS Architecture Blog pattern:
    delay = random(0, min(cap, base * 2^attempt)).
    """
    ceiling = min(cap_ms, base_ms * (2 ** (attempt - 1)))
    return random.randint(0, max(1, ceiling))


def _max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("BEDROCK_MAX_ATTEMPTS", "6")))
    except ValueError:
        return 6

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", config=_BEDROCK_CONFIG)
    return _client


# Anthropic's Claude 5.x family (and newer) deprecated caller-controlled
# sampling on Bedrock: passing temperature or topP returns a
# ValidationException ("temperature is deprecated for this model") on BOTH
# invoke_model and Converse -- it is not silently ignored, the call fails. Omit
# those fields for such models; they run at the model's fixed server-side
# default. Mirrors the GPT-5 temperature lock handled in _responses_backend.
# Matches the configured id (us.anthropic.claude-sonnet-5-5), the bare id, and
# the global./us. inference-profile ARN forms. Extend as new families ship.
_NO_CALLER_SAMPLING_SUBSTR = (
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-opus-5",
)


def _supports_caller_sampling(model_id: str) -> bool:
    mid = model_id or ""
    return not any(s in mid for s in _NO_CALLER_SAMPLING_SUBSTR)


def _build_converse_kwargs(req: Request) -> dict[str, Any]:
    inference: dict[str, Any] = {"maxTokens": req.max_tokens}
    if _supports_caller_sampling(req.model_id):
        inference["temperature"] = req.temperature
        if req.top_p is not None:
            inference["topP"] = req.top_p
    elif req.temperature != 0.0 or req.top_p is not None:
        logger.warning(
            "model %s dropped caller sampling; omitting temperature=%r "
            "top_p=%r (model runs at its server-side default).",
            req.model_id, req.temperature, req.top_p,
        )
    if req.stop_sequences:
        inference["stopSequences"] = req.stop_sequences
    kwargs: dict[str, Any] = {
        "modelId": req.model_id,
        "messages": req.messages,
        "inferenceConfig": inference,
    }
    if req.system:
        system = req.system if isinstance(req.system, list) else [req.system]
        system_blocks: list[dict] = [{"text": s} for s in system]
        # fable-scale chunk 10: append a Bedrock Converse cachePoint after
        # the system prompt when caching is enabled globally. System prompts
        # are the largest stable input in a chat turn — caching yields real
        # input-token savings. Chat opts in via BEDROCK_PROMPT_CACHE_ENABLED;
        # the callers that want caching build a system prompt worth caching.
        if os.environ.get("BEDROCK_PROMPT_CACHE_ENABLED", "false").lower() == "true":
            system_blocks.append({"cachePoint": {"type": "default"}})
        kwargs["system"] = system_blocks
    if req.tools:
        kwargs["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": {"json": t.input_schema},
                    }
                }
                for t in req.tools
            ]
        }
    return kwargs


def _extract_text(content_blocks: list[dict]) -> str:
    for block in content_blocks:
        if "text" in block:
            return block["text"]
    return ""


def _extract_tool_calls(content_blocks: list[dict]) -> list[ToolCall] | None:
    calls = []
    for block in content_blocks:
        tu = block.get("toolUse")
        if tu:
            calls.append(
                ToolCall(
                    tool_use_id=tu["toolUseId"],
                    name=tu["name"],
                    input=tu.get("input", {}) or {},
                )
            )
    return calls or None


def invoke_with_retry(req: Request) -> Response:
    """fable-scale chunk 05: `invoke` wrapped in app-side backoff+jitter.

    Retries `ThrottlingException`, `ModelStreamErrorException`,
    `ServiceUnavailableException` up to `BEDROCK_MAX_ATTEMPTS` (default 6)
    with full-jitter exponential backoff. Non-retryable errors bubble
    immediately.
    """
    max_attempts = _max_attempts()
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return invoke(req)
        except ClientError as e:
            last_exc = e
            if not _is_retryable(e) or attempt >= max_attempts:
                raise
            delay_ms = _next_backoff_ms(attempt)
            logger.warning(
                "bedrock invoke retry: attempt=%d/%d delay_ms=%d code=%s model=%s",
                attempt, max_attempts, delay_ms,
                e.response.get("Error", {}).get("Code", ""),
                req.model_id,
            )
            time.sleep(delay_ms / 1000.0)
    # Defensive: the loop always raises on last_attempt above.
    assert last_exc is not None
    raise last_exc


def open_stream_with_retry(
    req: Request,
    on_retry: Optional[Callable[[int, int], None]] = None,
) -> Any:
    """Open a Converse stream; retry the initial API call on transient errors.

    Returns the boto EventStream object ready to iterate. Mid-stream errors
    are NOT retried — they carry partial state that can't safely be replayed.

    `on_retry(attempt, delay_ms)` is invoked before each sleep so streaming
    callers (chat) can emit a `busy_retrying` event through the SSE surface
    while we wait. Silent if not provided.
    """
    max_attempts = _max_attempts()
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            client = _get_client()
            kwargs = _build_converse_kwargs(req)
            raw = client.converse_stream(**kwargs)
            return raw["stream"]
        except ClientError as e:
            last_exc = e
            if not _is_retryable(e) or attempt >= max_attempts:
                raise
            delay_ms = _next_backoff_ms(attempt)
            logger.warning(
                "bedrock stream-init retry: attempt=%d/%d delay_ms=%d code=%s model=%s",
                attempt, max_attempts, delay_ms,
                e.response.get("Error", {}).get("Code", ""),
                req.model_id,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, delay_ms)
                except Exception:
                    logger.exception("on_retry callback raised; ignoring")
            time.sleep(delay_ms / 1000.0)
    assert last_exc is not None
    raise last_exc


def invoke(req: Request) -> Response:
    client = _get_client()
    kwargs = _build_converse_kwargs(req)
    raw = client.converse(**kwargs)

    output_message = raw.get("output", {}).get("message", {})
    content_blocks = output_message.get("content", [])
    usage_raw = raw.get("usage", {})
    stop_reason_raw = raw.get("stopReason", "end_turn")

    try:
        stop_reason = StopReason(stop_reason_raw)
    except ValueError as e:
        raise TranslationError(
            f"converse returned unknown stopReason={stop_reason_raw!r}; "
            f"add to StopReason enum or document the new value."
        ) from e

    return Response(
        text=_extract_text(content_blocks),
        content_blocks=content_blocks,
        usage=Usage(
            input_tokens=usage_raw.get("inputTokens", 0),
            output_tokens=usage_raw.get("outputTokens", 0),
        ),
        stop_reason=stop_reason,
        tool_calls=_extract_tool_calls(content_blocks),
        raw=raw,
    )


def invoke_stream(
    req: Request,
    on_retry: Optional[Callable[[int, int], None]] = None,
) -> Iterator[StreamEvent]:
    """Stream Converse events as unified StreamEvent objects.

    Converse only emits `contentBlockStart` for tool-use blocks; text
    blocks just start emitting `contentBlockDelta.text`. The accumulator
    only needs to track the currently-open tool block (if any) since
    text deltas are stateless. `contentBlockStop` flushes the open tool
    block, if any.

    fable-scale chunk 05: stream-init retries via `open_stream_with_retry`.
    `on_retry(attempt, delay_ms)` is called before each backoff sleep so
    the caller can emit a `busy_retrying` event through the SSE surface.
    """
    stream = open_stream_with_retry(req, on_retry=on_retry)

    current_tool_use_id: str | None = None
    current_tool_args_buffer: str = ""

    for event in stream:
        if "messageStart" in event:
            continue

        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            tu = start.get("toolUse")
            if tu:
                current_tool_use_id = tu.get("toolUseId", "")
                current_tool_args_buffer = ""
                yield ToolUseStart(
                    tool_use_id=current_tool_use_id,
                    name=tu.get("name", ""),
                )
            continue

        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                yield TextDelta(text=delta["text"])
            elif "toolUse" in delta and current_tool_use_id:
                partial = delta["toolUse"].get("input", "")
                if partial:
                    current_tool_args_buffer += partial
                    yield ToolUseArgumentsDelta(
                        tool_use_id=current_tool_use_id,
                        arguments_partial=partial,
                    )
            continue

        if "contentBlockStop" in event:
            if current_tool_use_id is not None:
                try:
                    args = json.loads(current_tool_args_buffer or "{}")
                except json.JSONDecodeError as e:
                    raise TranslationError(
                        f"Converse stream tool args for {current_tool_use_id!r} "
                        f"did not parse as JSON: {e}; buffered="
                        f"{current_tool_args_buffer!r}"
                    ) from e
                yield ToolUseStop(
                    tool_use_id=current_tool_use_id,
                    arguments=args,
                )
                current_tool_use_id = None
                current_tool_args_buffer = ""
            continue

        if "messageStop" in event:
            stop_reason_raw = event["messageStop"].get("stopReason", "end_turn")
            try:
                stop_reason = StopReason(stop_reason_raw)
            except ValueError as e:
                raise TranslationError(
                    f"converse stream messageStop unknown stopReason="
                    f"{stop_reason_raw!r}; add to StopReason enum."
                ) from e
            yield Stop(stop_reason=stop_reason)
            continue

        if "metadata" in event:
            usage_raw = event["metadata"].get("usage", {})
            yield Metadata(
                usage=Usage(
                    input_tokens=usage_raw.get("inputTokens", 0),
                    output_tokens=usage_raw.get("outputTokens", 0),
                )
            )
            continue

        # Unknown event type: log and skip rather than raising. Bedrock
        # may add new event types; we don't want a forward-compatible
        # event to break the stream. But log it so we notice.
        logger.warning(
            "Converse stream: ignoring unknown event keys=%s",
            list(event.keys()),
        )
