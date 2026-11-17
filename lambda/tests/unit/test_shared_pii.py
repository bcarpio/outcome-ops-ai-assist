# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared/pii.py module."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading (mirrors the established _load_audit_module pattern in
# test_shared_audit.py / test_shared_audit_writer.py)
# ============================================================================

mock_comprehend_client = MagicMock()


def _load_pii_module():
    """Load or reload the shared pii module from its exact file path."""
    if "shared_pii" in sys.modules:
        del sys.modules["shared_pii"]

    shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)

    pii_path = os.path.join(shared_dir, "pii.py")
    spec = importlib.util.spec_from_file_location("shared_pii", pii_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["shared_pii"] = module
    spec.loader.exec_module(module)

    # Replace the lazy-initialized Comprehend client with our mock so no
    # real AWS calls leak from the test suite.
    module._comprehend = mock_comprehend_client
    return module


pii_module = _load_pii_module()
redact = pii_module.redact
read_workspace_mode = pii_module.read_workspace_mode
RedactionResult = pii_module.RedactionResult
replacement_for = pii_module.replacement_for
skip_types_from_env = pii_module.skip_types_from_env
DEFAULT_SKIP_TYPES = pii_module.DEFAULT_SKIP_TYPES
MODE_STRICT = pii_module.MODE_STRICT
MODE_NONE = pii_module.MODE_NONE
VALID_MODES = pii_module.VALID_MODES
COMPREHEND_MAX_BYTES = pii_module.COMPREHEND_MAX_BYTES


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset the Comprehend mock between tests; keep the module pinned
    to our mock client even if a test reloads the module."""
    mock_comprehend_client.reset_mock()
    # reset_mock() preserves side_effect across tests, which leaks state
    # from error-path tests into happy-path tests. Clear explicitly.
    mock_comprehend_client.detect_pii_entities.side_effect = None
    mock_comprehend_client.detect_pii_entities.return_value = None
    pii_module._comprehend = mock_comprehend_client
    yield


def _comprehend_response(entities):
    """Wrap a list of {Type, BeginOffset, EndOffset} dicts in a
    Comprehend DetectPiiEntities response. Shape mirrors the boto3
    docs: https://docs.aws.amazon.com/comprehend/latest/dg/API_DetectPiiEntities.html"""
    return {"Entities": entities}


# ============================================================================
# Module-level constants and helpers
# ============================================================================


class TestConstants:
    def test_modes_exported(self):
        assert MODE_STRICT == "STRICT"
        assert MODE_NONE == "NONE"
        assert VALID_MODES == frozenset({"STRICT", "NONE"})

    def test_replacement_for_email(self):
        assert replacement_for("EMAIL") == "[REDACTED:EMAIL]"

    def test_replacement_for_phone(self):
        assert replacement_for("PHONE") == "[REDACTED:PHONE]"

    def test_replacement_for_unknown_type_passthrough(self):
        # Comprehend may add new types; don't fail on unfamiliar ones.
        assert replacement_for("WEIRD_NEW_TYPE") == "[REDACTED:WEIRD_NEW_TYPE]"


# ============================================================================
# Gating: org disabled / workspace NONE / empty text
# ============================================================================


class TestGating:
    def test_org_disabled_skips_comprehend(self):
        result = redact(
            "email me at brian@example.com please",
            mode=MODE_STRICT,
            org_enabled=False,
        )
        assert result.text == "email me at brian@example.com please"
        assert result.skipped_reason == "org_disabled"
        assert result.summary == {}
        mock_comprehend_client.detect_pii_entities.assert_not_called()

    def test_workspace_none_skips_comprehend(self):
        result = redact(
            "email me at brian@example.com please",
            mode=MODE_NONE,
            org_enabled=True,
        )
        assert result.text == "email me at brian@example.com please"
        assert result.skipped_reason == "workspace_none"
        assert result.summary == {}
        mock_comprehend_client.detect_pii_entities.assert_not_called()

    def test_empty_string_skips_comprehend(self):
        result = redact("", mode=MODE_STRICT, org_enabled=True)
        assert result.text == ""
        assert result.char_count == 0
        assert result.skipped_reason == "empty_text"
        mock_comprehend_client.detect_pii_entities.assert_not_called()

    def test_whitespace_only_skips_comprehend(self):
        result = redact("   \n\t\n  ", mode=MODE_STRICT, org_enabled=True)
        assert result.skipped_reason == "empty_text"
        mock_comprehend_client.detect_pii_entities.assert_not_called()

    def test_default_kwargs_skip_comprehend(self):
        # Default mode=NONE, org_enabled=True - safe-by-default for any
        # caller that forgets to thread the workspace mode through.
        result = redact("brian@example.com")
        assert result.skipped_reason == "workspace_none"
        mock_comprehend_client.detect_pii_entities.assert_not_called()


# ============================================================================
# Happy path: STRICT + org enabled + Comprehend matches
# ============================================================================


class TestRedaction:
    def test_single_email_replaced_inline(self):
        text = "email me at brian@example.com please"
        # "brian@example.com" starts at offset 12 and is 17 chars long.
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 12, "EndOffset": 29,
                 "Score": 0.99},
            ])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert result.text == "email me at [REDACTED:EMAIL] please"
        assert result.summary == {"EMAIL": 1}
        assert result.char_count == len(text)
        assert result.skipped_reason == ""
        mock_comprehend_client.detect_pii_entities.assert_called_once()

    def test_multiple_entity_types(self):
        text = "Call brian@example.com or 555-0100"
        # brian@example.com = offsets 5..22, 555-0100 = offsets 26..34
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 5, "EndOffset": 22,
                 "Score": 0.99},
                {"Type": "PHONE", "BeginOffset": 26, "EndOffset": 34,
                 "Score": 0.99},
            ])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert "[REDACTED:EMAIL]" in result.text
        assert "[REDACTED:PHONE]" in result.text
        assert "brian@example.com" not in result.text
        assert "555-0100" not in result.text
        assert result.summary == {"EMAIL": 1, "PHONE": 1}

    def test_back_to_front_replacement_keeps_offsets_valid(self):
        # Three entities; replacement must preserve offsets even though
        # the replacement string is a different length than the entity.
        # Pass skip_types=frozenset() to disable the default skip list -
        # this test is about offset arithmetic, not skip-list semantics.
        text = "A brian@example.com B 555-0100 C 192.168.1.1 D"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 2, "EndOffset": 19,
                 "Score": 0.99},
                {"Type": "PHONE", "BeginOffset": 22, "EndOffset": 30,
                 "Score": 0.99},
                {"Type": "IP_ADDRESS", "BeginOffset": 33, "EndOffset": 44,
                 "Score": 0.99},
            ])
        )
        result = redact(
            text, mode=MODE_STRICT, org_enabled=True,
            skip_types=frozenset(),
        )
        assert result.text == (
            "A [REDACTED:EMAIL] B [REDACTED:PHONE] C "
            "[REDACTED:IP_ADDRESS] D"
        )
        assert result.summary == {"EMAIL": 1, "PHONE": 1, "IP_ADDRESS": 1}

    def test_no_entities_returns_text_unchanged(self):
        text = "this is just a normal sentence"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert result.text == text
        assert result.summary == {}
        assert result.skipped_reason == ""

    def test_multiple_of_same_type_counted(self):
        text = "alice@x.com cc bob@y.com"
        # alice@x.com 0..11, bob@y.com 15..24
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 0, "EndOffset": 11,
                 "Score": 0.99},
                {"Type": "EMAIL", "BeginOffset": 15, "EndOffset": 24,
                 "Score": 0.99},
            ])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert result.summary == {"EMAIL": 2}
        assert result.text.count("[REDACTED:EMAIL]") == 2


