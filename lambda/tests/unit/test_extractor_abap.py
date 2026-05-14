"""Unit tests for the ABAP hybrid extractor (plan-gkg chunk 01, ADR-020)."""

from __future__ import annotations

import json
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
    _get_tree_sitter_parser,
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


# ---------------------------------------------------------------------------
# Tree-sitter OO walk (ADR-020 v1.1)
# ---------------------------------------------------------------------------


_HAS_TS_ABAP = _get_tree_sitter_parser() is not None


@pytest.mark.skipif(
    not _HAS_TS_ABAP,
    reason="tree-sitter-abap grammar not installed in this environment",
)
class TestTreeSitterOoWalk:
    """The Tree-sitter walk must exercise every OO node kind enumerated
    in ADR-020 v1.1 and emit a stable Fact stream against the
    ``zcl_all_edges`` fixture.
    """

    FIXTURE = "zcl_all_edges.clas.abap"
    GOLDEN = "zcl_all_edges.expected.json"

    @pytest.fixture
    def facts(self):
        return _load(self.FIXTURE)

    @pytest.fixture
    def golden(self):
        with open(os.path.join(FIXTURE_DIR, self.GOLDEN)) as fh:
            return json.load(fh)

    def test_dispatcher_picks_tree_sitter(self):
        with open(os.path.join(FIXTURE_DIR, self.FIXTURE)) as fh:
            text = fh.read()
        assert _looks_oo(text) is True

    def test_facts_match_golden(self, facts, golden):
        # We compare on the (kind, source, target, line, language)
        # tuple so the test is robust to test-runtime CWD differences
        # in ``file_path``. Symbol IDs derived from the fixture path
        # are normalised the same way on both sides.
        fixture_suffix = "tests/fixtures/extractor/abap/zcl_all_edges.clas.abap"

        def normalise(value: str) -> str:
            idx = value.find(fixture_suffix)
            if idx == -1:
                return value
            return "abap:" + fixture_suffix + value[idx + len(fixture_suffix) :]

        def key(rec):
            if isinstance(rec, dict):
                return (
                    rec["kind"],
                    normalise(rec["source_symbol_id"]),
                    normalise(rec["target_symbol_id"]),
                    rec["line"],
                    rec["language"],
                )
            return (
                rec.kind,
                normalise(rec.source_symbol_id),
                normalise(rec.target_symbol_id),
                rec.line,
                rec.language,
            )

        actual = sorted(key(f) for f in facts)
        expected = sorted(key(g) for g in golden)
        assert actual == expected

    def test_total_fact_count(self, facts, golden):
        assert len(facts) == len(golden)

    def test_class_declaration_emits_defines(self, facts):
        targets = {f.target_symbol_id for f in facts if f.kind == "defines"}
        assert any("ZCL_ALL_EDGES" in t for t in targets)
        assert any("ZCL_ALL_EDGES::RUN" in t for t in targets)
        assert any("ZCL_ALL_EDGES::LOAD" in t for t in targets)
        assert any("ZCL_ALL_EDGES::HELPER" in t for t in targets)

    def test_create_object_resolves_to_declared_class(self, facts):
        targets = [f.target_symbol_id for f in facts if f.kind == "calls"]
        assert "abap:ZCL_OTHER::<constructor>" in targets

    def test_instance_call_resolves_via_data_decl(self, facts):
        targets = [f.target_symbol_id for f in facts if f.kind == "calls"]
        assert "abap:ZCL_OTHER::DO_WORK" in targets

    def test_static_functional_call(self, facts):
        targets = [f.target_symbol_id for f in facts if f.kind == "calls"]
        assert "abap:ZCL_STATIC::COMPUTE" in targets

    def test_legacy_call_method_static_via_macro_workaround(self, facts):
        # ``CALL METHOD zcl_legacy=>log.`` parses as a ``macro_include``
        # — the walk pattern-matches that shape to keep the legacy
        # static-call syntax in the fact stream.
        targets = [f.target_symbol_id for f in facts if f.kind == "calls"]
        assert "abap:ZCL_LEGACY::LOG" in targets

    def test_call_function_module(self, facts):
        targets = [f.target_symbol_id for f in facts if f.kind == "calls"]
        assert "abap:BAPI_COMMIT::*" in targets

    def test_perform_emits_call(self, facts):
        targets = [f.target_symbol_id for f in facts if f.kind == "calls"]
        assert any("CLEANUP" in t for t in targets)

    def test_include_inside_method_emits_import(self, facts):
        imports = [f for f in facts if f.kind == "imports"]
        assert any("ZINC_RUNTIME" in f.target_symbol_id for f in imports)


@pytest.mark.skipif(
    not _HAS_TS_ABAP,
    reason="tree-sitter-abap grammar not installed in this environment",
)
class TestTreeSitterTopLevel:
    """REPORT / INCLUDE / FUNCTION at top level must emit the right
    facts even when the grammar's procedural-statement coverage is
    partial.
    """

    def test_report_emits_defines(self):
        src = b"REPORT z_demo.\nCLASS ZCL_X DEFINITION.\nENDCLASS.\n"
        facts = list(_extract_via_tree_sitter("z_demo.abap", src))
        defines = [f.target_symbol_id for f in facts if f.kind == "defines"]
        assert any(t.endswith("::Z_DEMO") for t in defines)

    def test_function_implementation_emits_defines(self):
        src = (
            b"FUNCTION zf_run.\nENDFUNCTION.\n"
            b"CLASS ZCL_X DEFINITION.\nENDCLASS.\n"
        )
        facts = list(_extract_via_tree_sitter("zf_run.abap", src))
        defines = [f.target_symbol_id for f in facts if f.kind == "defines"]
        assert any(t.endswith("::ZF_RUN") for t in defines)

    def test_top_level_include_emits_imports(self):
        src = (
            b"INCLUDE z_top.\n"
            b"CLASS ZCL_X DEFINITION.\nENDCLASS.\n"
        )
        facts = list(_extract_via_tree_sitter("x.abap", src))
        imports = [f for f in facts if f.kind == "imports"]
        assert any("Z_TOP" in f.target_symbol_id for f in imports)
