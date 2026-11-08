"""Unit tests for the Java cross-module reference resolver (ADR-055).

Black-box over the Java extractor + the language-agnostic resolution pass: extract
two fixture files, concatenate their facts, run ``resolve_repo_facts``, and assert
that cross-package references rebind to their definition ids while unresolvable
references stay raw (never-guess).
"""

from __future__ import annotations

import os
import sys

import pytest

EXTRACTOR_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "process-batch-summary")
)
if EXTRACTOR_PARENT not in sys.path:
    sys.path.insert(0, EXTRACTOR_PARENT)

from extractor import extract_facts, resolve_repo_facts  # noqa: E402

FIXTURE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "java")
)


def _load(name):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "java"))


def _one(facts, kind, pred):
    hits = [f for f in facts if f.kind == kind and pred(f)]
    assert len(hits) == 1, f"expected exactly one {kind} match, got {len(hits)}"
    return hits[0]


class TestJavaResolutionFixtures:
    @pytest.fixture
    def facts(self):
        return _load("resolve_defs.java") + _load("resolve_caller.java")

    @pytest.fixture
    def resolved(self, facts):
        return resolve_repo_facts(facts)

    @pytest.fixture
    def widget_def(self, resolved):
        return _one(
            resolved, "defines", lambda f: f.target_symbol_id.endswith("::Widget")
        ).target_symbol_id

    @pytest.fixture
    def target_method_def(self, resolved):
        return _one(
            resolved,
            "defines",
            lambda f: f.target_symbol_id.endswith("::Widget::targetMethod"),
        ).target_symbol_id

    def test_cross_package_method_call_resolves(self, resolved, target_method_def):
        """`w.targetMethod()` in com.outcomeops.caller binds to the definition in
        com.outcomeops.defs (unique-name fallback across packages)."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::targetMethod"),
        )
        assert call.target_symbol_id == target_method_def
        assert "com.outcomeops.defs" in call.target_symbol_id

    def test_extends_resolves_via_fqn_import(self, resolved, widget_def):
        """`SubWidget extends Widget` binds to the imported Widget definition."""
        ext = _one(resolved, "extends", lambda f: True)
        assert ext.target_symbol_id == widget_def

    def test_import_resolves_to_definition(self, resolved, widget_def):
        """`import com.outcomeops.defs.Widget;` binds to the Widget definition so
        importers() works."""
        imp = _one(resolved, "imports", lambda f: True)
        assert imp.target_symbol_id == widget_def

    def test_unresolvable_call_left_raw(self, resolved):
        """undefinedHelper() is neither imported nor defined -> raw."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::undefinedHelper"),
        )
        assert "com.outcomeops.caller" in call.target_symbol_id

    def test_resolution_is_idempotent(self, facts):
        once = resolve_repo_facts(facts)
        twice = resolve_repo_facts(once)
        key = lambda fs: sorted(
            (f.kind, f.source_symbol_id, f.target_symbol_id) for f in fs
        )
        assert key(once) == key(twice)
