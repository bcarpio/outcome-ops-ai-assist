# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared/audit.py module."""

import importlib.util
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()


def _load_audit_module():
    """Load or reload the shared audit module from its exact file path."""
    if "shared_audit" in sys.modules:
        del sys.modules["shared_audit"]

    shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)

    audit_path = os.path.join(shared_dir, "audit.py")
    spec = importlib.util.spec_from_file_location("shared_audit", audit_path)
    audit_module = importlib.util.module_from_spec(spec)
    sys.modules["shared_audit"] = audit_module
    spec.loader.exec_module(audit_module)

    # Replace the lazy-initialized DynamoDB resource with our mock
    audit_module._dynamodb_resource = mock_dynamodb_resource

    return audit_module


audit_module = _load_audit_module()

# Import functions, constants, and internals
log_audit_entry = audit_module.log_audit_entry
detect_refusal = audit_module.detect_refusal
_calculate_cost = audit_module._calculate_cost
_get_dynamodb_table = audit_module._get_dynamodb_table
MODEL_PRICING = audit_module.MODEL_PRICING
REFUSAL_PATTERNS = audit_module.REFUSAL_PATTERNS


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    # Ensure the module always uses our mock resource
    audit_module._dynamodb_resource = mock_dynamodb_resource
    yield