# ============================================================================
# Chunking for >100KB inputs
# ============================================================================


class TestChunking:
    def test_short_text_single_call(self):
        text = "small text"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([])
        )
        redact(text, mode=MODE_STRICT, org_enabled=True)
        assert mock_comprehend_client.detect_pii_entities.call_count == 1

    def test_long_text_chunked(self):
        # Build a payload >100KB with paragraph breaks.
        para = "a " * 30_000  # ~60K chars
        text = para + "\n\n" + para  # >120K total, two paragraphs

        # Each chunk returns no entities for simplicity.
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)

        # Two chunks => two Comprehend calls.
        assert mock_comprehend_client.detect_pii_entities.call_count == 2
        # Round-trip preserved (joined with \n\n).
        assert result.text == text
        assert result.summary == {}

    def test_chunk_summaries_sum(self):
        para_a = "x " * 30_000  # ~60K chars
        para_b = "y " * 30_000
        text = para_a + "\n\n" + para_b

        # reports 2 emails, reports 3 phones.
        side_effects = [
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 0, "EndOffset": 1,
                 "Score": 0.99},
                {"Type": "EMAIL", "BeginOffset": 2, "EndOffset": 3,
                 "Score": 0.99},
            ]),
            _comprehend_response([
                {"Type": "PHONE", "BeginOffset": 0, "EndOffset": 1,
                 "Score": 0.99},
                {"Type": "PHONE", "BeginOffset": 2, "EndOffset": 3,
                 "Score": 0.99},
                {"Type": "PHONE", "BeginOffset": 4, "EndOffset": 5,
                 "Score": 0.99},
            ]),
        ]
        mock_comprehend_client.detect_pii_entities.side_effect = side_effects
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert result.summary == {"EMAIL": 2, "PHONE": 3}


