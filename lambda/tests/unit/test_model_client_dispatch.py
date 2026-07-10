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


def test_dispatch_unknown_prefix_raises_unsupported_model_error(monkeypatch):
    # When BEDROCK_DEFAULT_BACKEND is unset (the prd posture: no silent
    # fallback per ADR-035 rationale), unknown prefixes raise.
    monkeypatch.delenv("BEDROCK_DEFAULT_BACKEND", raising=False)
    with pytest.raises(UnsupportedModelError):
        model_client._resolve_backend("cohere.command-r-plus")
    with pytest.raises(UnsupportedModelError):
        model_client._resolve_backend("qwen.qwen3-coder-480b-a35b-instruct")
    with pytest.raises(UnsupportedModelError):
        model_client._resolve_backend("deepseek.v3.2")


def test_dispatch_empty_model_id_raises_unsupported_model_error():
    with pytest.raises(UnsupportedModelError):
        model_client._resolve_backend("")


# ---------------------------------------------------------------------------
# Per-deploy default backend fallback (BEDROCK_DEFAULT_BACKEND)
# ---------------------------------------------------------------------------


class TestDefaultBackendFallback:
    """The openai workspace runs Mantle exclusively -- qwen / deepseek /
    mistral / moonshotai etc. all live behind the same Responses-API
    endpoint as gpt-5.*. Hard-coding every Mantle family in _DISPATCH
    became dead weight as soon as the Mantle catalog grew, so we added
    a per-deploy ``BEDROCK_DEFAULT_BACKEND`` env var. Unset -> raise
    (prd's posture, unchanged). Set -> route unknown families to that
    backend (the openai workspace's posture)."""

    @pytest.mark.parametrize("unknown_model_id", [
        "qwen.qwen3-coder-480b-a35b-instruct",
        "deepseek.v3.2",
        "mistral.mistral-large-3-675b-instruct",
        "moonshotai.kimi-k2-thinking",
        "zai.glm-4.7",
        "minimax.minimax-m2",
        "cohere.command-r-plus",  # not on Mantle either; default still kicks in
    ])
    def test_responses_default_routes_unknown_to_mantle(
        self, monkeypatch, unknown_model_id,
    ):
        monkeypatch.setenv("BEDROCK_DEFAULT_BACKEND", "responses")
        assert model_client._resolve_backend(unknown_model_id) == "responses"

    def test_converse_default_routes_unknown_to_bedrock(self, monkeypatch):
        # Documents the symmetric case even though it's not a real
        # deploy posture today -- if someone wanted a Bedrock-Converse-
        # only deploy and to default Mantle catalog families to
        # converse (they wouldn't work, but the dispatch would honor it).
        monkeypatch.setenv("BEDROCK_DEFAULT_BACKEND", "converse")
        assert model_client._resolve_backend("qwen.qwen3-coder-x") == "converse"

    def test_explicit_dispatch_wins_over_default(self, monkeypatch):
        # openai.gpt-oss-* is the load-bearing case: it lives on
        # Bedrock Converse (not Mantle) even though its prefix sits
        # inside openai.*. The explicit row in _DISPATCH must beat
        # the default even when the default would have sent it the
        # wrong way.
        monkeypatch.setenv("BEDROCK_DEFAULT_BACKEND", "responses")
        assert model_client._resolve_backend("openai.gpt-oss-120b-1:0") == "converse"
        # And the gpt-5* path stays on responses (same as default here).
        assert model_client._resolve_backend("openai.gpt-5-5:0") == "responses"

    def test_invalid_default_value_raises_backend_config_error(self, monkeypatch):
        from shared.model_client_types import BackendConfigError
        monkeypatch.setenv("BEDROCK_DEFAULT_BACKEND", "bogus")
        with pytest.raises(BackendConfigError):
            model_client._resolve_backend("qwen.qwen3-coder-x")

    def test_empty_string_default_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_DEFAULT_BACKEND", "")
        with pytest.raises(UnsupportedModelError):
            model_client._resolve_backend("qwen.qwen3-coder-x")

    def test_whitespace_default_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_DEFAULT_BACKEND", "   ")
        with pytest.raises(UnsupportedModelError):
            model_client._resolve_backend("qwen.qwen3-coder-x")


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