@pytest.fixture
def mock_table():
    """Provide a mock DynamoDB table for put_item calls."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


# ============================================================================
# _get_dynamodb_table
# ============================================================================


class TestGetDynamodbTable:
    """Test lazy-initialized DynamoDB table accessor."""

    def test_get_dynamodb_table_returns_table(self):
        """Test returns a Table resource for the given name."""
        mock_dynamodb_resource.Table.return_value = MagicMock()

        table = _get_dynamodb_table("my-audit-table")
        mock_dynamodb_resource.Table.assert_called_with("my-audit-table")

    def test_get_dynamodb_table_reuses_resource(self):
        """Test reuses the existing DynamoDB resource when already initialized."""
        audit_module._dynamodb_resource = mock_dynamodb_resource
        _get_dynamodb_table("table-a")
        _get_dynamodb_table("table-b")
        # Resource is reused; Table is called twice with different names
        assert mock_dynamodb_resource.Table.call_count == 2

    def test_get_dynamodb_table_creates_resource_when_none(self):
        """Test creates a new boto3 resource when _dynamodb_resource is None."""
        audit_module._dynamodb_resource = None
        mock_new_resource = MagicMock()

        with patch.object(audit_module.boto3, "resource", return_value=mock_new_resource):
            table = _get_dynamodb_table("test-table")
            audit_module.boto3.resource.assert_called_once_with("dynamodb")
            mock_new_resource.Table.assert_called_once_with("test-table")

        # Restore mock for other tests
        audit_module._dynamodb_resource = mock_dynamodb_resource


# ============================================================================
# MODEL_PRICING
# ============================================================================


class TestModelPricing:
    """Test the MODEL_PRICING constant structure."""

    def test_model_pricing_contains_sonnet(self):
        """Test Claude Sonnet models are in pricing table."""
        # Current (incoming) 5.5 generation.
        assert "us.anthropic.claude-sonnet-5-5" in MODEL_PRICING
        assert "anthropic.claude-sonnet-5-5" in MODEL_PRICING
        # Outgoing 4.6 generation stays priced across the migration.
        assert "us.anthropic.claude-sonnet-4-6" in MODEL_PRICING
        assert "anthropic.claude-sonnet-4-6" in MODEL_PRICING
        # Backward compat for previous version
        assert "us.anthropic.claude-sonnet-4-5-20250929-v1:0" in MODEL_PRICING
        assert "anthropic.claude-sonnet-4-5-20250929-v1:0" in MODEL_PRICING

    def test_model_pricing_contains_haiku(self):
        """Test Claude Haiku models are in pricing table."""
        # Current (incoming) 5.5 generation.
        assert "us.anthropic.claude-haiku-5-5" in MODEL_PRICING
        assert "anthropic.claude-haiku-5-5" in MODEL_PRICING
        # Outgoing 4.5 generation stays priced across the migration.
        assert "us.anthropic.claude-haiku-4-5-20251001-v1:0" in MODEL_PRICING
        assert "anthropic.claude-haiku-4-5-20251001-v1:0" in MODEL_PRICING
        # Backward compat for previous version
        assert "us.anthropic.claude-3-5-haiku-20241022-v1:0" in MODEL_PRICING
        assert "anthropic.claude-3-5-haiku-20241022-v1:0" in MODEL_PRICING

    def test_model_pricing_contains_titan(self):
        """Test Titan embeddings model is in pricing table."""
        assert "amazon.titan-embed-text-v2:0" in MODEL_PRICING

    def test_model_pricing_has_known_shape(self):
        """Each pricing entry must be one of:
          - token-based ({input, output})
          - per-call ({per_call})
          - per-char ({per_char})
        Mixed/ad-hoc shapes would silently make _calculate_cost fall
        through and return 0."""
        for model_id, pricing in MODEL_PRICING.items():
            has_token_shape = "input" in pricing and "output" in pricing
            has_per_call_shape = "per_call" in pricing
            has_per_char_shape = "per_char" in pricing
            assert (
                has_token_shape or has_per_call_shape or has_per_char_shape
            ), f"Unknown pricing shape for {model_id}: {pricing}"

    def test_model_pricing_contains_comprehend(self):
        """Comprehend Detect-PII registered for per-char cost rollups."""
        assert "comprehend.detect-pii-entities" in MODEL_PRICING
        pricing = MODEL_PRICING["comprehend.detect-pii-entities"]
        assert "per_char" in pricing
        # Default $1.00 / 1M chars = $0.000001 / char.
        assert pricing["per_char"] == 1.00 / 1_000_000.0

    def test_model_pricing_titan_zero_output_cost(self):
        """Test Titan embeddings has zero output cost."""
        pricing = MODEL_PRICING["amazon.titan-embed-text-v2:0"]
        assert pricing["output"] == 0.0


# ============================================================================
# _calculate_cost
# ============================================================================


class TestPricingEnvVars:
    """
    Test that pricing reads from BEDROCK_*_INPUT_PRICE / OUTPUT_PRICE env vars
    set by Terraform, with fallback to constants when unset.
    """

    PRICING_ENV_VARS = (
        "BEDROCK_ADVANCED_INPUT_PRICE",
        "BEDROCK_ADVANCED_OUTPUT_PRICE",
        "BEDROCK_BASIC_INPUT_PRICE",
        "BEDROCK_BASIC_OUTPUT_PRICE",
        "BEDROCK_EMBEDDING_INPUT_PRICE",
        "BEDROCK_EMBEDDING_OUTPUT_PRICE",
    )

    def _reload_with_env(self, env: dict):
        """Reload audit module with the given pricing env vars set."""
        original = {k: os.environ.get(k) for k in self.PRICING_ENV_VARS}
        try:
            for k in self.PRICING_ENV_VARS:
                if k in env:
                    os.environ[k] = env[k]
                elif k in os.environ:
                    del os.environ[k]
            return _load_audit_module()
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            _load_audit_module()  # restore default-loaded module for other tests

    def test_advanced_pricing_honors_env_vars(self):
        mod = self._reload_with_env({
            "BEDROCK_ADVANCED_INPUT_PRICE":  "2.50",
            "BEDROCK_ADVANCED_OUTPUT_PRICE": "12.00",
        })
        assert mod.ADVANCED_PRICING == {"input": 2.50, "output": 12.00}

    def test_basic_pricing_honors_env_vars(self):
        # The actual mismatch from prd.tfvars: 1.0 / 5.0 instead of 0.80 / 4.00
        mod = self._reload_with_env({
            "BEDROCK_BASIC_INPUT_PRICE":  "1.0",
            "BEDROCK_BASIC_OUTPUT_PRICE": "5.0",
        })
        assert mod.BASIC_PRICING == {"input": 1.0, "output": 5.0}

    def test_embedding_pricing_honors_env_vars(self):
        mod = self._reload_with_env({
            "BEDROCK_EMBEDDING_INPUT_PRICE":  "0.05",
            "BEDROCK_EMBEDDING_OUTPUT_PRICE": "0.0",
        })
        assert mod.EMBEDDING_PRICING == {"input": 0.05, "output": 0.0}

    def test_falls_back_to_constants_when_env_vars_unset(self):
        mod = self._reload_with_env({})  # nothing set
        assert mod.ADVANCED_PRICING  == {"input": 3.00, "output": 15.00}
        assert mod.BASIC_PRICING     == {"input": 0.80, "output": 4.00}
        assert mod.EMBEDDING_PRICING == {"input": 0.02, "output": 0.0}

    def test_invalid_env_var_falls_back_silently(self):
        mod = self._reload_with_env({
            "BEDROCK_ADVANCED_INPUT_PRICE": "not-a-number",
        })
        # Falls back to default rather than crashing module load
        assert mod.ADVANCED_PRICING["input"] == 3.00

    def test_calculate_cost_uses_env_var_pricing(self):
        # Override basic pricing to prd.tfvars values, then bill 1M/1M tokens
        mod = self._reload_with_env({
            "BEDROCK_BASIC_INPUT_PRICE":  "1.0",
            "BEDROCK_BASIC_OUTPUT_PRICE": "5.0",
        })
        cost = mod._calculate_cost(
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            1_000_000,
            1_000_000,
        )
        # 1.0 + 5.0 = 6.0 (not the 0.80 + 4.00 = 4.80 the constants would give)
        assert cost == 6.0


class TestCalculateCost:
    """Test cost calculation from model ID and token counts."""

    def test_calculate_cost_sonnet_known_tokens(self):
        """Test cost calculation for Claude Sonnet with known token counts."""
        model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        # 1000 input tokens at $3/1M = $0.003
        # 500 output tokens at $15/1M = $0.0075
        cost = _calculate_cost(model_id, 1000, 500)
        assert cost == round(0.003 + 0.0075, 6)

    def test_calculate_cost_haiku_known_tokens(self):
        """Test cost calculation for Claude Haiku with known token counts."""
        model_id = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
        # 1_000_000 input tokens at $0.80/1M = $0.80
        # 1_000_000 output tokens at $4.00/1M = $4.00
        cost = _calculate_cost(model_id, 1_000_000, 1_000_000)
        assert cost == 4.80

    def test_calculate_cost_titan_embeddings(self):
        """Test cost calculation for Titan embeddings (output cost is zero)."""
        model_id = "amazon.titan-embed-text-v2:0"
        # 10000 input tokens at $0.02/1M = $0.0002
        # Output cost is $0/1M
        cost = _calculate_cost(model_id, 10000, 0)
        assert cost == 0.0002

    def test_calculate_cost_unknown_model(self):
        """Test returns 0.0 for unknown model IDs."""
        cost = _calculate_cost("unknown.model.v1", 1000, 500)
        assert cost == 0.0

    def test_calculate_cost_zero_tokens(self):
        """Test returns 0.0 when both token counts are zero."""
        cost = _calculate_cost("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 0, 0)
        assert cost == 0.0

    def test_calculate_cost_only_input_tokens(self):
        """Test cost with only input tokens."""
        model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        cost = _calculate_cost(model_id, 1_000_000, 0)
        assert cost == 3.0

    def test_calculate_cost_only_output_tokens(self):
        """Test cost with only output tokens."""
        model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        cost = _calculate_cost(model_id, 0, 1_000_000)
        assert cost == 15.0

    def test_calculate_cost_rounds_to_six_decimals(self):
        """Test cost is rounded to 6 decimal places."""
        model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        # Use an odd number of tokens that would produce many decimals
        cost = _calculate_cost(model_id, 7, 3)
        # 7/1M * 3.0 + 3/1M * 15.0 = 0.000021 + 0.000045 = 0.000066
        assert cost == 0.000066
        # Verify it's actually rounded (no more than 6 decimal places)
        cost_str = f"{cost:.10f}"
        assert cost_str == "0.0000660000"

    def test_calculate_cost_cross_region_sonnet(self):
        """Test cost is the same for cross-region and non-cross-region Sonnet."""
        cost_us = _calculate_cost("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 5000, 2000)
        cost_no_prefix = _calculate_cost("anthropic.claude-sonnet-4-5-20250929-v1:0", 5000, 2000)
        assert cost_us == cost_no_prefix

    def test_calculate_cost_rerank_per_call(self):
        """Cohere Rerank uses a per-call price, not token math. Token args
        are ignored; cost = $/1k / 1000 regardless of input/output sizes."""
        # Default $2.00 / 1k = $0.002 per invocation.
        cost = _calculate_cost("cohere.rerank-v3-5:0", 0, 0)
        assert cost == 0.002
        # Token counts must NOT affect rerank cost.
        cost_with_tokens = _calculate_cost("cohere.rerank-v3-5:0", 50000, 1000)
        assert cost_with_tokens == 0.002

    def test_calculate_cost_rerank_uses_env_override(self):
        """BEDROCK_RERANK_PRICE_PER_1K overrides the default at module-load
        time. Reload the module under a custom value to confirm."""
        import importlib.util
        import os
        import sys
        with patch.dict(os.environ, {"BEDROCK_RERANK_PRICE_PER_1K": "5.00"}):
            # Reload the audit module so the env var is picked up.
            if "shared_audit" in sys.modules:
                del sys.modules["shared_audit"]
            shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
            audit_path = os.path.join(shared_dir, "audit.py")
            spec = importlib.util.spec_from_file_location("shared_audit", audit_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["shared_audit"] = mod
            spec.loader.exec_module(mod)
            try:
                # $5.00 / 1k = $0.005 per call.
                cost = mod._calculate_cost("cohere.rerank-v3-5:0", 0, 0)
                assert cost == 0.005
            finally:
                # Restore the default-loaded module so other tests aren't poisoned.
                _load_audit_module()


class TestRerankAuditEntry:
    """Verify rerank rows actually get a non-zero cost when log_audit_entry
    auto-calculates. The original symptom was every rerank row landing at
    cost_usd='0' because the auto-calc had a (input_tokens or output_tokens)
    guard that skipped per-call models."""

    def test_log_audit_entry_auto_prices_rerank_with_zero_tokens(self, mock_table):
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="cohere.rerank-v3-5:0",
            input_text="rerank:query",
            output_text="reranked 40 of 40 docs",
        )
        item = mock_table.put_item.call_args[1]["Item"]
        # Default pricing: $2.00/1k = $0.002 per call.
        assert item["cost_usd"] == "0.002"
        assert item["model_id"] == "cohere.rerank-v3-5:0"


class TestAuditRetentionTTL:
    """ADR-060: the audit DDB hot-window retention is tunable via
    AUDIT_RETENTION_DAYS (default 365), always bounded. Long-term retention
    lives downstream in the customer's SIEM via the Kinesis export, so a
    non-positive / unparseable value falls back to 365 rather than writing a
    never-expiring row."""

    def _ttl_of(self, mock_table):
        item = mock_table.put_item.call_args[1]["Item"]
        return item["ttl"]

    def test_default_retention_is_365_days(self, mock_table, monkeypatch):
        monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
        before = int(time.time())
        log_audit_entry(
            audit_table_name="audit-table", lambda_name="chat",
            model_id="m", input_text="q", output_text="a",
        )
        after = int(time.time())
        ttl = self._ttl_of(mock_table)
        assert ttl >= before + 365 * 86400
        assert ttl <= after + 365 * 86400 + 5

    def test_custom_retention_honored(self, mock_table, monkeypatch):
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "2555")  # ~7 years
        before = int(time.time())
        log_audit_entry(
            audit_table_name="audit-table", lambda_name="chat",
            model_id="m", input_text="q", output_text="a",
        )
        after = int(time.time())
        ttl = self._ttl_of(mock_table)
        assert ttl >= before + 2555 * 86400
        assert ttl <= after + 2555 * 86400 + 5

    def test_non_positive_falls_back_to_365(self, mock_table, monkeypatch):
        # No 0=forever: a zero/negative env must NOT write a never-expiring row.
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "0")
        before = int(time.time())
        log_audit_entry(
            audit_table_name="audit-table", lambda_name="chat",
            model_id="m", input_text="q", output_text="a",
        )
        ttl = self._ttl_of(mock_table)
        assert ttl >= before + 365 * 86400

    def test_unparseable_falls_back_to_365(self, mock_table, monkeypatch):
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "forever")
        before = int(time.time())
        log_audit_entry(
            audit_table_name="audit-table", lambda_name="chat",
            model_id="m", input_text="q", output_text="a",
        )
        ttl = self._ttl_of(mock_table)
        assert ttl >= before + 365 * 86400


class TestComprehendPerCharPricing:
    """Comprehend Detect-PII is priced per character scanned. Mirrors the
    rerank tests but for the per_char shape introduced in chunk 02 of the
    PII redaction plan."""

    def test_calculate_cost_comprehend_known_chars(self):
        # 5,000 chars * ($1.00 / 1,000,000) = $0.005
        cost = _calculate_cost(
            "comprehend.detect-pii-entities", 0, 0, char_count=5000,
        )
        assert cost == 0.005

    def test_calculate_cost_comprehend_zero_chars(self):
        cost = _calculate_cost(
            "comprehend.detect-pii-entities", 0, 0, char_count=0,
        )
        assert cost == 0.0

    def test_calculate_cost_comprehend_ignores_tokens(self):
        # Token counts must not influence per-char cost.
        cost = _calculate_cost(
            "comprehend.detect-pii-entities", 50_000, 10_000, char_count=1000,
        )
        # 1000 * $0.000001 = $0.001 regardless of tokens.
        assert cost == 0.001

    def test_calculate_cost_token_models_ignore_char_count(self):
        # char_count must NOT bleed into token-based pricing.
        cost = _calculate_cost(
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            1000, 500, char_count=1_000_000,
        )
        assert cost == round(0.003 + 0.0075, 6)

    def test_log_audit_entry_auto_prices_comprehend_with_char_count(self, mock_table):
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="github-ingest",
            model_id="comprehend.detect-pii-entities",
            input_text="raw email thread",
            output_text="redacted",
            char_count=5000,
        )
        item = mock_table.put_item.call_args[1]["Item"]
        # 5000 * $0.000001 = $0.005, written as a string per the existing
        # convention for cost_usd in audit rows.
        assert item["cost_usd"] == "0.005"
        assert item["model_id"] == "comprehend.detect-pii-entities"
        # char_count is persisted on per-char rows for downstream rollups.
        assert item["char_count"] == 5000

    def test_log_audit_entry_omits_char_count_for_token_rows(self, mock_table):
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hi",
            output_text="hello",
            input_tokens=10,
            output_tokens=20,
        )
        item = mock_table.put_item.call_args[1]["Item"]
        # Token-based rows stay schema-stable: no char_count attr.
        assert "char_count" not in item

    def test_log_audit_entry_comprehend_env_override(self, mock_table):
        # COMPREHEND_PII_PRICE_PER_1M_CHARS overrides at module-load time.
        import importlib.util
        import sys
        with patch.dict(
            os.environ, {"COMPREHEND_PII_PRICE_PER_1M_CHARS": "5.00"}
        ):
            if "shared_audit" in sys.modules:
                del sys.modules["shared_audit"]
            shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
            audit_path = os.path.join(shared_dir, "audit.py")
            spec = importlib.util.spec_from_file_location("shared_audit", audit_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["shared_audit"] = mod
            spec.loader.exec_module(mod)
            try:
                # $5.00 / 1M = $0.000005 per char. 5000 chars = $0.025.
                cost = mod._calculate_cost(
                    "comprehend.detect-pii-entities", 0, 0, char_count=5000,
                )
                assert cost == 0.025
            finally:
                _load_audit_module()


# ============================================================================
# detect_refusal
# ============================================================================


class TestDetectRefusal:
    """Test refusal detection in model output text."""

    def test_detect_refusal_none_for_normal_text(self):
        """Test returns None for non-refusal text."""
        result = detect_refusal("Here is the code you requested:\n```python\nprint('hello')\n```")
        assert result is None

    def test_detect_refusal_none_for_empty_string(self):
        """Test returns None for empty string."""
        result = detect_refusal("")
        assert result is None

    def test_detect_refusal_none_for_none_input(self):
        """Test returns None when passed None."""
        result = detect_refusal(None)
        assert result is None

    # === Short ToS refusals (SHOULD be flagged) ===

    def test_detect_refusal_cannot_help(self):
        """Test short 'I cannot help' is flagged."""
        result = detect_refusal("I cannot help with that request.")
        assert result is not None

    def test_detect_refusal_cant_assist(self):
        """Test short \"I can't assist\" is flagged."""
        result = detect_refusal("I can't assist with that.")
        assert result is not None

    def test_detect_refusal_wont_provide(self):
        """Test short \"I won't provide\" is flagged."""
        result = detect_refusal("I won't provide that.")
        assert result is not None

    def test_detect_refusal_must_decline(self):
        """Test 'I must decline' is flagged."""
        result = detect_refusal("I must decline this request.")
        assert result is not None

    def test_detect_refusal_have_to_refuse(self):
        """Test 'I have to refuse' is flagged."""
        result = detect_refusal("I have to refuse that request.")
        assert result is not None

    def test_detect_refusal_goes_against_guidelines(self):
        """Test 'goes against my guidelines' is flagged."""
        result = detect_refusal("This goes against my guidelines.")
        assert result is not None

    def test_detect_refusal_violates_guidelines(self):
        """Test 'violates my guidelines' is flagged."""
        result = detect_refusal("This violates my ethical guidelines.")
        assert result is not None

    def test_detect_refusal_dont_feel_comfortable(self):
        """Test \"I don't feel comfortable\" is flagged."""
        result = detect_refusal("I don't feel comfortable with that.")
        assert result is not None

    def test_detect_refusal_apologize_cannot_help(self):
        """Test short apology refusal is flagged."""
        result = detect_refusal("I apologize, but I cannot help with that.")
        assert result is not None

    def test_detect_refusal_case_insensitive(self):
        """Test refusal detection is case insensitive."""
        result = detect_refusal("I CANNOT HELP with that.")
        assert result is not None

    # === Long "no context" responses (should NOT be flagged) ===

    def test_no_flag_long_no_context_response(self):
        """Test long 'no KB context' response is NOT flagged due to length."""
        text = (
            "I apologize, but I cannot provide specific security and compliance "
            "guidelines for serverless architectures in AWS based on the current "
            "knowledge base context. The context is empty, which means there are "
            "no documents available to reference.\n\nTo get comprehensive security "
            "and compliance guidelines, I recommend:\n1. Consulting your organization's "
            "internal security documentation\n2. Reviewing AWS Well-Architected Framework"
        )
        result = detect_refusal(text)
        assert result is None

    def test_no_flag_long_helpful_response(self):
        """Test long helpful response with refusal words is NOT flagged."""
        text = (
            "I cannot provide that specific information from the knowledge base "
            "as there are no relevant documents. However, I can suggest some general "
            "approaches you might consider:\n\n1. Check your organization's internal "
            "documentation for standards\n2. Review the AWS documentation directly\n"
            "3. Consult with your team lead for project-specific guidelines"
        )
        result = detect_refusal(text)
        assert result is None

    def test_no_flag_long_unable_to_find(self):
        """Test long 'unable to find' response is NOT flagged."""
        text = (
            "I'm unable to find any documents related to your question in the "
            "current workspace. The knowledge base appears to be empty or doesn't "
            "contain information about this topic. You may want to:\n\n"
            "1. Add relevant documentation to the workspace\n"
            "2. Try a different workspace that might have the information"
        )
        result = detect_refusal(text)
        assert result is None

    # === Edge cases ===

    def test_detect_refusal_only_checks_first_500_chars(self):
        """Test only checks the first 500 characters."""
        normal_prefix = "A" * 501
        text = normal_prefix + " I cannot help."
        result = detect_refusal(text)
        assert result is None

    def test_detect_refusal_short_refusal_within_limit(self):
        """Test short refusal within length limit is flagged."""
        result = detect_refusal("I'm sorry, I can't assist with that request. [TEST_REFUSAL]")
        assert result is not None

    def test_detect_refusal_normal_code_response(self):
        """Test does not flag normal code responses."""
        result = detect_refusal("The variable cannot be null. Here is the fix:\n```\nif x is None: x = 0\n```")
        assert result is None