# ============================================================================
# Non-fatal error handling
# ============================================================================


class TestErrorHandling:
    def test_comprehend_client_error_swallowed(self):
        text = "brian@example.com"
        mock_comprehend_client.detect_pii_entities.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "DetectPiiEntities",
        )
        # Must not raise.
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert result.text == text  # raw text passed through
        assert result.skipped_reason == "comprehend_error"
        assert result.summary == {}

    def test_unexpected_exception_swallowed(self):
        text = "brian@example.com"
        mock_comprehend_client.detect_pii_entities.side_effect = (
            RuntimeError("boom")
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert result.text == text
        assert result.skipped_reason == "comprehend_error"


# ============================================================================
# Language code threading
# ============================================================================


class TestLanguageCode:
    def test_default_language_is_en(self):
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([])
        )
        redact("hello", mode=MODE_STRICT, org_enabled=True)
        kwargs = mock_comprehend_client.detect_pii_entities.call_args.kwargs
        assert kwargs.get("LanguageCode") == "en"

    def test_language_code_passed_through(self):
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([])
        )
        redact("hola", mode=MODE_STRICT, org_enabled=True, language_code="es")
        kwargs = mock_comprehend_client.detect_pii_entities.call_args.kwargs
        assert kwargs.get("LanguageCode") == "es"


# ============================================================================
# read_workspace_mode helper
# ============================================================================


class TestReadWorkspaceMode:
    def test_returns_strict_for_legacy_workspace_missing_attr(self):
        """Workspaces created before lack privacy_mode -
        reader must default to STRICT (the safe default)."""
        table = MagicMock()
        table.get_item.return_value = {"Item": {"workspace_id": "ws1"}}
        assert read_workspace_mode(table, "ws1") == "STRICT"

    def test_returns_explicit_none(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "privacy_mode": "NONE"}
        }
        assert read_workspace_mode(table, "ws1") == "NONE"

    def test_returns_explicit_strict(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"workspace_id": "ws1", "privacy_mode": "STRICT"}
        }
        assert read_workspace_mode(table, "ws1") == "STRICT"

    def test_missing_item_returns_strict(self):
        table = MagicMock()
        table.get_item.return_value = {}  # no Item key at all
        assert read_workspace_mode(table, "ws1") == "STRICT"

    def test_clienterror_returns_strict_non_fatal(self):
        """A transient DDB ClientError must not break ingest. The reader
        defaults to STRICT (err on the side of redacting)."""
        table = MagicMock()
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "rate"}},
            "GetItem",
        )
        assert read_workspace_mode(table, "ws1") == "STRICT"


# ============================================================================
# Skip-types feature (developer-KB-friendly default)
# ============================================================================


class TestDefaultSkipTypes:
    def test_default_skip_set_contents(self):
        """The default skip list is the developer-KB tuned set: URL,
        IP_ADDRESS, MAC_ADDRESS, DATE_TIME. Names / emails / phones /
        addresses / SSNs / cards stay redacted by default."""
        assert DEFAULT_SKIP_TYPES == frozenset({
            "URL", "IP_ADDRESS", "MAC_ADDRESS", "DATE_TIME",
        })


class TestSkipTypesFromEnv:
    def test_env_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("PII_REDACTION_SKIP_TYPES", raising=False)
        assert skip_types_from_env() == DEFAULT_SKIP_TYPES

    def test_env_explicit_empty_means_redact_all(self):
        # Operator-deliberate: empty string => no skips, redact every type.
        assert skip_types_from_env("") == frozenset()

    def test_env_custom_list(self):
        result = skip_types_from_env("URL,IP_ADDRESS")
        assert result == frozenset({"URL", "IP_ADDRESS"})

    def test_env_strips_whitespace(self):
        result = skip_types_from_env(" URL , IP_ADDRESS ,  MAC_ADDRESS")
        assert result == frozenset({"URL", "IP_ADDRESS", "MAC_ADDRESS"})

    def test_env_drops_empty_segments(self):
        result = skip_types_from_env("URL,,IP_ADDRESS,,")
        assert result == frozenset({"URL", "IP_ADDRESS"})