def test_invoke_unknown_model_id_raises_unsupported_before_backend_call():
    with pytest.raises(UnsupportedModelError):
        model_client.invoke(
            model_id="cohere.command-r-plus",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )


def test_invoke_stream_routes_to_correct_backend_and_yields():
    from shared.model_client_types import Usage

    def fake_stream(req, on_retry=None):
        # fable-scale chunk 05: invoke_stream now accepts an on_retry
        # callback for stream-init backoff notifications.
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


# ---------------------------------------------------------------------------
# ADR-016 v1.2 -- workload-aware profile ARN resolution (chunk 03)
# ---------------------------------------------------------------------------


class TestApplicationInferenceProfileArnBackendDispatch:
    """Profile ARNs must route to converse (Anthropic-only wrapping)."""

    def test_profile_arn_routes_to_converse(self):
        arn = (
            "arn:aws:bedrock:us-west-2:123456789012:inference-profile/"
            "abc123def456"
        )
        assert model_client._resolve_backend(arn) == "converse"

    def test_profile_arn_routes_to_converse_regardless_of_region(self):
        assert (
            model_client._resolve_backend(
                "arn:aws:bedrock:us-east-2:123456789012:inference-profile/xyz"
            )
            == "converse"
        )

    def test_non_profile_arn_still_hits_prefix_dispatch(self):
        # A foundation-model ARN (no /inference-profile/ segment) should
        # NOT get the profile shortcut -- it would silently misroute.
        with pytest.raises(UnsupportedModelError):
            model_client._resolve_backend(
                "arn:aws:bedrock:us-west-2::foundation-model/some-mystery-model"
            )

    def test_application_inference_profile_arn_routes_to_converse(self):
        # Regression: application profile ARNs have resource type
        # ":application-inference-profile/", not ":inference-profile/".
        # The initial chunk 03 check anchored on ":inference-profile/"
        # (leading colon) and missed application profiles because their
        # separator is "-", not ":". Landed a real "No backend
        # registered" DLQ from process-repo-summary against the
        # code-maps-sonnet profile ARN in prd 2026-07-08.
        arn = (
            "arn:aws:bedrock:us-west-2:123456789012:"
            "application-inference-profile/182i9hm83wov"
        )
        assert model_client._resolve_backend(arn) == "converse"


class TestProfileArnEnvVarResolution:
    """resolve_profile_arn maps (workload, tier) to env var value."""

    def test_env_var_name_shape(self):
        # sanity: the private helper name shape is what terraform will
        # write. If this changes, terraform/bedrock-inference-profiles.tf
        # must change too.
        assert (
            model_client._profile_env_var_name("chat", "advanced")
            == "BEDROCK_CHAT_ADVANCED_PROFILE_ARN"
        )
        assert (
            model_client._profile_env_var_name("code-maps", "basic")
            == "BEDROCK_CODE_MAPS_BASIC_PROFILE_ARN"
        )
        assert (
            model_client._profile_env_var_name("chat", "titan-embed")
            == "BEDROCK_CHAT_TITAN_EMBED_PROFILE_ARN"
        )
        assert (
            model_client._profile_env_var_name("chat", "cohere-rerank")
            == "BEDROCK_CHAT_COHERE_RERANK_PROFILE_ARN"
        )

    def test_resolves_when_env_var_set(self, monkeypatch):
        monkeypatch.setenv(
            "BEDROCK_CHAT_ADVANCED_PROFILE_ARN",
            "arn:aws:bedrock:us-west-2:1:inference-profile/x",
        )
        assert (
            model_client.resolve_profile_arn("chat", "advanced")
            == "arn:aws:bedrock:us-west-2:1:inference-profile/x"
        )

    def test_returns_none_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv(
            "BEDROCK_CODE_MAPS_BASIC_PROFILE_ARN", raising=False,
        )
        assert model_client.resolve_profile_arn("code-maps", "basic") is None

    def test_unknown_workload_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown workload"):
            model_client.resolve_profile_arn("marketing", "advanced")

    def test_unknown_tier_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            model_client.resolve_profile_arn("chat", "premium")


