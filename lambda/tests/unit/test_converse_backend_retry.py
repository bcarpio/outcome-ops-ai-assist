# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for fable-scale chunk 05 retry semantics in _converse_backend.

Covers:
- _is_retryable classification of error codes.
- _next_backoff_ms bounds + jitter.
- invoke_with_retry retries transient ClientError, gives up after
  max_attempts, re-raises non-retryable immediately.
- open_stream_with_retry same semantics; invokes on_retry callback with
  attempt + delay_ms.
"""
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _load():
    if "outcomeops_converse_backend" in sys.modules:
        del sys.modules["outcomeops_converse_backend"]
    lambda_root = os.path.join(os.path.dirname(__file__), "../..")
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    return importlib.import_module("shared._converse_backend")


mod = _load()


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "test"}},
        operation_name="Converse",
    )


class TestIsRetryable:
    def test_throttling_is_retryable(self):
        assert mod._is_retryable(_client_error("ThrottlingException")) is True

    def test_service_unavailable_is_retryable(self):
        assert mod._is_retryable(_client_error("ServiceUnavailableException")) is True

    def test_model_stream_error_is_retryable(self):
        assert mod._is_retryable(_client_error("ModelStreamErrorException")) is True

    def test_validation_exception_not_retryable(self):
        assert mod._is_retryable(_client_error("ValidationException")) is False

    def test_model_error_not_retryable(self):
        # Client-side model errors are NOT retryable (bad tool schema etc).
        assert mod._is_retryable(_client_error("ModelErrorException")) is False

    def test_non_client_error_not_retryable(self):
        assert mod._is_retryable(RuntimeError("boom")) is False


class TestBackoff:
    def test_first_attempt_capped_at_base(self):
        # attempt=1: ceiling = base * 2^0 = base
        for _ in range(10):
            delay = mod._next_backoff_ms(1, base_ms=500)
            assert 0 <= delay <= 500

    def test_later_attempts_grow_but_capped(self):
        # attempt=10 blows past cap; should still be bounded.
        for _ in range(10):
            delay = mod._next_backoff_ms(10, base_ms=500, cap_ms=8000)
            assert 0 <= delay <= 8000

    def test_jitter_produces_different_values(self):
        # Full jitter -> unlikely to see the same value twice in a row for
        # a decent-sized attempt.
        vals = {mod._next_backoff_ms(5) for _ in range(20)}
        # Very high confidence more than one distinct value emerged.
        assert len(vals) > 1


class TestInvokeWithRetry:
    def test_successful_call_returns_immediately(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
        mod = _load()
        with patch.object(mod, "invoke", return_value=MagicMock(text="ok")) as m:
            resp = mod.invoke_with_retry(MagicMock())
            assert resp.text == "ok"
            assert m.call_count == 1

    def test_retries_transient_error_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
        mod = _load()
        errs = [
            _client_error("ThrottlingException"),
            _client_error("ThrottlingException"),
        ]
        ok = MagicMock(text="ok")
        with patch.object(mod, "invoke", side_effect=[*errs, ok]) as m, \
             patch.object(mod.time, "sleep"):
            resp = mod.invoke_with_retry(MagicMock())
            assert resp.text == "ok"
            assert m.call_count == 3

    def test_gives_up_after_max_attempts(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
        mod = _load()
        with patch.object(mod, "invoke", side_effect=_client_error("ThrottlingException")) as m, \
             patch.object(mod.time, "sleep"):
            with pytest.raises(ClientError) as exc_info:
                mod.invoke_with_retry(MagicMock())
            assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"
            assert m.call_count == 3

    def test_non_retryable_raises_immediately(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "5")
        mod = _load()
        with patch.object(mod, "invoke", side_effect=_client_error("ValidationException")) as m:
            with pytest.raises(ClientError) as exc_info:
                mod.invoke_with_retry(MagicMock())
            assert exc_info.value.response["Error"]["Code"] == "ValidationException"
            assert m.call_count == 1  # no retries


class TestOpenStreamWithRetry:
    def test_invokes_on_retry_callback(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
        mod = _load()

        # Mock the boto client — first call throws throttle, second returns
        # a stream shape ({"stream": <stream>}).
        client = MagicMock()
        fake_stream = object()
        client.converse_stream.side_effect = [
            _client_error("ThrottlingException"),
            {"stream": fake_stream},
        ]
        seen_retries = []

        def on_retry(attempt, delay_ms):
            seen_retries.append((attempt, delay_ms))

        with patch.object(mod, "_get_client", return_value=client), \
             patch.object(mod.time, "sleep"), \
             patch.object(mod, "_build_converse_kwargs", return_value={}):
            stream = mod.open_stream_with_retry(MagicMock(model_id="m"), on_retry=on_retry)
        assert stream is fake_stream
        assert len(seen_retries) == 1  # only one retry needed
        attempt, delay_ms = seen_retries[0]
        assert attempt == 1  # after first failure, before second attempt
        assert delay_ms >= 0

    def test_callback_exception_does_not_break_retry(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
        mod = _load()
        client = MagicMock()
        fake_stream = object()
        client.converse_stream.side_effect = [
            _client_error("ThrottlingException"),
            {"stream": fake_stream},
        ]

        def bad_on_retry(a, d):
            raise RuntimeError("callback broken")

        with patch.object(mod, "_get_client", return_value=client), \
             patch.object(mod.time, "sleep"), \
             patch.object(mod, "_build_converse_kwargs", return_value={}):
            stream = mod.open_stream_with_retry(MagicMock(model_id="m"), on_retry=bad_on_retry)
        assert stream is fake_stream

    def test_gives_up_after_max_attempts(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "2")
        mod = _load()
        client = MagicMock()
        client.converse_stream.side_effect = _client_error("ThrottlingException")

        with patch.object(mod, "_get_client", return_value=client), \
             patch.object(mod.time, "sleep"), \
             patch.object(mod, "_build_converse_kwargs", return_value={}):
            with pytest.raises(ClientError):
                mod.open_stream_with_retry(MagicMock(model_id="m"))
        assert client.converse_stream.call_count == 2


class TestPromptCache:
    """fable-scale chunk 10: BEDROCK_PROMPT_CACHE_ENABLED gates a
    Converse cachePoint appended to the system-prompt block."""

    def test_cache_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_PROMPT_CACHE_ENABLED", raising=False)
        mod = _load()
        req = MagicMock()
        req.model_id = "test-model"
        req.messages = []
        req.system = "You are a test."
        req.max_tokens = 100
        req.temperature = 0.0
        req.top_p = None
        req.stop_sequences = None
        req.tools = None
        kwargs = mod._build_converse_kwargs(req)
        # No cachePoint block appended.
        assert kwargs["system"] == [{"text": "You are a test."}]

    def test_cache_enabled_appends_cache_point(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_PROMPT_CACHE_ENABLED", "true")
        mod = _load()
        req = MagicMock()
        req.model_id = "test-model"
        req.messages = []
        req.system = "You are a test."
        req.max_tokens = 100
        req.temperature = 0.0
        req.top_p = None
        req.stop_sequences = None
        req.tools = None
        kwargs = mod._build_converse_kwargs(req)
        # cachePoint appended AFTER the system text block.
        assert kwargs["system"] == [
            {"text": "You are a test."},
            {"cachePoint": {"type": "default"}},
        ]

    def test_cache_enabled_multi_system_appends_once(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_PROMPT_CACHE_ENABLED", "true")
        mod = _load()
        req = MagicMock()
        req.model_id = "test-model"
        req.messages = []
        req.system = ["A", "B"]
        req.max_tokens = 100
        req.temperature = 0.0
        req.top_p = None
        req.stop_sequences = None
        req.tools = None
        kwargs = mod._build_converse_kwargs(req)
        assert kwargs["system"] == [
            {"text": "A"},
            {"text": "B"},
            {"cachePoint": {"type": "default"}},
        ]
