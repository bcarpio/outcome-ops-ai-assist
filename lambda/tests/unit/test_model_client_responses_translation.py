"""Unit tests for shared._responses_backend translation paths (chunks 04, 05, 06).

Covers:
- chunk 04: system -> instructions, multi-turn messages -> input list,
  image content blocks -> input_image typed items
- chunk 05: function_call output items -> tool_calls + Converse-shaped
  toolUse blocks in content_blocks
- chunk 06: tools= request translation, toolUse history -> function_call
  input items, toolResult history -> function_call_output input items
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import _responses_backend
from shared.model_client_types import (
    Request,
    StopReason,
    ToolSpec,
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
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
    return patch("boto3.client", return_value=ssm)


def _mock_openai(fake_client):
    return patch("shared._responses_backend.OpenAI", return_value=fake_client)


def _completed_text(text: str, in_t: int = 1, out_t: int = 1):
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


def _capture_kwargs(text="ok"):
    """Return (fake_client, captured) where captured["kwargs"] is set after
    fake_client.responses.create is called."""
    fake_client = MagicMock()
    captured: dict = {}

    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        return _completed_text(text)

    fake_client.responses.create.side_effect = fake_create
    return fake_client, captured


# ============================================================================
# chunk 04: system prompt -> instructions
# ============================================================================


def test_system_string_routes_to_instructions_kwarg():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                system="You are an assistant.",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            ))
    assert captured["kwargs"]["instructions"] == "You are an assistant."


def test_system_list_concatenated_with_double_newline_into_instructions():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                system=["You are X.", "Rules apply."],
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            ))
    assert captured["kwargs"]["instructions"] == "You are X.\n\nRules apply."


def test_system_none_omits_instructions_kwarg():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            ))
    assert "instructions" not in captured["kwargs"]


def test_system_role_inside_messages_raises_translation_error():
    fake_client = MagicMock()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                _responses_backend.invoke(Request(
                    model_id="openai.gpt-5.5",
                    messages=[
                        {"role": "system", "content": [{"text": "you are X"}]},
                        {"role": "user", "content": [{"text": "hi"}]},
                    ],
                ))


# ============================================================================
# chunk 04: messages -> input (single-turn shorthand vs multi-turn list)
# ============================================================================


def test_single_turn_user_text_input_is_string():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "Capital of France?"}]}],
            ))
    assert captured["kwargs"]["input"] == "Capital of France?"


def test_single_turn_user_multiple_text_blocks_concatenated():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [
                    {"text": "First."},
                    {"text": "Second."},
                ]}],
            ))
    assert captured["kwargs"]["input"] == "First.\n\nSecond."


def test_multi_turn_user_assistant_user_becomes_typed_item_list():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[
                    {"role": "user", "content": [{"text": "A"}]},
                    {"role": "assistant", "content": [{"text": "B"}]},
                    {"role": "user", "content": [{"text": "C"}]},
                ],
            ))
    items = captured["kwargs"]["input"]
    assert isinstance(items, list)
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "A"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "B"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "C"}]},
    ]


# ============================================================================
# chunk 04: image content blocks
# ============================================================================


def test_user_image_block_translates_to_input_image_data_url():
    fake_client, captured = _capture_kwargs()
    raw_bytes = b"\x89PNG\r\n\x1a\n\x00\x00"
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [
                    {"text": "what is this?"},
                    {"image": {"format": "png", "source": {"bytes": raw_bytes}}},
                ]}],
            ))
    items = captured["kwargs"]["input"]
    assert isinstance(items, list)
    user_content = items[0]["content"]
    assert user_content[0] == {"type": "input_text", "text": "what is this?"}
    expected_b64 = base64.b64encode(raw_bytes).decode("ascii")
    assert user_content[1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{expected_b64}",
    }


def test_user_image_block_unknown_format_raises_translation_error():
    fake_client = MagicMock()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                _responses_backend.invoke(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [
                        {"image": {"format": "tiff", "source": {"bytes": b"x"}}},
                    ]}],
                ))


# ============================================================================
# chunk 04: stop_sequences dropped with warning
# ============================================================================


def test_stop_sequences_dropped_with_warning(caplog):
    import logging
    caplog.set_level(logging.WARNING)
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "x"}]}],
                stop_sequences=["STOP"],
            ))
    assert "stop" not in captured["kwargs"]
    assert "stop_output_tokens" not in captured["kwargs"]
    assert any(
        "stop_sequences" in r.message.lower() or "stop_sequences" in r.getMessage()
        for r in caplog.records
    )


def test_top_p_dropped_when_set():
    # GPT-5 family on Mantle rejects ``top_p`` (locked at 1.0
    # server-side). Pin that the backend drops it even when the
    # caller sets it. Pre-2026-06-18 this test verified the opposite
    # ("passed when set") -- live-burned that day on the first
    # entitled call against the openai workspace.
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "x"}]}],
                top_p=0.9,
            ))
    assert "top_p" not in captured["kwargs"]


def test_temperature_dropped_when_set():
    # Same shape as test_top_p_dropped_when_set. The caller-facing
    # Request model still accepts ``temperature`` so the rest of the
    # platform (which prefers 0.0 for determinism) doesn't need to
    # know which backend's running -- the dispatcher drops it
    # silently for Responses.
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "x"}]}],
                temperature=0.0,
            ))
    assert "temperature" not in captured["kwargs"]


# ============================================================================
# chunk 06: tools= request translation
# ============================================================================


def test_tools_translate_to_top_level_function_tool_list():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "weather?"}]}],
                tools=[ToolSpec(
                    name="get_weather",
                    description="Get current weather",
                    input_schema={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                )],
            ))
    tools = captured["kwargs"]["tools"]
    assert tools == [{
        "type": "function",
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }]


def test_tools_none_omits_tools_kwarg():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            ))
    assert "tools" not in captured["kwargs"]


# ============================================================================
# chunk 06: toolUse history -> function_call input items
# ============================================================================


def test_assistant_toolUse_history_becomes_function_call_input_item():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[
                    {"role": "user", "content": [{"text": "weather in SF?"}]},
                    {"role": "assistant", "content": [
                        {"text": "Let me check."},
                        {"toolUse": {
                            "toolUseId": "call_abc",
                            "name": "get_weather",
                            "input": {"city": "SF"},
                        }},
                    ]},
                    {"role": "user", "content": [{"toolResult": {
                        "toolUseId": "call_abc",
                        "content": [{"text": "72F sunny"}],
                    }}]},
                ],
            ))
    items = captured["kwargs"]["input"]
    # Expect: user input msg, assistant text msg, function_call item,
    # function_call_output item
    assert isinstance(items, list)
    assert items[0]["role"] == "user"
    assert items[1]["role"] == "assistant"
    assert items[1]["content"] == [{"type": "output_text", "text": "Let me check."}]
    assert items[2] == {
        "type": "function_call",
        "call_id": "call_abc",
        "name": "get_weather",
        "arguments": json.dumps({"city": "SF"}),
    }
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": "72F sunny",
    }


def test_tool_result_error_status_prepends_error_marker():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[
                    {"role": "user", "content": [{"text": "x"}]},
                    {"role": "assistant", "content": [{"toolUse": {
                        "toolUseId": "c1", "name": "n", "input": {},
                    }}]},
                    {"role": "user", "content": [{"toolResult": {
                        "toolUseId": "c1",
                        "content": [{"text": "city unknown"}],
                        "status": "error",
                    }}]},
                ],
            ))
    items = captured["kwargs"]["input"]
    fco = next(it for it in items if it.get("type") == "function_call_output")
    assert fco["output"] == "[error] city unknown"


def test_toolResult_mixed_with_text_in_same_user_message_raises():
    fake_client = MagicMock()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                _responses_backend.invoke(Request(
                    model_id="openai.gpt-5.5",
                    messages=[
                        {"role": "user", "content": [{"text": "hi"}]},
                        {"role": "assistant", "content": [{"toolUse": {
                            "toolUseId": "c1", "name": "n", "input": {},
                        }}]},
                        {"role": "user", "content": [
                            {"toolResult": {"toolUseId": "c1", "content": [{"text": "r"}]}},
                            {"text": "and one more thing"},
                        ]},
                    ],
                ))


def test_parallel_tool_use_in_one_assistant_message_emits_two_function_call_items():
    fake_client, captured = _capture_kwargs()
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[
                    {"role": "user", "content": [{"text": "weather in SF and LA?"}]},
                    {"role": "assistant", "content": [
                        {"toolUse": {
                            "toolUseId": "c1", "name": "get_weather",
                            "input": {"city": "SF"},
                        }},
                        {"toolUse": {
                            "toolUseId": "c2", "name": "get_weather",
                            "input": {"city": "LA"},
                        }},
                    ]},
                    {"role": "user", "content": [
                        {"toolResult": {"toolUseId": "c1", "content": [{"text": "72F"}]}},
                    ]},
                    {"role": "user", "content": [
                        {"toolResult": {"toolUseId": "c2", "content": [{"text": "80F"}]}},
                    ]},
                ],
            ))
    items = captured["kwargs"]["input"]
    fc_items = [it for it in items if it.get("type") == "function_call"]
    assert len(fc_items) == 2
    assert {fc["call_id"] for fc in fc_items} == {"c1", "c2"}


# ============================================================================
# chunk 05: function_call output item -> tool_calls + content_blocks
# ============================================================================


def test_invoke_function_call_output_item_populates_tool_calls_and_content_blocks():
    fake_client = MagicMock()
    fake_client.responses.create.return_value = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=20, output_tokens=5),
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="Let me check.")],
            ),
            SimpleNamespace(
                type="function_call",
                call_id="call_xyz",
                name="get_weather",
                arguments='{"city": "SF"}',
            ),
        ],
    )
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            resp = _responses_backend.invoke(Request(
                model_id="openai.gpt-5.5",
                messages=[{"role": "user", "content": [{"text": "weather?"}]}],
            ))
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.text == "Let me check."
    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].tool_use_id == "call_xyz"
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].input == {"city": "SF"}
    # Converse-shaped toolUse block included in content_blocks too
    tu_blocks = [b for b in resp.content_blocks if "toolUse" in b]
    assert len(tu_blocks) == 1
    assert tu_blocks[0]["toolUse"]["toolUseId"] == "call_xyz"


def test_invoke_function_call_invalid_arguments_json_raises():
    fake_client = MagicMock()
    fake_client.responses.create.return_value = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        output=[SimpleNamespace(
            type="function_call",
            call_id="call_x",
            name="n",
            arguments="not-json",
        )],
    )
    with _ssm_returns("k"):
        with _mock_openai(fake_client):
            with pytest.raises(TranslationError):
                _responses_backend.invoke(Request(
                    model_id="openai.gpt-5.5",
                    messages=[{"role": "user", "content": [{"text": "x"}]}],
                ))
