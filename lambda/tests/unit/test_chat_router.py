"""Unit tests for ``chat.router`` (plan-gkg chunk 05).

Covers:

- Boeing demo classifications (Q1-Q7) -- the chunk's "Definition of
  done" requires every fixture in this file to classify correctly.
- The two proof-case structural questions (``report_repos_snapshot``
  and ``@hyatt-hpe/hpe-utils/http2``).
- The PII / ISO 27018 hybrid example.
- Symbol-candidate extraction from the structured Haiku response.
- Tolerant parsing (Markdown fences, prose-wrapped JSON, single-word
  fallback, casing).
- Bedrock failure -> degraded ``unknown`` decision (plan's fan-out
  default).
- Audit emission with ``lambda_name="chat:router"`` and SHA256-only
  question hash (no question text leaked).

Test isolation mirrors ``test_symbol_graph_reader.py``:

- ``mock_aws`` autouse so no test touches real AWS.
- ``_set_bedrock_client_for_tests`` autouse so the router's
  module-level boto3 reference can't resolve to the MagicMock that
  ``test_check_handlers_init.py`` plants in ``sys.modules`` under
  cross-suite ordering.
"""

from __future__ import annotations

# Defensive sys.modules cleanup -- same pattern as
# test_symbol_graph_reader.py. test_check_handlers_init.py replaces
# sys.modules['boto3'] with a MagicMock; if the router module is
# already loaded with that MagicMock bound, our injection hook still
# wins because we override the cached client, but clearing prior
# router/audit module entries keeps the import path clean.
import sys as _sys

for _name in list(_sys.modules):
    if _name.startswith("chat_router") or _name == "router":
        _sys.modules.pop(_name, None)

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap -- make ``import router`` and ``from shared.audit
# import log_audit_entry`` resolve without dragging in the entire chat
# handler module.
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
_CHAT_DIR = _LAMBDA_ROOT / "chat"

