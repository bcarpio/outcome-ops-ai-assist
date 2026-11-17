# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""shared._responses_backend — Bedrock Mantle Responses API backend.

Uses the official `openai` Python SDK pointed at the Bedrock Mantle
endpoint via base_url override. Auth is a long-lived Bedrock API key
loaded from SSM at module init (per ADR-035 §4).

Translation table per ADR-035 §3:

  Unified (Converse-flavored)      OpenAI SDK Responses-API args
  ---------------------------      -----------------------------
  Request.system                   instructions=<str>
  Request.messages (single user
    text)                          input=<str>
  Request.messages (multi-turn or
    non-text content)              input=[<typed items>...]
  Request.max_tokens               max_output_tokens=<int>
  Request.temperature              temperature=<float>
  Request.top_p                    top_p=<float>
  Request.stop_sequences           (dropped; not supported on Responses)
  Request.tools                    tools=[{type:"function", name,
                                          description, parameters}]
  toolUse content block            input item {type:"function_call",
                                              call_id, name, arguments}
  toolResult content block         input item {type:"function_call_output",
                                              call_id, output}
  image content block              input item {type:"input_image",
                                              image_url:"data:..."}

Response translation walks raw.output[] and pulls type=="message"
content into text + content_blocks, type=="function_call" into
tool_calls + Converse-shaped toolUse blocks. status="completed" with
any function_call output -> StopReason.TOOL_USE; otherwise END_TURN.
status="incomplete" + reason="max_output_tokens" -> MAX_TOKENS.

