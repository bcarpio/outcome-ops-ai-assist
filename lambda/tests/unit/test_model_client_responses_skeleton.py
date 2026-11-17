# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared._responses_backend (chunk 03 skeleton).

Mock the openai SDK; no real network. Mock boto3.ssm for the API key load.
"""

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import _responses_backend
from shared.model_client_types import (
    BackendConfigError,
    Metadata,
    Request,
    Stop,
    StopReason,
    TextDelta,
    ToolUseArgumentsDelta,
    ToolUseStart,
    ToolUseStop,
    TranslationError,
)


@pytest.fixture(autouse=True)
def reset_module_state():
    _responses_backend._client = None
    yield
    _responses_backend._client = None


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv(
        "BEDROCK_MANTLE_API_KEY_PARAM",
        "/test/outcome-ops-ai-assist/bedrock/mantle-api-key",
    )
    yield


def _ssm_returns(value: str):
    """Patch boto3.client('ssm').get_parameter to return `value`."""
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
    return patch("boto3.client", return_value=ssm)


def _mock_openai(fake_client):
    """Patch the OpenAI constructor to return `fake_client`."""
    return patch("shared._responses_backend.OpenAI", return_value=fake_client)


# ===== module-init / API key loading =====


def test_module_init_raises_when_ssm_param_env_unset(monkeypatch):
    monkeypatch.delenv("BEDROCK_MANTLE_API_KEY_PARAM", raising=False)
    with pytest.raises(BackendConfigError):
        _responses_backend._get_client()


def test_module_init_raises_when_aws_region_unset(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("THIS_REGION", raising=False)
    with _ssm_returns("real-key-value"):
        with pytest.raises(BackendConfigError):
            _responses_backend._get_client()


def test_module_init_raises_on_placeholder_ssm_value():
    with _ssm_returns("PLACEHOLDER_CONFIGURE_ME"):
        with pytest.raises(BackendConfigError):
            _responses_backend._get_client()


def test_module_init_raises_on_empty_ssm_value():
    with _ssm_returns(""):
        with pytest.raises(BackendConfigError):
            _responses_backend._get_client()


def test_module_init_builds_client_with_correct_base_url():
    fake_openai = MagicMock()
    with _ssm_returns("sk-bedrock-real-value"):
        with patch("shared._responses_backend.OpenAI", return_value=fake_openai) as ctor:
            client = _responses_backend._get_client()
    assert client is fake_openai
    ctor.assert_called_once()
    kwargs = ctor.call_args.kwargs
    assert kwargs["api_key"] == "sk-bedrock-real-value"
    assert kwargs["base_url"] == "https://bedrock-mantle.us-east-2.api.aws/openai/v1"


# ===== invoke (non-streaming) =====


def _fake_completed_text_response(text: str, in_t: int = 10, out_t: int = 5):
    """Build a fake openai-SDK Response object for a text-only completion."""
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=in_t, output_tokens=out_t),
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
    )


def test_invoke_text_only_passes_input_max_output_tokens():
    # GPT-5 family on Mantle rejects ``temperature`` and ``top_p`` --
    # both are locked at 1.0 server-side. Burned 2026-06-18 on the
    # first entitled call. The backend drops both unconditionally;
    # tests pin that they're absent from the SDK kwargs even when the
    # caller set them.
    fake_client = MagicMock()
    fake_client.responses.create.return_value = _fake_completed_text_response("Paris")
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            resp = _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "Capital of France?"}]}],
                max_tokens=64,
                temperature=0.0,
            ))
    kwargs = fake_client.responses.create.call_args.kwargs
    assert kwargs["model"] == "openai.gpt-5.5"
    assert kwargs["input"] == "Capital of France?"
    # max_output_tokens is the headroom (4x) of caller's max_tokens
    # capped at the budget ceiling (8192). Caller asked for 64
    # visible output tokens -> 256 total budget (below ceiling).
    # Pinning both the multiplier and the ceiling here so a future
    # change has to update the constant and this test together.
    assert kwargs["max_output_tokens"] == 64 * 4
    # reasoning.effort is read from BEDROCK_RESPONSES_REASONING_EFFORT
    # env var with default "none" -- disables GPT-5's deliberation
    # entirely for chat-friendly latency. Mantle vocabulary differs
    # from OpenAI direct; "minimal" (OpenAI's term) is rejected with
    # HTTP 400 at the wire. Live-burned 2026-06-25: tried "minimal"
    # first, got "Unsupported value ... Supported values are: 'none',
    # 'low', 'medium', 'high', and 'xhigh'."
    assert kwargs["reasoning"] == {"effort": "none"}
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert resp.text == "Paris"
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 5


def test_invoke_caps_reasoning_budget_at_ceiling():
    # Chat sends max_tokens=8192. Without the ceiling, the 4x
    # headroom multiplier would request 32768 tokens of budget,
    # which lets GPT-5.5 reason for 15+ minutes and hit Lambda's
    # 900s timeout (live-burned 2026-06-25). The ceiling caps the
    # SDK kwarg at _RESPONSES_REASONING_BUDGET_CEILING regardless
    # of what the multiplier would produce.
    fake_client = MagicMock()
    fake_client.responses.create.return_value = _fake_completed_text_response("ok")
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "x"}]}],
                max_tokens=8192,  # chat's typical setting
            ))
    kwargs = fake_client.responses.create.call_args.kwargs
    # 8192 * 4 = 32768 -> capped at 8192. Without the cap this
    # would be 32768.
    assert kwargs["max_output_tokens"] == 8192


def test_invoke_reasoning_effort_respects_env_override(monkeypatch):
    # Default is "minimal"; deploys that want a smarter (slower)
    # chat tier can override via BEDROCK_RESPONSES_REASONING_EFFORT.
    # Reading the env at module load means the test must reload
    # the module after monkeypatching. Simpler: patch the
    # module-level constant directly -- same semantics for the
    # downstream behavior we care about.
    monkeypatch.setattr(
        _responses_backend, "_RESPONSES_REASONING_EFFORT", "high",
    )
    fake_client = MagicMock()
    fake_client.responses.create.return_value = _fake_completed_text_response("ok")
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "x"}]}],
                max_tokens=64,
            ))
    kwargs = fake_client.responses.create.call_args.kwargs
    assert kwargs["reasoning"] == {"effort": "high"}


def test_invoke_max_output_tokens_incomplete_maps_max_tokens_stop_reason():
    fake_client = MagicMock()
    fake_client.responses.create.return_value = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(input_tokens=3, output_tokens=64),
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="counting...")],
            )
        ],
    )
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            resp = _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "count to 1000"}]}],
                max_tokens=64,
            ))
    assert resp.stop_reason == StopReason.MAX_TOKENS


def test_invoke_unknown_status_raises_translation_error():
    fake_client = MagicMock()
    fake_client.responses.create.return_value = SimpleNamespace(
        status="cancelled",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
        output=[],
    )
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                _responses_backend.invoke(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [{"text": "x"}]}],
                ))


def test_invoke_unknown_output_item_type_raises_translation_error():
    fake_client = MagicMock()
    fake_client.responses.create.return_value = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        output=[SimpleNamespace(type="alien_future_item", content=[])],
    )
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                _responses_backend.invoke(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [{"text": "x"}]}],
                ))


def test_invoke_reasoning_items_are_skipped_not_surfaced():
    fake_client = MagicMock()
    fake_client.responses.create.return_value = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=20, output_tokens=10),
        output=[
            SimpleNamespace(type="reasoning", content=[]),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="final answer")],
            ),
        ],
    )
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            resp = _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "x"}]}],
            ))
    assert resp.text == "final answer"


# NOTE: the chunks 04-06 implementation removes the skeleton's
# "raise on system/multi-turn/tools" guards. Tests for the new
# translation paths live in
# test_model_client_responses_translation.py.


# ===== invoke_stream (streaming) =====


class _FakeStreamCtx:
    """Context manager that yields a fixed event list."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *args):
        return False


