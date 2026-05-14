"""Unit tests for the ABAP hybrid extractor (plan-gkg chunk 01, ADR-020)."""

from __future__ import annotations

import os
import sys

import pytest

EXTRACTOR_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "process-batch-summary")
)
if EXTRACTOR_PARENT not in sys.path:
    sys.path.insert(0, EXTRACTOR_PARENT)

from extractor import extract_facts  # noqa: E402
from extractor.abap_extractor import (  # noqa: E402
    _extract_via_regex,
    _extract_via_tree_sitter,
    _looks_oo,
    _looks_procedural,
)


FIXTURE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "abap")
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "abap"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestHeaderDispatcher:
    def test_oo_header_detected(self):
        assert _looks_oo("CLASS ZCL_FOO DEFINITION.") is True
        assert _looks_oo("CLASS ZCL_FOO IMPLEMENTATION.") is True

    def test_procedural_marker_detected(self):
        assert _looks_procedural("REPORT z_foo.") is True
        assert _looks_procedural("FUNCTION-POOL z.") is True
        assert _looks_procedural("START-OF-SELECTION.") is True

    def test_neither_pattern_falls_back_to_regex(self):
        text = b"DATA: lv_x TYPE i."
        # Pure data section -> no markers; the regex backend still
        # runs (and emits nothing for this trivial input).
        facts = list(_extract_via_regex("x.abap", text))
        assert facts == []


class TestOoAbap:
    @pytest.fixture
    def facts(self):
        return _load("zcl_booking.clas.abap")

    def test_class_defined(self, facts):
        defines = _by_kind(facts, "defines")
        assert any(
            f.target_symbol_id.endswith("::ZCL_BOOKING") for f in defines
        )

    def test_methods_namespaced_to_class(self, facts):
        defines = _by_kind(facts, "defines")
        ids = {f.target_symbol_id for f in defines}
        assert any("ZCL_BOOKING::CONFIRM" in i for i in ids)
        assert any("ZCL_BOOKING::NOTIFY_OPS" in i for i in ids)

    def test_static_call_uses_class_module(self, facts):
        calls = _by_kind(facts, "calls")
        targets = [f.target_symbol_id for f in calls]
        assert any("abap:ZCL_LOGGER::LOG" in t for t in targets)

    def test_call_function_module(self, facts):
        calls = _by_kind(facts, "calls")
        targets = [f.target_symbol_id for f in calls]
        assert any("BAPI_TRANSACTION_COMMIT::*" in t for t in targets)


class TestProceduralAbap:
    @pytest.fixture
    def facts(self):
        return _load("z_booking_report.abap")

    def test_include_emitted_as_import(self, facts):
        imports = _by_kind(facts, "imports")
        assert any("Z_BOOKING_TOP" in f.target_symbol_id for f in imports)

    def test_forms_defined(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any("MAIN_ROUTINE" in i for i in ids)
        assert any("HELPER_ROUTINE" in i for i in ids)

    def test_perform_emits_call(self, facts):
        calls = _by_kind(facts, "calls")
        names = [f.target_symbol_id for f in calls]
        assert any("MAIN_ROUTINE" in n for n in names)
        assert any("HELPER_ROUTINE" in n for n in names)


class TestTreeSitterBackendFallback:
    """When the FrankRogalski grammar is unavailable the dispatcher
    must silently fall back to the regex backend (ADR-020 contingency).
    """

    def test_tree_sitter_backend_returns_facts(self):
        oo = b"CLASS ZCL_FOO DEFINITION.\nMETHOD bar.\nENDMETHOD.\nENDCLASS.\n"
        facts = list(_extract_via_tree_sitter("x.abap", oo))
        # Whatever the underlying backend, the dispatcher must emit
        # at least the class definition.
        assert any("ZCL_FOO" in f.target_symbol_id for f in facts)
