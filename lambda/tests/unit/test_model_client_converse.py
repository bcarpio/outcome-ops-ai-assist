# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared._converse_backend (chunk 02).

Mock boto3's bedrock-runtime client; no real AWS calls.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from shared import _converse_backend
from shared.model_client_types import (
    Metadata,
    Request,
    Stop,
    StopReason,
    TextDelta,
    ToolSpec,
    ToolUseArgumentsDelta,
    ToolUseStart,
    ToolUseStop,
    TranslationError,
)


@pytest.fixture(autouse=True)
def reset_module_client():
    _converse_backend._client = None
    yield
    _converse_backend._client = None


# ---- invoke (non-streaming) ----


def _converse_resp_text_only():
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Paris"}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 12, "outputTokens": 1, "totalTokens": 13},
    }


def test_converse_text_only_request_kwargs():
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        resp = _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "Capital of France?"}]}],
            max_tokens=64,
            temperature=0.0,
        ))
    kwargs = fake_client.converse.call_args.kwargs
    assert kwargs["modelId"] == "anthropic.claude-opus-4-7-20260514-v1:0"
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"text": "Capital of France?"}]}
    ]
    assert kwargs["inferenceConfig"] == {"maxTokens": 64, "temperature": 0.0}
    assert "system" not in kwargs
    assert "toolConfig" not in kwargs
    assert resp.text == "Paris"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 1
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.tool_calls is None


def test_converse_system_string_normalized_to_block_list():
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system="You are X",
        ))
    kwargs = fake_client.converse.call_args.kwargs
    assert kwargs["system"] == [{"text": "You are X"}]


def test_converse_system_list_emits_one_block_per_entry():
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=["You are X", "Rules apply"],
        ))
    kwargs = fake_client.converse.call_args.kwargs
    assert kwargs["system"] == [{"text": "You are X"}, {"text": "Rules apply"}]


def test_converse_top_p_and_stop_sequences_passed_when_set():
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            top_p=0.9,
            stop_sequences=["STOP"],
        ))
    ic = fake_client.converse.call_args.kwargs["inferenceConfig"]
    assert ic["topP"] == 0.9
    assert ic["stopSequences"] == ["STOP"]


def test_converse_omits_sampling_for_claude_5x():
    # Claude 5.x deprecated caller-controlled sampling on Bedrock: temperature
    # and topP must be omitted or Converse raises ValidationException. maxTokens
    # and stopSequences are still allowed.
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        _converse_backend.invoke(Request(
            model_id="us.anthropic.claude-sonnet-5-5",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            max_tokens=64,
            temperature=0.3,
            top_p=0.9,
            stop_sequences=["STOP"],
        ))
    ic = fake_client.converse.call_args.kwargs["inferenceConfig"]
    assert "temperature" not in ic
    assert "topP" not in ic
    assert ic["maxTokens"] == 64
    assert ic["stopSequences"] == ["STOP"]


def test_converse_keeps_sampling_for_claude_4x():
    # The 4.x models we run today (haiku-4-5 / sonnet-4-6) still accept sampling.
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        _converse_backend.invoke(Request(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            temperature=0.1,
        ))
    ic = fake_client.converse.call_args.kwargs["inferenceConfig"]
    assert ic["temperature"] == 0.1


def test_converse_tools_translate_to_toolconfig():
    fake_client = MagicMock()
    fake_client.converse.return_value = _converse_resp_text_only()
    with patch("boto3.client", return_value=fake_client):
        _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            tools=[ToolSpec(
                name="get_weather",
                description="Get the weather",
                input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
            )],
        ))
    tc = fake_client.converse.call_args.kwargs["toolConfig"]
    assert tc["tools"][0]["toolSpec"]["name"] == "get_weather"
    assert tc["tools"][0]["toolSpec"]["description"] == "Get the weather"
    assert tc["tools"][0]["toolSpec"]["inputSchema"] == {"json": {
        "type": "object", "properties": {"city": {"type": "string"}}}}


def test_converse_response_extracts_tool_use_block():
    fake_client = MagicMock()
    fake_client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [
            {"text": "Let me check."},
            {"toolUse": {
                "toolUseId": "tu_abc",
                "name": "get_weather",
                "input": {"city": "SF"},
            }},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 20, "outputTokens": 5},
    }
    with patch("boto3.client", return_value=fake_client):
        resp = _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "weather?"}]}],
        ))
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.text == "Let me check."
    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].tool_use_id == "tu_abc"
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].input == {"city": "SF"}


def test_converse_max_tokens_stop_reason_maps():
    fake_client = MagicMock()
    resp = _converse_resp_text_only()
    resp["stopReason"] = "max_tokens"
    fake_client.converse.return_value = resp
    with patch("boto3.client", return_value=fake_client):
        out = _converse_backend.invoke(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        ))
    assert out.stop_reason == StopReason.MAX_TOKENS


def test_converse_unknown_stop_reason_raises_translation_error():
    fake_client = MagicMock()
    resp = _converse_resp_text_only()
    resp["stopReason"] = "novel_value"
    fake_client.converse.return_value = resp
    with patch("boto3.client", return_value=fake_client):
        with pytest.raises(TranslationError):
            _converse_backend.invoke(Request(
                model_id="anthropic.claude-opus-4-7-20260514-v1:0",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            ))