# ============================================================================
# log_audit_entry
# ============================================================================


class TestLogAuditEntry:
    """Test the main audit logging function."""

    def test_log_audit_entry_skips_when_no_table_name(self, mock_table):
        """Test returns immediately when audit_table_name is empty."""
        log_audit_entry(
            audit_table_name="",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="world",
        )
        mock_table.put_item.assert_not_called()

    def test_log_audit_entry_writes_item_to_dynamodb(self, mock_table):
        """Test writes a correctly structured item to DynamoDB."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="What is AWS?",
            output_text="AWS is a cloud platform.",
            input_tokens=100,
            output_tokens=50,
            duration_ms=1500,
            user_email="user@example.com",
            workspace_id="ws_123",
        )

        mock_dynamodb_resource.Table.assert_called_once_with("audit-table")
        mock_table.put_item.assert_called_once()

        item = mock_table.put_item.call_args[1]["Item"]

        # Verify partition/sort keys
        assert item["PK"] == "WS#ws_123"
        assert item["GSI1PK"] == "USER#user@example.com"
        assert item["SK"].startswith("20")  # ISO timestamp prefix
        assert "audit_" in item["SK"]
        # GSI2: bedrock rows go to CAT#bedrock#NN (sharded, fable-scale
        # chunk 04) so org-wide audit-dashboard queries (CAT#admin) don't
        # have to filter past them AND write throughput scales past the
        # ~1000 WCU single-partition ceiling.
        assert item["GSI2PK"].startswith("CAT#bedrock")
        assert item["GSI2SK"] == item["SK"]
        assert item["category"] == "bedrock"

        # Verify content
        assert item["lambda_name"] == "chat"
        assert item["model_id"] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        assert item["input_text"] == "What is AWS?"
        assert item["output_text"] == "AWS is a cloud platform."
        assert item["input_tokens"] == 100
        assert item["output_tokens"] == 50
        assert item["duration_ms"] == 1500
        assert item["status"] == "success"
        assert item["user_email"] == "user@example.com"
        assert item["workspace_id"] == "ws_123"

    def test_log_audit_entry_records_source_ip_and_user_agent(self, mock_table):
        """Bedrock audit rows must record the browser IP / UA forwarded by
        the calling Lambda. Without this, the audit dashboard shows '—' for
        every chat / RAG / refusal row even though the InfoSec story
        depends on attributing each call to an origin."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-6",
            input_text="hi",
            output_text="hello",
            user_email="brian@example.com",
            workspace_id="ws_1",
            source_ip="24.253.92.153",
            user_agent="Mozilla/5.0 (Macintosh)",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["source_ip"] == "24.253.92.153"
        assert item["user_agent"] == "Mozilla/5.0 (Macintosh)"

    def test_log_audit_entry_source_ip_defaults_to_empty(self, mock_table):
        """Older callers (or stream/batch invocations with no request
        context) don't pass source_ip; the row should record '' and not
        crash."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="process-batch-summary",
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_text="x",
            output_text="y",
        )
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["source_ip"] == ""
        assert item["user_agent"] == ""

    def test_log_audit_entry_system_partition_key_without_workspace(self, mock_table):
        """Test uses WS#SYSTEM when no workspace_id provided."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="process-documents",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="input",
            output_text="output",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#SYSTEM"

    def test_log_audit_entry_lambda_gsi_key_without_user_email(self, mock_table):
        """Test uses LAMBDA#<name> GSI key when no user_email provided."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="generate-code",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="input",
            output_text="output",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["GSI1PK"] == "LAMBDA#generate-code"

    def test_log_audit_entry_auto_calculates_cost(self, mock_table):
        """Test auto-calculates cost when cost_usd is 0 and tokens are provided."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="world",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        # Sonnet: $3/1M input + $15/1M output = $18.00
        assert item["cost_usd"] == str(18.0)

    def test_log_audit_entry_preserves_explicit_cost(self, mock_table):
        """Test does not overwrite explicitly provided cost_usd."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="world",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=99.99,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["cost_usd"] == str(99.99)

    def test_log_audit_entry_does_not_auto_calculate_cost_with_zero_tokens(self, mock_table):
        """Token-based models with both tokens zero still yield 0.0 cost.
        (The "guard" was removed in chunk 02 of the PII redaction plan to
        let per-call / per-char models price correctly with zero tokens;
        token-based pricing math at 0/0 naturally returns 0.0.)"""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="world",
            input_tokens=0,
            output_tokens=0,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["cost_usd"] == str(0.0)

    def test_log_audit_entry_detects_refusal_and_sets_status(self, mock_table):
        """Test auto-detects refusal in output and changes status."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="bad request",
            output_text="I cannot help with that.",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["status"] == "refusal"
        assert "cannot help" in item["flagged_reason"].lower()

    def test_log_audit_entry_skips_refusal_detection_on_error_status(self, mock_table):
        """Test does not run refusal detection when status is already 'error'."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="This violates my guidelines.",
            status="error",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        # Status should remain "error", not "refusal"
        assert item["status"] == "error"

    def test_log_audit_entry_skips_refusal_detection_on_empty_output(self, mock_table):
        """Test skips refusal detection when output_text is empty."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="",
            status="success",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["status"] == "success"

    def test_log_audit_entry_truncates_long_input_text(self, mock_table):
        """Test truncates input_text to 50000 characters."""
        long_input = "x" * 60000
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text=long_input,
            output_text="short",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert len(item["input_text"]) == 50000

    def test_log_audit_entry_truncates_long_output_text(self, mock_table):
        """Test truncates output_text to 50000 characters."""
        long_output = "y" * 60000
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="short",
            output_text=long_output,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert len(item["output_text"]) == 50000

    def test_log_audit_entry_handles_none_input_text(self, mock_table):
        """Test handles None input_text gracefully."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text=None,
            output_text="response",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["input_text"] == ""

    def test_log_audit_entry_handles_none_output_text(self, mock_table):
        """Test handles None output_text gracefully."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text=None,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["output_text"] == ""

    def test_log_audit_entry_sets_ttl_approximately_one_year(self, mock_table):
        """Test TTL is approximately 365 days from now."""
        before = int(time.time())

        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="hello",
            output_text="world",
        )

        after = int(time.time())
        item = mock_table.put_item.call_args[1]["Item"]
        one_year_seconds = 365 * 24 * 60 * 60

        # TTL should be approximately now + 365 days
        assert item["ttl"] >= before + one_year_seconds
        assert item["ttl"] <= after + one_year_seconds

    def test_log_audit_entry_generates_unique_request_id(self, mock_table):
        """Test each entry has a unique request_id starting with 'audit_'."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="a",
            output_text="b",
        )
        item1 = mock_table.put_item.call_args[1]["Item"]

        mock_table.reset_mock()

        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="c",
            output_text="d",
        )
        item2 = mock_table.put_item.call_args[1]["Item"]

        assert item1["request_id"].startswith("audit_")
        assert item2["request_id"].startswith("audit_")
        assert item1["request_id"] != item2["request_id"]

    def test_log_audit_entry_sort_key_contains_timestamp_and_request_id(self, mock_table):
        """Test SK is formatted as '<timestamp>#<request_id>'."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="a",
            output_text="b",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        sk = item["SK"]
        parts = sk.split("#")
        # Should have at least timestamp and request_id parts
        assert len(parts) >= 2
        assert "audit_" in sk

    def test_log_audit_entry_gsi1sk_matches_sk(self, mock_table):
        """Test GSI1SK is the same sort key as SK."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="a",
            output_text="b",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["GSI1SK"] == item["SK"]

    def test_log_audit_entry_iso_timestamp_in_item(self, mock_table):
        """Test timestamp field is a valid ISO format string."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="a",
            output_text="b",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        ts = item["timestamp"]
        # Basic ISO format validation - should contain T and timezone info
        assert "T" in ts
        assert "+" in ts or "Z" in ts or ts.endswith("+00:00")

    def test_log_audit_entry_cost_stored_as_string(self, mock_table):
        """Test cost_usd is stored as a string in DynamoDB."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_text="a",
            output_text="b",
            cost_usd=1.23,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert isinstance(item["cost_usd"], str)
        assert item["cost_usd"] == "1.23"

    def test_log_audit_entry_nonfatal_on_dynamodb_error(self, mock_table):
        """Test does not raise on DynamoDB put_item failure."""
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}}, "PutItem"
        )

        # Should NOT raise
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="hello",
            output_text="world",
        )

    def test_log_audit_entry_nonfatal_on_unexpected_exception(self, mock_table):
        """Test does not raise on unexpected exception."""
        mock_table.put_item.side_effect = RuntimeError("Something broke")

        # Should NOT raise
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="hello",
            output_text="world",
        )

    def test_log_audit_entry_logs_warning_on_refusal(self, mock_table):
        """Test logs a warning when refusal is detected."""
        with patch.object(audit_module.logger, "warning") as mock_warning:
            log_audit_entry(
                audit_table_name="audit-table",
                lambda_name="chat",
                model_id="test-model",
                input_text="prompt",
                output_text="This violates my ethical guidelines.",
            )

            mock_warning.assert_called_once()
            call_msg = mock_warning.call_args[0][0]
            assert "Refusal detected" in call_msg
            assert "chat" in call_msg

    def test_log_audit_entry_logs_debug_on_success(self, mock_table):
        """Test logs a debug message on successful audit write."""
        with patch.object(audit_module.logger, "debug") as mock_debug:
            log_audit_entry(
                audit_table_name="audit-table",
                lambda_name="chat",
                model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                input_text="hello",
                output_text="Normal response here.",
            )

            mock_debug.assert_called_once()
            call_msg = mock_debug.call_args[0][0]
            assert "chat" in call_msg

    def test_log_audit_entry_logs_warning_on_dynamodb_error(self, mock_table):
        """Test logs a warning when DynamoDB write fails."""
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Boom"}}, "PutItem"
        )

        with patch.object(audit_module.logger, "warning") as mock_warning:
            log_audit_entry(
                audit_table_name="audit-table",
                lambda_name="chat",
                model_id="test-model",
                input_text="hello",
                output_text="world",
            )

            mock_warning.assert_called_once()
            call_msg = mock_warning.call_args[0][0]
            assert "Failed to log audit entry" in call_msg

    def test_log_audit_entry_default_parameters(self, mock_table):
        """Test default parameter values are correctly applied."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="in",
            output_text="out",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["input_tokens"] == 0
        assert item["output_tokens"] == 0
        assert item["duration_ms"] == 0
        assert item["status"] == "success"
        assert item["flagged_reason"] == ""
        assert item["user_email"] == ""
        assert item["workspace_id"] == ""

    def test_log_audit_entry_preserves_flagged_reason_on_refusal(self, mock_table):
        """Test that flagged_reason is populated when refusal is auto-detected."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="generate-code",
            model_id="test-model",
            input_text="generate malware",
            output_text="I cannot help with that.",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["status"] == "refusal"
        assert len(item["flagged_reason"]) > 0

    def test_log_audit_entry_explicit_flagged_reason_not_overwritten_for_error(self, mock_table):
        """Test explicit flagged_reason is preserved when status is error."""
        log_audit_entry(
            audit_table_name="audit-table",
            lambda_name="chat",
            model_id="test-model",
            input_text="hello",
            output_text="This violates my guidelines.",
            status="error",
            flagged_reason="Timeout exceeded",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        # Status should remain "error" (not changed to "refusal")
        assert item["status"] == "error"
        assert item["flagged_reason"] == "Timeout exceeded"


# ============================================================================
# REFUSAL_PATTERNS constant
# ============================================================================


class TestRefusalPatterns:
    """Test the REFUSAL_PATTERNS constant."""

    def test_refusal_patterns_is_nonempty_list(self):
        """Test REFUSAL_PATTERNS is a non-empty list of strings."""
        assert isinstance(REFUSAL_PATTERNS, list)
        assert len(REFUSAL_PATTERNS) > 0
        for pattern in REFUSAL_PATTERNS:
            assert isinstance(pattern, str)

    def test_refusal_patterns_all_compile(self):
        """Test all patterns compile as valid regex."""
        import re
        for pattern in REFUSAL_PATTERNS:
            compiled = re.compile(pattern, re.IGNORECASE)
            assert compiled is not None