class TestInvokeWithWorkloadTier:
    """The workload+tier dispatch path (chunk 03 API)."""

    def _fake_converse(self, captured):
        def fake_invoke(req):
            captured["model_id"] = req.model_id
            from shared.model_client_types import Response, StopReason, Usage
            return Response(
                text="ok", content_blocks=[{"text": "ok"}],
                usage=Usage(input_tokens=1, output_tokens=1),
                stop_reason=StopReason.END_TURN,
            )
        return fake_invoke

    def test_workload_tier_uses_profile_arn_env(self, monkeypatch):
        profile_arn = "arn:aws:bedrock:us-west-2:1:inference-profile/chat-adv"
        monkeypatch.setenv("BEDROCK_CHAT_ADVANCED_PROFILE_ARN", profile_arn)
        captured = {}
        with patch(
            "shared._converse_backend.invoke",
            side_effect=self._fake_converse(captured),
        ):
            model_client.invoke(
                workload="chat", tier="advanced",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )
        assert captured["model_id"] == profile_arn

    def test_workload_tier_falls_back_to_legacy_model_id_env(
        self, monkeypatch, caplog,
    ):
        # Post-chunk-04 rollout in flight: env var isn't wired for this
        # Lambda yet, so we fall back to BEDROCK_ADVANCED_MODEL_ID with
        # a WARN log so the attribution gap is visible in CloudWatch.
        monkeypatch.delenv("BEDROCK_CODE_MAPS_ADVANCED_PROFILE_ARN", raising=False)
        monkeypatch.setenv("BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        captured = {}
        import logging
        with caplog.at_level(logging.WARNING):
            with patch(
                "shared._converse_backend.invoke",
                side_effect=self._fake_converse(captured),
            ):
                model_client.invoke(
                    workload="code-maps", tier="advanced",
                    messages=[{"role": "user", "content": [{"text": "hi"}]}],
                )
        assert captured["model_id"] == "us.anthropic.claude-sonnet-4-6"
        assert any(
            "falling back to untagged" in rec.message
            for rec in caplog.records
        )

    def test_workload_tier_neither_env_raises_backend_config_error(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "BEDROCK_PR_ANALYZER_BASIC_PROFILE_ARN", raising=False,
        )
        monkeypatch.delenv("BEDROCK_BASIC_MODEL_ID", raising=False)
        with pytest.raises(BackendConfigError):
            model_client.invoke(
                workload="pr-analyzer", tier="basic",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )

    def test_workload_without_tier_raises_value_error(self):
        with pytest.raises(ValueError, match="workload and tier"):
            model_client.invoke(
                workload="chat",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )

    def test_tier_without_workload_raises_value_error(self):
        with pytest.raises(ValueError, match="workload and tier"):
            model_client.invoke(
                tier="advanced",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )

    def test_neither_workload_nor_model_id_raises_value_error(self):
        with pytest.raises(ValueError, match="Either model_id"):
            model_client.invoke(
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )

    def test_legacy_model_id_path_still_works(self):
        captured = {}
        with patch(
            "shared._converse_backend.invoke",
            side_effect=self._fake_converse(captured),
        ):
            model_client.invoke(
                model_id="us.anthropic.claude-sonnet-4-6",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
            )
        assert captured["model_id"] == "us.anthropic.claude-sonnet-4-6"