def test_converse_propagates_validation_exception_loudly():
    # The exact failure mode from 2026-06-04: Converse rejects a model_id.
    # The shared client must surface this, not swallow it.
    fake_client = MagicMock()
    fake_client.converse.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "The provided model identifier is invalid."}},
        "Converse",
    )
    with patch("boto3.client", return_value=fake_client):
        with pytest.raises(ClientError) as exc:
            _converse_backend.invoke(Request(
                model_id="us.openai.gpt-5-5:0",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            ))
    assert "ValidationException" in str(exc.value)


# ---- invoke_stream (streaming) ----


def _stream_events(events):
    """Helper: return a fake `converse_stream` response wrapping `events`."""
    return {"stream": iter(events)}


def test_stream_text_only_emits_text_deltas_then_stop_then_metadata():
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": " world"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5}}},
    ]
    fake_client = MagicMock()
    fake_client.converse_stream.return_value = _stream_events(events)
    with patch("boto3.client", return_value=fake_client):
        emitted = list(_converse_backend.invoke_stream(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "say hi"}]}],
        )))
    text_deltas = [e for e in emitted if isinstance(e, TextDelta)]
    assert [t.text for t in text_deltas] == ["Hello", " world"]
    stop = [e for e in emitted if isinstance(e, Stop)]
    assert len(stop) == 1
    assert stop[0].stop_reason == StopReason.END_TURN
    meta = [e for e in emitted if isinstance(e, Metadata)]
    assert len(meta) == 1
    assert meta[0].usage.input_tokens == 3
    assert meta[0].usage.output_tokens == 2


def test_stream_tool_use_emits_start_args_deltas_stop():
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {
            "start": {"toolUse": {"toolUseId": "tu_1", "name": "get_weather"}},
            "contentBlockIndex": 0,
        }},
        {"contentBlockDelta": {
            "delta": {"toolUse": {"input": '{"city":'}},
            "contentBlockIndex": 0,
        }},
        {"contentBlockDelta": {
            "delta": {"toolUse": {"input": ' "SF"}'}},
            "contentBlockIndex": 0,
        }},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 4}}},
    ]
    fake_client = MagicMock()
    fake_client.converse_stream.return_value = _stream_events(events)
    with patch("boto3.client", return_value=fake_client):
        emitted = list(_converse_backend.invoke_stream(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "weather?"}]}],
        )))
    starts = [e for e in emitted if isinstance(e, ToolUseStart)]
    assert len(starts) == 1 and starts[0].name == "get_weather"
    deltas = [e for e in emitted if isinstance(e, ToolUseArgumentsDelta)]
    assert "".join(d.arguments_partial for d in deltas) == '{"city": "SF"}'
    stops_tool = [e for e in emitted if isinstance(e, ToolUseStop)]
    assert len(stops_tool) == 1
    assert stops_tool[0].arguments == {"city": "SF"}
    stops_msg = [e for e in emitted if isinstance(e, Stop)]
    assert stops_msg[0].stop_reason == StopReason.TOOL_USE


def test_stream_tool_args_invalid_json_raises_translation_error():
    events = [
        {"contentBlockStart": {
            "start": {"toolUse": {"toolUseId": "tu_x", "name": "x"}},
            "contentBlockIndex": 0,
        }},
        {"contentBlockDelta": {
            "delta": {"toolUse": {"input": "not-json"}},
            "contentBlockIndex": 0,
        }},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    fake_client = MagicMock()
    fake_client.converse_stream.return_value = _stream_events(events)
    with patch("boto3.client", return_value=fake_client):
        with pytest.raises(TranslationError):
            list(_converse_backend.invoke_stream(Request(
                model_id="anthropic.claude-opus-4-7-20260514-v1:0",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )))


def test_stream_unknown_stop_reason_raises_translation_error():
    events = [
        {"contentBlockDelta": {"delta": {"text": "x"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "novel"}},
    ]
    fake_client = MagicMock()
    fake_client.converse_stream.return_value = _stream_events(events)
    with patch("boto3.client", return_value=fake_client):
        with pytest.raises(TranslationError):
            list(_converse_backend.invoke_stream(Request(
                model_id="anthropic.claude-opus-4-7-20260514-v1:0",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )))


def test_stream_max_tokens_stop_reason_maps():
    events = [
        {"contentBlockDelta": {"delta": {"text": "x"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "max_tokens"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}},
    ]
    fake_client = MagicMock()
    fake_client.converse_stream.return_value = _stream_events(events)
    with patch("boto3.client", return_value=fake_client):
        emitted = list(_converse_backend.invoke_stream(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "count high"}]}],
        )))
    stops = [e for e in emitted if isinstance(e, Stop)]
    assert stops[0].stop_reason == StopReason.MAX_TOKENS


def test_stream_unknown_event_type_is_ignored_not_raised():
    # Forward-compatibility: a new event type should not break the stream.
    events = [
        {"contentBlockDelta": {"delta": {"text": "x"}, "contentBlockIndex": 0}},
        {"someFutureEventType": {"foo": "bar"}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    fake_client = MagicMock()
    fake_client.converse_stream.return_value = _stream_events(events)
    with patch("boto3.client", return_value=fake_client):
        emitted = list(_converse_backend.invoke_stream(Request(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "x"}]}],
        )))
    assert [e for e in emitted if isinstance(e, TextDelta)]
    assert [e for e in emitted if isinstance(e, Stop)]
