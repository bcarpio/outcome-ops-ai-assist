# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the C# cross-module reference resolver (ADR-055).

Black-box over the C# extractor + the language-agnostic resolution pass: extract
two fixture files, concatenate their facts, run ``resolve_repo_facts``, and assert
that cross-namespace references rebind to their definition ids while unresolvable
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
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "csharp")
)


def _load(name):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "csharp"))


def _one(facts, kind, pred):
    hits = [f for f in facts if f.kind == kind and pred(f)]
    assert len(hits) == 1, f"expected exactly one {kind} match, got {len(hits)}"
    return hits[0]


class TestCsharpResolutionFixtures:
    @pytest.fixture
    def facts(self):
        return _load("resolve_defs.cs") + _load("resolve_caller.cs")

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
            lambda f: f.target_symbol_id.endswith("::Widget::TargetMethod"),
        ).target_symbol_id

    def test_cross_namespace_method_call_resolves(self, resolved, target_method_def):
        """`w.TargetMethod()` in OutcomeOps.Caller binds to the definition in
        OutcomeOps.Defs."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::TargetMethod"),
        )
        assert call.target_symbol_id == target_method_def
        assert "OutcomeOps.Defs" in call.target_symbol_id
        assert "OutcomeOps.Caller::TargetMethod" not in call.target_symbol_id

    def test_type_reference_call_resolves(self, resolved, widget_def):
        """`new Widget()` binds to the Widget type definition."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::Widget")
            and f.source_symbol_id.endswith("::Go"),
        )
        assert call.target_symbol_id == widget_def

    def test_extends_resolves_to_base_definition(self, resolved, widget_def):
        """`SubWidget : Widget` extends binds to the Widget definition."""
        ext = _one(resolved, "extends", lambda f: True)
        assert ext.target_symbol_id == widget_def

    def test_unresolvable_call_left_raw(self, resolved):
        """UndefinedHelper() is neither using-imported nor defined -> raw."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::UndefinedHelper"),
        )
        # Stays in the CALLER's namespace -- never guessed to a definition.
        assert "OutcomeOps.Caller" in call.target_symbol_id

    def test_namespace_using_left_raw(self, resolved):
        """A plain namespace `using` names no single definition -> raw."""
        imp = _one(resolved, "imports", lambda f: True)
        assert imp.target_symbol_id == "csharp:OutcomeOps::Defs"

    def test_resolution_is_idempotent(self, facts):
        once = resolve_repo_facts(facts)
        twice = resolve_repo_facts(once)
        key = lambda fs: sorted(
            (f.kind, f.source_symbol_id, f.target_symbol_id) for f in fs
        )
        assert key(once) == key(twice)