Streaming consumes the SDK's typed events. Tool-call argument
accumulation is handled by the SDK (it emits a Done event with the
full arguments string); we just json.loads at the Done event.
"""

import base64
import json
import logging
import os
from typing import Any, Iterator

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
    ToolSpec,
    ToolUseArgumentsDelta,
    ToolUseStart,
    ToolUseStop,
    TranslationError,
    Usage,
)

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


# ============================================================================
# Module init / API key loading
# ============================================================================


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


# ============================================================================
# Request translation (chunk 04)
# ============================================================================


_IMAGE_FORMAT_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def _normalize_system_to_instructions(system) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        # Concat with a blank line between blocks, mirroring how Converse
        # surfaces a multi-block system to the model.
        return "\n\n".join(s for s in system if s)
    raise TranslationError(
        f"Request.system must be str or list[str]; got {type(system).__name__}"
    )


def _image_data_url(image_block: dict) -> str:
    """Convert a Converse image block to an OpenAI input_image data URL."""
    img = image_block["image"]
    fmt = img.get("format", "").lower()
    mime = _IMAGE_FORMAT_MIME.get(fmt)
    if not mime:
        raise TranslationError(
            f"Converse image block has unsupported format={fmt!r}; "
            f"expected one of {list(_IMAGE_FORMAT_MIME)}."
        )
    source = img.get("source", {})
    raw_bytes = source.get("bytes")
    if raw_bytes is None:
        raise TranslationError(
            "Converse image block missing source.bytes (no other source "
            "type supported)."
        )
    if isinstance(raw_bytes, bytes):
        b64 = base64.b64encode(raw_bytes).decode("ascii")
    else:
        # already base64-string-encoded (the form Converse usually carries
        # when the bytes were assembled by a previous step).
        b64 = raw_bytes
    return f"data:{mime};base64,{b64}"


def _translate_content_block_for_user(block: dict) -> dict:
    if "text" in block:
        return {"type": "input_text", "text": block["text"]}
    if "image" in block:
        return {"type": "input_image", "image_url": _image_data_url(block)}
    if "toolResult" in block:
        # toolResult blocks are handled at the message level (they split
        # the user message into role:"tool" items). If we see one here, the
        # caller mixed toolResult with non-toolResult content in the same
        # message, which the translator does not support — flag it loudly.
        raise TranslationError(
            "toolResult content blocks must be the only blocks in their "
            "message; mixing toolResult with other content in one user "
            "message is not supported. Split into separate messages."
        )
    raise TranslationError(
        f"Unsupported user content block keys={list(block.keys())}"
    )


def _translate_content_block_for_assistant(block: dict) -> dict | None:
    if "text" in block:
        return {"type": "output_text", "text": block["text"]}
    if "toolUse" in block:
        # toolUse blocks become separate function_call items at the
        # message level. The caller of _translate_messages_to_input
        # handles splitting; we don't return a content sub-item here.
        return None
    raise TranslationError(
        f"Unsupported assistant content block keys={list(block.keys())}"
    )


def _translate_messages_to_input(messages: list[dict]) -> str | list[dict]:
    """Translate Converse-shaped messages into the SDK's input= argument.

    Single-turn user text -> bare string (the SDK's string shorthand).
    Anything else -> list of typed items.

    Tool-use translation:
      - assistant content {"toolUse": ...} -> separate
        {"type":"function_call", call_id, name, arguments} input item
      - user content {"toolResult": ...} -> separate
        {"type":"function_call_output", call_id, output} input item
        (one per toolResult block in the source message)
    """
    if not messages:
        raise TranslationError("Request.messages is empty.")

    # Fast path: single user message containing only text blocks.
    if (
        len(messages) == 1
        and messages[0].get("role") == "user"
        and all("text" in b for b in messages[0].get("content", []))
    ):
        content = messages[0]["content"]
        return "\n\n".join(b["text"] for b in content)

    items: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])
        if role == "system":
            raise TranslationError(
                "system-role messages are not supported inside "
                "Request.messages on the Responses backend. Pass system "
                "via Request.system; the translator routes it to "
                "instructions=."
            )

        if role == "user":
            # toolResult blocks become separate function_call_output items.
            tool_result_blocks = [b for b in content if "toolResult" in b]
            non_tool_blocks = [b for b in content if "toolResult" not in b]
            if tool_result_blocks and non_tool_blocks:
                raise TranslationError(
                    "User message mixes toolResult with other content; "
                    "split into separate messages."
                )
            if tool_result_blocks:
                for tr_block in tool_result_blocks:
                    tr = tr_block["toolResult"]
                    call_id = tr.get("toolUseId", "")
                    # toolResult.content is typically a list of {text:...}
                    # blocks. Flatten to a single string for OpenAI.
                    result_content = tr.get("content", [])
                    output_str = "\n\n".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and "text" in b
                    )
                    if tr.get("status") == "error":
                        output_str = f"[error] {output_str}"
                    items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_str,
                    })
                continue
            # Plain user content (text / images).
            user_content_items = [
                _translate_content_block_for_user(b) for b in non_tool_blocks
            ]
            items.append({"role": "user", "content": user_content_items})
            continue

        if role == "assistant":
            # toolUse blocks become separate function_call items.
            tool_use_blocks = [b for b in content if "toolUse" in b]
            text_blocks = [b for b in content if "text" in b]
            other_blocks = [
                b for b in content
                if "toolUse" not in b and "text" not in b
            ]
            if other_blocks:
                raise TranslationError(
                    f"Assistant message has unsupported content blocks: "
                    f"{[list(b.keys()) for b in other_blocks]}"
                )

            if text_blocks:
                assistant_content_items = [
                    _translate_content_block_for_assistant(b)
                    for b in text_blocks
                ]
                items.append({
                    "role": "assistant",
                    "content": assistant_content_items,
                })

            for tu_block in tool_use_blocks:
                tu = tu_block["toolUse"]
                items.append({
                    "type": "function_call",
                    "call_id": tu.get("toolUseId", ""),
                    "name": tu.get("name", ""),
                    "arguments": json.dumps(tu.get("input", {})),
                })
            continue

        raise TranslationError(f"Unsupported message role={role!r}")

    return items


def _translate_tools(tools: list[ToolSpec] | None) -> list[dict] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        }
        for t in tools
    ]


# GPT-5 family bills internal reasoning tokens against max_output_tokens.
# A caller asking for "1024 visible output tokens" gets ZERO output if the
# model spends 1024 on reasoning -- the call returns
# stop_reason=MAX_TOKENS with empty text.
#
# The unified Request.max_tokens semantically means "visible output I
# want to see." Converse backend honors it directly. Responses backend
# grants 4x headroom so reasoning has room without starving the output,
# capped at REASONING_BUDGET_CEILING to prevent runaway invocations
# when a caller asks for a big budget (e.g. chat at 8192 -> 32768
# without the cap, which lets GPT-5.5 reason for 15+ min and trip
# Lambda's timeout).
#
# Live-burned 2026-06-18 on the first openai workspace architectural-
# summary call after AWS Sales cleared GPT-5.5 entitlement:
#   process-repo-summary requested max_tokens=1024
#   -> MAX_TOKENS / empty text -> "No text content in openai.gpt-5.5
#      response" -> repo-summary errored -> no code-units dispatched
#      -> process-batch-summary never ran -> 0 code-map vectors.
#
# Live-burned again 2026-06-25 on the openai chat workspace:
#   chat (max_tokens=8192) -> 32768 budget -> default reasoning
#   "medium" let GPT-5.5 reason for >15min -> Lambda 900s timeout
#   killed three invocations in a row.
#
# REASONING_EFFORT="minimal" (added 2026-06-25) tells GPT-5 family
# to spend the minimum reasoning chain that produces a coherent
# answer. Per OpenAI guidance for chat / latency-sensitive use
# cases; equivalent reasoning depth to a previous-generation
# non-reasoning model.
#
# Revisit both knobs when a non-reasoning Responses-backed model
# emerges (today: gpt-5.4, gpt-5.5, codex-* are all reasoning models).
_RESPONSES_REASONING_HEADROOM = 4
_RESPONSES_REASONING_BUDGET_CEILING = 8192
# Bedrock Mantle accepts: "none" | "low" | "medium" | "high" | "xhigh".
# NOTE: OpenAI direct uses "minimal" for the same intent as Mantle's
# "none" -- the wire vocabulary diverged. Mantle's default is "medium";
# we override to "none" for chat-friendly latency (disables the
# internal chain-of-thought entirely). Reads at module load so a
# per-deploy env override can switch without code change.
# Live-burned 2026-06-25: tried "minimal" first (the OpenAI-direct
# value), got HTTP 400 "Unsupported value: 'minimal' is not
# supported with the 'openai.gpt-5.5' model. Supported values are:
# 'none', 'low', 'medium', 'high', and 'xhigh'."
_RESPONSES_REASONING_EFFORT = os.environ.get(
    "BEDROCK_RESPONSES_REASONING_EFFORT", "none"
).strip().lower()


def _build_responses_kwargs(req: Request) -> dict[str, Any]:
    # Reasoning budget: 4x the caller's visible-token request, capped
    # so a generous caller can't accidentally request 30k+ tokens
    # of reasoning + then time out at the Lambda boundary.
    reasoning_budget = min(
        req.max_tokens * _RESPONSES_REASONING_HEADROOM,
        _RESPONSES_REASONING_BUDGET_CEILING,
    )
    kwargs: dict[str, Any] = {
        "model": req.model_id,
        "input": _translate_messages_to_input(req.messages),
        "max_output_tokens": reasoning_budget,
        # Tells GPT-5 family how thoroughly to deliberate before
        # answering. "minimal" matches chat-UI latency expectations
        # and matches Claude's perceived speed on the same prompts.
        "reasoning": {"effort": _RESPONSES_REASONING_EFFORT},
    }
    instructions = _normalize_system_to_instructions(req.system)
    if instructions is not None:
        kwargs["instructions"] = instructions
    # GPT-5 family (gpt-5.4, gpt-5.5, codex-*) on Mantle reject
    # ``temperature`` and ``top_p`` with HTTP 400
    # "Unsupported parameter ... is not supported with this model."
    # The Responses API exposes these fields in the SDK but locks them
    # server-side at 1.0 for the reasoning models -- the only Responses-
    # supporting family on Mantle today. Dropping with a warning so the
    # caller knows their determinism preference can't be honored.
    # Live-burned 2026-06-18 on the openai workspace's first
    # entitled call after AWS Sales cleared the gate.
    if req.temperature != 0.0:
        logger.warning(
            "Responses API does not accept temperature; dropping %r. "
            "(GPT-5 family is locked at temperature=1.0 server-side.)",
            req.temperature,
        )
    if req.top_p is not None:
        logger.warning(
            "Responses API does not accept top_p; dropping %r. "
            "(GPT-5 family is locked server-side.)",
            req.top_p,
        )
    if req.stop_sequences:
        logger.warning(
            "Responses API does not support stop_sequences; dropping %r. "
            "(Caller will not see the configured stop sequences honored.)",
            req.stop_sequences,
        )
    tools = _translate_tools(req.tools)
    if tools:
        kwargs["tools"] = tools
    return kwargs


# ============================================================================
# Response translation (chunk 05)
# ============================================================================


def _translate_response_to_unified(raw) -> Response:
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
            call_id = getattr(item, "call_id", "")
            name = getattr(item, "name", "")
            arguments_str = getattr(item, "arguments", "") or "{}"
            try:
                args = json.loads(arguments_str)
            except json.JSONDecodeError as e:
                raise TranslationError(
                    f"Responses function_call arguments for call_id="
                    f"{call_id!r} name={name!r} did not parse as JSON: {e}"
                ) from e
            tool_calls.append(ToolCall(
                tool_use_id=call_id,
                name=name,
                input=args,
            ))
            content_blocks.append({
                "toolUse": {
                    "toolUseId": call_id,
                    "name": name,
                    "input": args,
                }
            })
        elif item_type == "reasoning":
            # Internal model thinking on o-series. Don't surface.
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


# ============================================================================
# Public entry points
# ============================================================================


def invoke(req: Request) -> Response:
    client = _get_client()
    kwargs = _build_responses_kwargs(req)
    raw = client.responses.create(**kwargs)
    return _translate_response_to_unified(raw)


def invoke_stream(req: Request) -> Iterator[StreamEvent]:
    client = _get_client()
    kwargs = _build_responses_kwargs(req)

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
                    call_id = getattr(item, "call_id", "") or ""
                    item_id = getattr(item, "id", "") or ""
                    if item_id:
                        call_id_by_item_id[item_id] = call_id
                    yield ToolUseStart(
                        tool_use_id=call_id,
                        name=getattr(item, "name", "") or "",
                    )
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
                continue

            if event_type in ("response.failed", "error"):
                err = getattr(event, "response", None) or event
                raise TranslationError(
                    f"Responses stream emitted failure event type={event_type!r}: "
                    f"{err!r}"
                )

            raise TranslationError(
                f"Responses stream emitted unknown event type={event_type!r}. "
                f"Either add it to the silent-skip list or wire its "
                f"translation."
            )
