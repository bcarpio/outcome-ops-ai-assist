"""Unit tests for shared.model_client dispatch (chunk 01).

These tests do not exercise real boto3 / HTTPS. Backends are stubs.
"""

from unittest.mock import patch

import pytest

from shared import model_client
from shared.model_client import (
    BackendConfigError,
    Metadata,
    Stop,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolUseArgumentsDelta,
    ToolUseStart,
    ToolUseStop,
    UnsupportedModelError,
)


@pytest.mark.parametrize(
    "model_id, expected_backend",
    [
        ("anthropic.claude-opus-4-7-20260514-v1:0", "converse"),
        ("us.anthropic.claude-opus-4-7-20260514-v1:0", "converse"),
        ("meta.llama-3-1-405b-instruct-v1:0", "converse"),
        ("us.meta.llama-3-1-405b-instruct-v1:0", "converse"),
        ("amazon.nova-pro-v1:0", "converse"),
        ("us.amazon.nova-pro-v1:0", "converse"),
        ("openai.gpt-oss-120b-1:0", "converse"),
        ("openai.gpt-oss-20b-1:0", "converse"),
        ("openai.gpt-5-5:0", "responses"),
        ("openai.gpt-5-4:0", "responses"),
        ("us.openai.gpt-5-5:0", "responses"),
        ("openai.codex-1:0", "responses"),
        ("us.openai.codex-1:0", "responses"),
    ],
)
def test_dispatch_resolves_correct_backend(model_id, expected_backend):
    assert model_client._resolve_backend(model_id) == expected_backend


def test_dispatch_unknown_prefix_raises_unsupported_model_error():
    with pytest.raises(UnsupportedModelError):
        model_client._resolve_backend("cohere.command-r-plus")


def test_dispatch_empty_model_id_raises_unsupported_model_error():
    with pytest.raises(UnsupportedModelError):
        model_client._resolve_backend("")


def test_longest_prefix_wins():
    # openai.gpt-5-5:0 must match `openai.gpt-5` (responses), not the
    # later `openai.gpt-oss-` (converse) — the longest-prefix-wins sort
    # in _resolve_backend is what makes this guarantee.
    assert model_client._resolve_backend("openai.gpt-5-5:0") == "responses"
    # openai.gpt-oss-120b-1:0 must still match `openai.gpt-oss-` (converse).
    assert model_client._resolve_backend("openai.gpt-oss-120b-1:0") == "converse"


def test_invoke_routes_to_converse_backend_stub():
    # We don't want the stub's NotImplementedError to mask a dispatch
    # bug. Patch the converse stub with a sentinel that captures the
    # request and asserts it received the right model_id.
    captured = {}

    def fake_invoke(req):
        captured["model_id"] = req.model_id
        captured["messages"] = req.messages
        from shared.model_client_types import Response, StopReason, Usage
        return Response(
            text="ok", content_blocks=[{"text": "ok"}],
            usage=Usage(input_tokens=1, output_tokens=1),
            stop_reason=StopReason.END_TURN,
        )

    with patch("shared._converse_backend.invoke", side_effect=fake_invoke):
        resp = model_client.invoke(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )
    assert captured["model_id"] == "anthropic.claude-opus-4-7-20260514-v1:0"
    assert resp.text == "ok"


def test_invoke_routes_to_responses_backend_stub():
    captured = {}

    def fake_invoke(req):
        captured["model_id"] = req.model_id
        from shared.model_client_types import Response, StopReason, Usage
        return Response(
            text="ok", content_blocks=[{"text": "ok"}],
            usage=Usage(input_tokens=1, output_tokens=1),
            stop_reason=StopReason.END_TURN,
        )

    with patch("shared._responses_backend.invoke", side_effect=fake_invoke):
        resp = model_client.invoke(
            model_id="openai.gpt-5-5:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )
    assert captured["model_id"] == "openai.gpt-5-5:0"
    assert resp.text == "ok"


def test_invoke_propagates_not_implemented_from_stub():
    # With no patching, the stubs raise NotImplementedError. invoke()
    # must propagate that, not swallow it.
    with pytest.raises(NotImplementedError):
        model_client.invoke(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )


def test_invoke_unknown_model_id_raises_unsupported_before_backend_call():
    with pytest.raises(UnsupportedModelError):
        model_client.invoke(
            model_id="cohere.command-r-plus",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )


def test_invoke_stream_routes_to_correct_backend_and_yields():
    from shared.model_client_types import Usage

    def fake_stream(req):
        assert req.model_id == "anthropic.claude-opus-4-7-20260514-v1:0"
        yield TextDelta(text="hi")
        yield Stop(stop_reason=StopReason.END_TURN)
        yield Metadata(usage=Usage(input_tokens=1, output_tokens=1))

    with patch("shared._converse_backend.invoke_stream", side_effect=fake_stream):
        events = list(model_client.invoke_stream(
            model_id="anthropic.claude-opus-4-7-20260514-v1:0",
            messages=[{"role": "user", "content": [{"text": "say hi"}]}],
        ))
    assert any(isinstance(e, TextDelta) for e in events)
    assert any(isinstance(e, Stop) for e in events)
    assert any(isinstance(e, Metadata) for e in events)


def test_invoke_stream_unknown_model_id_raises_unsupported():
    with pytest.raises(UnsupportedModelError):
        # Iterator is lazy; trigger by listing.
        list(model_client.invoke_stream(
            model_id="cohere.command-r-plus",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        ))


def test_stream_event_subclasses_are_instances_of_streamevent():
    # Guard against the dataclass hierarchy being broken by future edits.
    from shared.model_client_types import Usage

    assert isinstance(TextDelta(text="x"), StreamEvent)
    assert isinstance(ToolUseStart(tool_use_id="t1", name="x"), StreamEvent)
    assert isinstance(
        ToolUseArgumentsDelta(tool_use_id="t1", arguments_partial="{"),
        StreamEvent,
    )
    assert isinstance(ToolUseStop(tool_use_id="t1", arguments={}), StreamEvent)
    assert isinstance(Stop(stop_reason=StopReason.END_TURN), StreamEvent)
    assert isinstance(
        Metadata(usage=Usage(input_tokens=1, output_tokens=2)),
        StreamEvent,
    )