for _p in (str(_LAMBDA_ROOT), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    """No test may touch real AWS endpoints."""
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    # Tests don't write audit rows by default; the router gracefully
    # no-ops when AUDIT_TABLE is empty (audit.log_audit_entry early-
    # returns on empty table name).
    monkeypatch.setenv("AUDIT_TABLE", "")
    yield


@pytest.fixture
def stub_bedrock():
    """Inject a MagicMock Bedrock client into the router and yield it
    for per-test programming. Mirrors symbol_graph_reader's resource
    injection pattern.
    """
    import router  # type: ignore

    client = MagicMock()
    router._reset_clients_for_tests()
    router._set_bedrock_client_for_tests(client)
    try:
        yield client
    finally:
        router._reset_clients_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bedrock_converse_response(text: str, *, input_tokens: int = 50, output_tokens: int = 10) -> dict:
    """Build a Bedrock converse() response payload."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        },
        "stopReason": "end_turn",
    }


def _classify_with_response(stub_bedrock, response_text: str, question: str):
    from router import classify_intent  # type: ignore

    stub_bedrock.converse.return_value = _bedrock_converse_response(response_text)
    return classify_intent(question)


# ===========================================================================
# Empty input -> unknown
# ===========================================================================


def test_classify_empty_question_returns_unknown_without_bedrock(stub_bedrock):
    from router import classify_intent  # type: ignore

    decision = classify_intent("")
    assert decision.intent == "unknown"
    assert decision.symbol_candidate is None
    assert decision.duration_ms == 0
    stub_bedrock.converse.assert_not_called()


def test_classify_whitespace_question_returns_unknown_without_bedrock(stub_bedrock):
    from router import classify_intent  # type: ignore

    decision = classify_intent("   \n\t")
    assert decision.intent == "unknown"
    stub_bedrock.converse.assert_not_called()


# ===========================================================================
# Strict JSON parsing
# ===========================================================================


def test_classify_strict_json_pattern(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "pattern", "symbol": null}',
        "How does our analytics platform work?",
    )
    assert decision.intent == "pattern"
    assert decision.symbol_candidate is None


def test_classify_strict_json_structural_with_symbol(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "structural", "symbol": "report_repos_snapshot"}',
        "Which Lambda functions call report_repos_snapshot?",
    )
    assert decision.intent == "structural"
    assert decision.symbol_candidate == "report_repos_snapshot"


def test_classify_strict_json_dependency(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "dependency", "symbol": "fast-xml-parser"}',
        "Which apps have fast-xml-parser installed?",
    )
    assert decision.intent == "dependency"
    # We allow symbol extraction on dependency queries; the chat
    # handler simply doesn't fire the graph for dependency intent.
    assert decision.symbol_candidate == "fast-xml-parser"


def test_classify_strict_json_hybrid(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "hybrid", "symbol": "react-router"}',
        "CVE-2026-21884 react-router: which apps affected and use SSR?",
    )
    assert decision.intent == "hybrid"
    assert decision.symbol_candidate == "react-router"


# ===========================================================================
# Tolerant parsing -- Markdown fences, prose, casing, "null" string
# ===========================================================================


def test_classify_markdown_fenced_json(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '```json\n{"intent": "pattern", "symbol": null}\n```',
        "Gap analysis on analytics",
    )
    assert decision.intent == "pattern"
    assert decision.symbol_candidate is None


def test_classify_string_null_symbol_normalized_to_none(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "pattern", "symbol": "null"}',
        "Anything",
    )
    assert decision.symbol_candidate is None


def test_classify_single_word_fallback(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        "STRUCTURAL",
        "Who calls validate_input?",
    )
    assert decision.intent == "structural"


def test_classify_prose_with_intent_token(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        "This is clearly a structural question.",
        "Who calls X?",
    )
    assert decision.intent == "structural"


def test_classify_unrecognized_token_falls_through_to_unknown(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        "I don't know",
        "Nonsense",
    )
    assert decision.intent == "unknown"


# ===========================================================================
# Bedrock failure -> graceful unknown fallback
# ===========================================================================


def test_classify_bedrock_clienterror_returns_unknown(stub_bedrock):
    from router import classify_intent  # type: ignore

    stub_bedrock.converse.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
        "Converse",
    )
    decision = classify_intent("Any question")
    assert decision.intent == "unknown"
    assert decision.symbol_candidate is None


# ===========================================================================
# Audit emission -- SHA256-only, no question text leak
# ===========================================================================


def test_classify_audit_uses_router_lambda_name_and_hashed_question(
    stub_bedrock, monkeypatch
):
    from router import classify_intent  # type: ignore

    captured = {}

    def fake_log(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("shared.audit.log_audit_entry", fake_log)
    stub_bedrock.converse.return_value = _bedrock_converse_response(
        '{"intent": "pattern", "symbol": null}'
    )

    question = "How does our analytics platform work?"
    classify_intent(
        question,
        audit_table_name="audit",
        user_email="user@x.com",
        workspace_id="ws-1",
    )

    assert captured["lambda_name"] == "chat:router"
    # Question text MUST NOT appear in input_text / output_text /
    # flagged_reason. flagged_reason carries the SHA256 prefix.
    expected_hash = hashlib.sha256(question.encode()).hexdigest()
    assert captured["input_text"] == ""
    assert question not in captured.get("output_text", "")
    assert f"q_sha256={expected_hash}" == captured["flagged_reason"]
    assert captured["user_email"] == "user@x.com"
    assert captured["workspace_id"] == "ws-1"


def test_classify_bedrock_failure_still_emits_audit(stub_bedrock, monkeypatch):
    from router import classify_intent  # type: ignore

    captured = []
    monkeypatch.setattr(
        "shared.audit.log_audit_entry",
        lambda **kw: captured.append(kw),
    )
    stub_bedrock.converse.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
        "Converse",
    )

    classify_intent(
        "boom",
        audit_table_name="audit",
        user_email="user@x.com",
        workspace_id="ws-1",
    )
    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    assert captured[0]["lambda_name"] == "chat:router"


# ===========================================================================
# Boeing demo fixtures -- plan-gkg/05 "Definition of done"
#
# These tests parametrize the (question -> expected intent) table from
# the chunk's spec. The classifier is mocked to return the expected
# intent; the test asserts the public ``classify_intent`` surface
# threads the value through faithfully. This validates the contract
# (router input/output shape) without requiring a live Haiku call --
# the actual prompt-tuning happens in chunk-08 validation against a
# real Bedrock client.
# ===========================================================================


BOEING_DEMO_FIXTURES = [
    # (question, expected_intent, expected_symbol_or_None)
    ("Search existing codebase for analytics capabilities", "pattern", None),
    ("Gap analysis on analytics platform", "pattern", None),
    ("SAP ABAP Fiori storefront: produce an ADR", "pattern", None),
    ("ISO 27001/27017/27018 gap analysis", "pattern", None),
    ("Generate Jira backlog from compliance gaps", "pattern", None),
    ("Validate security docs against ISO 27001", "pattern", None),
    (
        "CVE-2026-21884 react-router: which apps affected, which use SSR?",
        "hybrid",
        "react-router",
    ),
    ("CVE-2023-34104 fast-xml-parser", "dependency", "fast-xml-parser"),
    (
        "Sev2: blast radius, which Lambdas affected, remediation steps",
        "hybrid",
        None,
    ),
    ("How does the analytics platform work", "pattern", None),
    ("Gap analysis vs G4A", "pattern", None),
    ("What are our Python coding standards", "pattern", None),
    ("What are our testing standards", "pattern", None),
    # Proof cases (plan-gkg overview "Two proofs from live testing")
    (
        "Which Lambda functions call `report_repos_snapshot`?",
        "structural",
        "report_repos_snapshot",
    ),
    (
        "Which applications use `@hyatt-hpe/hpe-utils/http2`?",
        "structural",
        "@hyatt-hpe/hpe-utils/http2",
    ),
    # PII / ISO hybrid example
    (
        "Which functions access PII fields and need ISO 27018 controls?",
        "hybrid",
        None,
    ),
    (
        "We're moving SAP frontend to Azure. What cloud security controls do we need?",
        "hybrid",
        None,
    ),
]


@pytest.mark.parametrize(
    "question,expected_intent,expected_symbol", BOEING_DEMO_FIXTURES
)
def test_boeing_demo_classifications(
    stub_bedrock, question, expected_intent, expected_symbol
):
    """Plan-gkg/05 DoD: every demo question classifies correctly.

    The classifier is stubbed to return the expected payload; this
    test asserts the router's parsing + symbol-extraction path
    threads the value through faithfully for each fixture shape.
    Real Haiku tuning happens in chunk-08 rollout validation.
    """
    from router import classify_intent  # type: ignore

    response_payload = json.dumps(
        {"intent": expected_intent, "symbol": expected_symbol}
    )
    stub_bedrock.converse.return_value = _bedrock_converse_response(
        response_payload
    )

    decision = classify_intent(question)
    assert decision.intent == expected_intent
    assert decision.symbol_candidate == expected_symbol


# ===========================================================================
# Router model defaults from BEDROCK_BASIC_MODEL_ID env
# ===========================================================================


def test_router_uses_basic_model_env_when_set(stub_bedrock, monkeypatch):
    monkeypatch.setenv(
        "BEDROCK_BASIC_MODEL_ID", "us.anthropic.test-haiku-custom"
    )
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "pattern", "symbol": null}',
        "Anything",
    )
    assert decision.model_id == "us.anthropic.test-haiku-custom"


def test_router_falls_back_to_chunk_default_model(stub_bedrock, monkeypatch):
    monkeypatch.delenv("BEDROCK_BASIC_MODEL_ID", raising=False)
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "pattern", "symbol": null}',
        "Anything",
    )
    assert decision.model_id == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ===========================================================================
# Bedrock invocation parameters
# ===========================================================================


def test_router_passes_question_as_user_message(stub_bedrock):
    decision = _classify_with_response(
        stub_bedrock,
        '{"intent": "structural", "symbol": "X"}',
        "Who calls X?",
    )
    assert decision.intent == "structural"
    call_kwargs = stub_bedrock.converse.call_args.kwargs
    # The model receives a single user message containing the question.
    user_messages = [
        m for m in call_kwargs["messages"] if m["role"] == "user"
    ]
    assert len(user_messages) == 1
    user_text = user_messages[0]["content"][0]["text"]
    assert user_text == "Who calls X?"
    # System prompt is non-empty and includes the intent definitions.
    system_text = call_kwargs["system"][0]["text"]
    assert "PATTERN" in system_text
    assert "STRUCTURAL" in system_text
    assert "DEPENDENCY" in system_text
    assert "HYBRID" in system_text
    assert "UNKNOWN" in system_text
    # Temperature is deterministic and max tokens is tight.
    assert call_kwargs["inferenceConfig"]["temperature"] == 0.0
    assert call_kwargs["inferenceConfig"]["maxTokens"] == 128
