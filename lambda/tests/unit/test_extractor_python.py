"""Unit tests for the Python extractor (plan-gkg chunk 01)."""

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


FIXTURE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "python")
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "python"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestAllEdges:
    """all_edges.py exercises every Python edge kind."""

    @pytest.fixture
    def facts(self):
        return _load("all_edges.py")

    def test_imports_present(self, facts):
        imports = _by_kind(facts, "imports")
        targets = [f.target_symbol_id for f in imports]
        assert "python:os::*" in targets
        assert "python:typing::List" in targets
        assert "python:helpers::normalize" in targets

    def test_class_and_method_defines(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any(i.endswith("::Service") for i in ids)
        assert any(i.endswith("::Service::run") for i in ids)
        assert any(i.endswith("::top_level") for i in ids)

    def test_extends_edge(self, facts):
        extends = _by_kind(facts, "extends")
        assert any("Base" in f.target_symbol_id for f in extends)

    def test_calls_resolve_self_to_unresolved_name(self, facts):
        calls = _by_kind(facts, "calls")
        # ``self.hello()`` -> emits unresolved name 'hello' per plan
        assert any(f.target_symbol_id.endswith("::hello") for f in calls)

    def test_module_attribute_call_preserved(self, facts):
        calls = _by_kind(facts, "calls")
        assert any("os.getcwd" in f.target_symbol_id for f in calls)


class TestNameCollisions:
    """Same name in different scopes must be namespaced distinctly."""

    @pytest.fixture
    def facts(self):
        return _load("name_collisions.py")

    def test_distinct_method_ids(self, facts):
        ids = {f.target_symbol_id for f in facts if f.kind == "defines"}
        # The module-level ``helper`` and the two class methods must
        # carry different canonical IDs.
        helpers = [i for i in ids if i.endswith("::helper") or "::helper" in i]
        assert len(helpers) >= 3


class TestReportReposSnapshot:
    """The load-bearing v1 success-case fixture.

    The plan's DoD says the extractor must surface 10 distinct callers
    of ``report_repos_snapshot``. Chunk 08 reuses this fixture as the
    live-data signal so the test name and assertion are stable.
    """

    @pytest.fixture
    def facts(self):
        return _load("report_repos_snapshot_callers.py")

    def test_one_import_of_symbol(self, facts):
        imports = _by_kind(facts, "imports")
        snap_imports = [
            f for f in imports if f.target_symbol_id.endswith("::report_repos_snapshot")
        ]
        assert len(snap_imports) == 1

    def test_ten_distinct_callers(self, facts):
        calls = _by_kind(facts, "calls")
        snap_calls = [
            f
            for f in calls
            if f.target_symbol_id.endswith("::report_repos_snapshot")
        ]
        callers = {f.source_symbol_id for f in snap_calls}
        assert len(callers) == 10, sorted(callers)


class TestExtractorBehavior:
    def test_unknown_language_returns_empty(self, tmp_path):
        p = tmp_path / "x.unknown"
        p.write_bytes(b"")
        assert list(extract_facts(str(p), "unknown-lang")) == []

    def test_missing_file_returns_empty(self):
        assert list(extract_facts("/nonexistent/path/x.py", "python")) == []

    def test_excluded_path_short_circuits(self, tmp_path):
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        f = node_modules / "x.py"
        f.write_text("def excluded(): pass\n")
        assert list(extract_facts(str(f), "python")) == []

    def test_extract_facts_from_bytes_round_trip(self):
        from extractor import extract_facts_from_bytes

        src = b"def foo():\n    bar()\n"
        facts = list(extract_facts_from_bytes("ex.py", "python", src))
        kinds = {f.kind for f in facts}
        assert "defines" in kinds and "calls" in kinds

    def test_extract_facts_from_bytes_skips_excluded(self):
        from extractor import extract_facts_from_bytes

        # ``node_modules/`` in the path -> excluded by ADR-009 alignment.
        facts = list(
            extract_facts_from_bytes(
                "node_modules/lib/x.py", "python", b"def f(): pass\n"
            )
        )
        assert facts == []

    def test_extract_facts_from_bytes_unknown_lang(self):
        from extractor import extract_facts_from_bytes

        assert list(extract_facts_from_bytes("x.py", "unknown", b"")) == []