def _mk_stream_events(*events):
    return _FakeStreamCtx(list(events))


def test_invoke_stream_text_delta_events_emit_unified_text_deltas():
    events = [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_text.delta", delta="Hello "),
        SimpleNamespace(type="response.output_text.delta", delta="world"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            emitted = list(_responses_backend.invoke_stream(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "say hi"}]}],
            )))
    text_deltas = [e for e in emitted if isinstance(e, TextDelta)]
    assert [t.text for t in text_deltas] == ["Hello ", "world"]
    stops = [e for e in emitted if isinstance(e, Stop)]
    assert len(stops) == 1
    assert stops[0].stop_reason == StopReason.END_TURN
    meta = [e for e in emitted if isinstance(e, Metadata)]
    assert len(meta) == 1
    assert meta[0].usage.input_tokens == 3


def test_invoke_stream_completed_incomplete_max_output_tokens_maps_stop():
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="hi"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=SimpleNamespace(input_tokens=1, output_tokens=64),
        )),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            emitted = list(_responses_backend.invoke_stream(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "count high"}]}],
            )))
    stops = [e for e in emitted if isinstance(e, Stop)]
    assert stops[0].stop_reason == StopReason.MAX_TOKENS


def test_invoke_stream_lifecycle_events_are_silently_skipped():
    events = [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.in_progress"),
        SimpleNamespace(type="response.queued"),
        SimpleNamespace(type="response.output_item.added", item=SimpleNamespace(type="message")),
        SimpleNamespace(type="response.content_part.added"),
        SimpleNamespace(type="response.output_text.delta", delta="x"),
        SimpleNamespace(type="response.output_text.done"),
        SimpleNamespace(type="response.content_part.done"),
        SimpleNamespace(type="response.output_item.done"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            emitted = list(_responses_backend.invoke_stream(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )))
    # Only the text_delta, stop, and metadata should surface — lifecycle events
    # are dropped.
    text = [e for e in emitted if isinstance(e, TextDelta)]
    assert len(text) == 1
    stops = [e for e in emitted if isinstance(e, Stop)]
    assert len(stops) == 1
    meta = [e for e in emitted if isinstance(e, Metadata)]
    assert len(meta) == 1


def test_invoke_stream_unknown_event_type_raises_translation_error():
    events = [
        SimpleNamespace(type="response.alien_future_event_type"),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                list(_responses_backend.invoke_stream(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [{"text": "x"}]}],
                )))


