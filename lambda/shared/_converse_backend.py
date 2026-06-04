"""shared._converse_backend — Bedrock Converse API backend.

Implements both `invoke()` (bedrock-runtime.converse) and `invoke_stream()`
(bedrock-runtime.converse_stream). Covers Claude, Llama, Nova, gpt-oss.

The unified Request shape is Converse-flavored on input (messages already
carry Converse content blocks), so request translation is near pass-through.
Response translation extracts text, usage, stop_reason, and any toolUse
blocks into the unified Response.
"""

import json
import logging
from typing import Any, Iterator

import boto3
from botocore.config import Config

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

# Matches the timeout pattern process-repo-summary uses for long-running
# summarizations (some prompts take 2-3 minutes).
_BEDROCK_CONFIG = Config(
    read_timeout=300,
    connect_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", config=_BEDROCK_CONFIG)
    return _client


def _build_converse_kwargs(req: Request) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "modelId": req.model_id,
        "messages": req.messages,
        "inferenceConfig": {
            "maxTokens": req.max_tokens,
            "temperature": req.temperature,
        },
    }
    if req.top_p is not None:
        kwargs["inferenceConfig"]["topP"] = req.top_p
    if req.stop_sequences:
        kwargs["inferenceConfig"]["stopSequences"] = req.stop_sequences
    if req.system:
        system = req.system if isinstance(req.system, list) else [req.system]
        kwargs["system"] = [{"text": s} for s in system]
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


def invoke_stream(req: Request) -> Iterator[StreamEvent]:
    """Stream Converse events as unified StreamEvent objects.

    Converse only emits `contentBlockStart` for tool-use blocks; text
    blocks just start emitting `contentBlockDelta.text`. The accumulator
    only needs to track the currently-open tool block (if any) since
    text deltas are stateless. `contentBlockStop` flushes the open tool
    block, if any.
    """
    client = _get_client()
    kwargs = _build_converse_kwargs(req)
    raw = client.converse_stream(**kwargs)
    stream = raw["stream"]

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
