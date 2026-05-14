"""Unit tests for the Go extractor (plan-gkg chunk 01)."""

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
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "go")
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "go"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestAllEdges:
    @pytest.fixture
    def facts(self):
        return _load("all_edges.go")

    def test_imports(self, facts):
        imports = _by_kind(facts, "imports")
        targets = [f.target_symbol_id for f in imports]
        assert any("go:fmt::*" in t for t in targets)
        assert any("go:github.com/foo/bar::*" in t for t in targets)

    def test_method_namespace_uses_receiver_type(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any("Service::Confirm" in i for i in ids)

    def test_top_level_func(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any(i.endswith("::TopLevel") for i in ids)
        assert any(i.endswith("::helper") for i in ids)

    def test_selector_call_preserved(self, facts):
        calls = _by_kind(facts, "calls")
        targets = [f.target_symbol_id for f in calls]
        assert any("fmt.Println" in t for t in targets)
        assert any("bar.DoThing" in t for t in targets)


class TestNameCollisions:
    @pytest.fixture
    def facts(self):
        return _load("name_collisions.go")

    def test_distinct_method_ids(self, facts):
        defines = _by_kind(facts, "defines")
        ids = {f.target_symbol_id for f in defines}
        a_methods = [i for i in ids if "A::Helper" in i]
        b_methods = [i for i in ids if "B::Helper" in i]
        assert a_methods and b_methods