def test_invoke_stream_failed_event_raises():
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="x"),
        SimpleNamespace(type="response.failed", response=SimpleNamespace(error="server error")),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                list(_responses_backend.invoke_stream(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [{"text": "x"}]}],
                )))


def test_invoke_stream_function_call_emits_toolusestart_and_arg_events():
    # Chunk 03 SHOULD emit the function-call events that the SDK provides
    # natively. Full multi-turn tool-result handling is chunk 06; the
    # streaming events themselves are basic and should work now.
    events = [
        SimpleNamespace(type="response.output_item.added", item=SimpleNamespace(
            type="function_call",
            id="fc_item_1",
            call_id="call_abc",
            name="get_weather",
        )),
        SimpleNamespace(type="response.function_call_arguments.delta",
                        item_id="fc_item_1", delta='{"city":'),
        SimpleNamespace(type="response.function_call_arguments.delta",
                        item_id="fc_item_1", delta=' "SF"}'),
        SimpleNamespace(type="response.function_call_arguments.done",
                        item_id="fc_item_1", name="get_weather",
                        arguments='{"city": "SF"}'),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=5, output_tokens=8),
        )),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            emitted = list(_responses_backend.invoke_stream(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "weather?"}]}],
            )))
    starts = [e for e in emitted if isinstance(e, ToolUseStart)]
    assert len(starts) == 1
    assert starts[0].tool_use_id == "call_abc"
    assert starts[0].name == "get_weather"
    deltas = [e for e in emitted if isinstance(e, ToolUseArgumentsDelta)]
    assert "".join(d.arguments_partial for d in deltas) == '{"city": "SF"}'
    assert all(d.tool_use_id == "call_abc" for d in deltas)
    tool_stops = [e for e in emitted if isinstance(e, ToolUseStop)]
    assert tool_stops[0].arguments == {"city": "SF"}
    assert tool_stops[0].tool_use_id == "call_abc"
    # Since a function_call was seen, the response.completed maps to TOOL_USE
    # not END_TURN.
    msg_stops = [e for e in emitted if isinstance(e, Stop)]
    assert msg_stops[0].stop_reason == StopReason.TOOL_USE


def test_invoke_stream_function_call_invalid_args_json_raises():
    events = [
        SimpleNamespace(type="response.output_item.added", item=SimpleNamespace(
            type="function_call", id="i1", call_id="call_x", name="x",
        )),
        SimpleNamespace(type="response.function_call_arguments.done",
                        item_id="i1", name="x", arguments="not-json"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(
            status="completed", incomplete_details=None,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )),
    ]
    fake_client = MagicMock()
    fake_client.responses.stream.return_value = _FakeStreamCtx(events)
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                list(_responses_backend.invoke_stream(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [{"text": "x"}]}],
                )))
