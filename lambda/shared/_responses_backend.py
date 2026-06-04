"""shared._responses_backend — Bedrock Mantle Responses API backend.

Uses the official `openai` Python SDK pointed at the Bedrock Mantle
endpoint via base_url override. Auth is a long-lived Bedrock API key
loaded from SSM at module init (per ADR-035 §4).

Chunk 03 (skeleton) handles text-only requests + text-only streaming
events + the basic stop_reason map. Chunks 04/05/06 land the full
translation surface (system prompt, multi-turn, images, tools).
"""

import json
import logging
import os
from typing import Iterator

import boto3
from openai import OpenAI

from .model_client_types import (
    BackendConfigError,
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

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = _load_api_key()
        region = os.environ.get("AWS_REGION") or os.environ.get("THIS_REGION")
        if not region:
            raise BackendConfigError(
                "Responses backend needs AWS_REGION (or THIS_REGION) set; "
                "Mantle endpoint is region-specific."
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
        )
    return _client


def _load_api_key() -> str:
    param_name = os.environ.get("BEDROCK_MANTLE_API_KEY_PARAM")
    if not param_name:
        raise BackendConfigError(
            "BEDROCK_MANTLE_API_KEY_PARAM env var not set; cannot reach "
            "Bedrock Mantle. Either point a tfvars to a Converse-backed "
            "model or provision the key per "
            "docs/runbooks/bedrock-mantle-api-key.md."
        )
    # The SSM param lives in the same region as the Mantle endpoint (each
    # env writes its own params; we don't replicate the bedrock key
    # cross-region). Construct the SSM client explicitly so boto3's
    # default-region resolution doesn't accidentally point at us-east-1.
    region = os.environ.get("AWS_REGION") or os.environ.get("THIS_REGION")
    ssm = boto3.client("ssm", region_name=region) if region else boto3.client("ssm")
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    value = resp["Parameter"]["Value"]
    if not value or value == "PLACEHOLDER_CONFIGURE_ME":
        raise BackendConfigError(
            f"Mantle API key at {param_name} is unconfigured. "
            f"Either provision the key (see "
            f"docs/runbooks/bedrock-mantle-api-key.md) or stop pointing "
            f"BEDROCK_*_MODEL_ID at a Responses-backed model."
        )
    return value


def _flatten_user_text(message: dict) -> str:
    """Convert a Converse-shaped user message to a plain text string.

    Chunk 03 skeleton: only single-turn user text is supported. Chunks
    04/06 land the richer input shapes (multi-turn, images, tool history).
    """
    content = message.get("content", [])
    parts: list[str] = []
    for block in content:
        if "text" in block:
            parts.append(block["text"])
        else:
            raise TranslationError(
                "Responses backend chunk 03 skeleton handles text-only "
                f"content blocks; got block keys={list(block.keys())}. "
                "Richer translation lands in chunks 04 (images) and 06 (tools)."
            )
    return "\n\n".join(parts)


def _translate_response_to_unified(raw) -> Response:
    """Convert an openai-SDK Response object into our unified Response."""
    text = ""
    content_blocks: list[dict] = []
    tool_calls: list[ToolCall] = []

    for item in raw.output:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            for sub in item.content:
                sub_type = getattr(sub, "type", None)
                if sub_type == "output_text":
                    content_blocks.append({"text": sub.text})
                    if not text:
                        text = sub.text
                elif sub_type == "refusal":
                    # Safety refusal — surface as text so audit captures it.
                    refusal_text = getattr(sub, "refusal", "") or ""
                    content_blocks.append({"text": refusal_text})
                    if not text:
                        text = refusal_text
                else:
                    raise TranslationError(
                        f"Responses output message has unknown content "
                        f"sub-item type={sub_type!r}"
                    )
        elif item_type == "function_call":
            # Chunk 06 will populate tool_calls + Converse-shaped toolUse
            # blocks here. Skeleton: raise so the boundary is loud.
            raise TranslationError(
                "function_call output items land in chunk 06."
            )
        elif item_type == "reasoning":
            # Internal model thinking on o-series. Don't surface to caller.
            continue
        else:
            raise TranslationError(
                f"Responses output has unknown item type={item_type!r}"
            )

    usage_raw = raw.usage
    usage = Usage(
        input_tokens=getattr(usage_raw, "input_tokens", 0) if usage_raw else 0,
        output_tokens=getattr(usage_raw, "output_tokens", 0) if usage_raw else 0,
    )

    status = getattr(raw, "status", None)
    if status == "completed":
        stop_reason = (
            StopReason.TOOL_USE if tool_calls else StopReason.END_TURN
        )
    elif status == "incomplete":
        details = getattr(raw, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details else None
        if reason == "max_output_tokens":
            stop_reason = StopReason.MAX_TOKENS
        else:
            raise TranslationError(
                f"Responses status=incomplete with unknown reason={reason!r}"
            )
    else:
        raise TranslationError(
            f"Responses returned status={status!r}; expected completed or incomplete."
        )

    return Response(
        text=text,
        content_blocks=content_blocks,
        usage=usage,
        stop_reason=stop_reason,
        tool_calls=tool_calls or None,
        raw=raw,
    )


def invoke(req: Request) -> Response:
    if req.system or req.tools or len(req.messages) > 1:
        raise TranslationError(
            "Responses backend chunk 03 skeleton handles single-turn user "
            "text only. Multi-turn / system / tools land in chunks 04+06."
        )

    client = _get_client()
    last = req.messages[-1]
    if last.get("role") != "user":
        raise TranslationError(
            f"Skeleton expects a single user-role message; got role="
            f"{last.get('role')!r}."
        )

    text = _flatten_user_text(last)

    kwargs: dict = {
        "model": req.model_id,
        "input": text,
        "max_output_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p
    if req.stop_sequences:
        logger.warning(
            "Responses API does not support stop_sequences; dropping %r. "
            "(Caller will not see the configured stop sequences honored.)",
            req.stop_sequences,
        )

    raw = client.responses.create(**kwargs)
    return _translate_response_to_unified(raw)


def invoke_stream(req: Request) -> Iterator[StreamEvent]:
    if req.system or req.tools or len(req.messages) > 1:
        raise TranslationError(
            "Responses backend chunk 03 skeleton streaming handles "
            "single-turn user text only. Multi-turn / system / tools land "
            "in chunks 04+06."
        )

    client = _get_client()
    last = req.messages[-1]
    if last.get("role") != "user":
        raise TranslationError(
            f"Skeleton expects a single user-role message; got role="
            f"{last.get('role')!r}."
        )

    text = _flatten_user_text(last)

    kwargs: dict = {
        "model": req.model_id,
        "input": text,
        "max_output_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p

    # Maps SDK item_id -> caller-facing call_id; populated when a
    # function_call OutputItemAddedEvent arrives. Chunk 06 wires this fully;
    # chunk 03 skeleton uses it only to detect that a tool was streamed and
    # therefore emit Stop(TOOL_USE) at completion.
    call_id_by_item_id: dict[str, str] = {}
    seen_function_call = False

    with client.responses.stream(**kwargs) as stream:
        for event in stream:
            event_type = getattr(event, "type", "")

            if event_type == "response.output_text.delta":
                yield TextDelta(text=event.delta)
                continue

            if event_type == "response.output_item.added":
                item = event.item
                item_type = getattr(item, "type", None)
                if item_type == "function_call":
                    seen_function_call = True
                    call_id = getattr(item, "call_id", "")
                    item_id = getattr(item, "id", "") or ""
                    if item_id:
                        call_id_by_item_id[item_id] = call_id
                    yield ToolUseStart(
                        tool_use_id=call_id,
                        name=getattr(item, "name", "") or "",
                    )
                    continue
                # message items, reasoning items, etc. — nothing to emit yet
                continue

            if event_type == "response.function_call_arguments.delta":
                call_id = call_id_by_item_id.get(event.item_id, event.item_id)
                yield ToolUseArgumentsDelta(
                    tool_use_id=call_id,
                    arguments_partial=event.delta,
                )
                continue

            if event_type == "response.function_call_arguments.done":
                call_id = call_id_by_item_id.get(event.item_id, event.item_id)
                try:
                    args = json.loads(event.arguments or "{}")
                except json.JSONDecodeError as e:
                    raise TranslationError(
                        f"Responses stream tool args for call_id={call_id!r} "
                        f"name={event.name!r} did not parse as JSON: {e}; "
                        f"raw arguments={event.arguments!r}"
                    ) from e
                yield ToolUseStop(tool_use_id=call_id, arguments=args)
                continue

            if event_type == "response.completed":
                resp = event.response
                status = getattr(resp, "status", None)
                if status == "completed":
                    stop_reason = (
                        StopReason.TOOL_USE if seen_function_call
                        else StopReason.END_TURN
                    )
                elif status == "incomplete":
                    details = getattr(resp, "incomplete_details", None)
                    reason = getattr(details, "reason", None) if details else None
                    if reason == "max_output_tokens":
                        stop_reason = StopReason.MAX_TOKENS
                    else:
                        raise TranslationError(
                            f"Responses stream completed with status="
                            f"incomplete reason={reason!r}"
                        )
                else:
                    raise TranslationError(
                        f"Responses stream completed event has status="
                        f"{status!r}"
                    )
                yield Stop(stop_reason=stop_reason)
                usage_raw = getattr(resp, "usage", None)
                if usage_raw:
                    yield Metadata(
                        usage=Usage(
                            input_tokens=getattr(usage_raw, "input_tokens", 0) or 0,
                            output_tokens=getattr(usage_raw, "output_tokens", 0) or 0,
                        )
                    )
                continue

            if event_type in (
                "response.created",
                "response.in_progress",
                "response.queued",
                "response.output_item.done",
                "response.content_part.added",
                "response.content_part.done",
                "response.output_text.done",
                "response.output_text.annotation.added",
            ):
                # Lifecycle / metadata events with no unified equivalent.
                # Silent skip is correct — they carry no per-token payload.
                continue

            if event_type in ("response.failed", "error"):
                err = getattr(event, "response", None) or event
                raise TranslationError(
                    f"Responses stream emitted failure event type={event_type!r}: "
                    f"{err!r}"
                )

            # Unknown event type: raise (loud failure per ADR-035 §7). If a
            # new SDK event type appears that we want to silently skip
            # (e.g., reasoning-text deltas), add it to the skip list above
            # rather than swallowing here.
            raise TranslationError(
                f"Responses stream emitted unknown event type={event_type!r}. "
                f"Either add it to the silent-skip list or wire its "
                f"translation in chunk 04/05/06."
            )