class TestRedactWithSkipTypes:
    def test_default_skip_keeps_url_ip_mac_date(self):
        """STRICT mode with default skip list: URL / IP_ADDRESS /
        MAC_ADDRESS / DATE_TIME pass through unredacted; EMAIL still
        redacted."""
        text = "Email brian@example.com on 2025-01-15 at 192.168.1.1"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 6, "EndOffset": 23,
                 "Score": 0.99},
                {"Type": "DATE_TIME", "BeginOffset": 27, "EndOffset": 37,
                 "Score": 0.99},
                {"Type": "IP_ADDRESS", "BeginOffset": 41, "EndOffset": 52,
                 "Score": 0.99},
            ])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert "[REDACTED:EMAIL]" in result.text
        # IP and DATE_TIME stay raw under the default skip list.
        assert "192.168.1.1" in result.text
        assert "2025-01-15" in result.text
        # Skipped types do NOT appear in the summary count.
        assert result.summary == {"EMAIL": 1}

    def test_explicit_empty_skip_redacts_everything(self):
        """Operator override: skip_types=frozenset() => redact every
        type Comprehend detects, including the developer-friendly ones."""
        text = "Visit https://internal.example.com on 192.168.1.1"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "URL", "BeginOffset": 6, "EndOffset": 34,
                 "Score": 0.99},
                {"Type": "IP_ADDRESS", "BeginOffset": 38, "EndOffset": 49,
                 "Score": 0.99},
            ])
        )
        result = redact(
            text, mode=MODE_STRICT, org_enabled=True,
            skip_types=frozenset(),
        )
        assert "https://internal.example.com" not in result.text
        assert "192.168.1.1" not in result.text
        assert result.summary == {"URL": 1, "IP_ADDRESS": 1}

    def test_custom_skip_list_overrides_default(self):
        """Caller can provide an arbitrary skip set; URL gets redacted
        when it's not in the override."""
        text = "Visit https://internal.example.com on 192.168.1.1"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "URL", "BeginOffset": 6, "EndOffset": 34,
                 "Score": 0.99},
                {"Type": "IP_ADDRESS", "BeginOffset": 38, "EndOffset": 49,
                 "Score": 0.99},
            ])
        )
        result = redact(
            text, mode=MODE_STRICT, org_enabled=True,
            skip_types=frozenset({"IP_ADDRESS"}),  # only IP skipped
        )
        assert "[REDACTED:URL]" in result.text
        assert "192.168.1.1" in result.text
        assert result.summary == {"URL": 1}

    def test_skip_does_not_affect_offset_arithmetic(self):
        """A skipped entity sandwiched between two redacted ones must
        not corrupt the offsets of either neighbor."""
        text = "Email brian@example.com from 192.168.1.1 cell 555-0100"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "EMAIL", "BeginOffset": 6, "EndOffset": 23,
                 "Score": 0.99},
                {"Type": "IP_ADDRESS", "BeginOffset": 29, "EndOffset": 40,
                 "Score": 0.99},
                {"Type": "PHONE", "BeginOffset": 46, "EndOffset": 54,
                 "Score": 0.99},
            ])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        # IP_ADDRESS in the middle is in the default skip list.
        assert "192.168.1.1" in result.text
        # EMAIL and PHONE on either side both got redacted at correct offsets.
        assert "[REDACTED:EMAIL]" in result.text
        assert "[REDACTED:PHONE]" in result.text
        assert "brian@example.com" not in result.text
        assert "555-0100" not in result.text

    def test_env_var_drives_default_when_skip_types_none(self, monkeypatch):
        """When the caller passes skip_types=None (the default),
        PII_REDACTION_SKIP_TYPES env var picks the policy. Empty string
        env var => redact every type."""
        monkeypatch.setenv("PII_REDACTION_SKIP_TYPES", "")
        text = "Visit https://internal.example.com"
        mock_comprehend_client.detect_pii_entities.return_value = (
            _comprehend_response([
                {"Type": "URL", "BeginOffset": 6, "EndOffset": 34,
                 "Score": 0.99},
            ])
        )
        result = redact(text, mode=MODE_STRICT, org_enabled=True)
        assert "[REDACTED:URL]" in result.text
        assert result.summary == {"URL": 1}
