# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the extractor base module."""

from __future__ import annotations

import os
import sys

import pytest

# Make the extractor package importable.
EXTRACTOR_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "process-batch-summary")
)
if EXTRACTOR_PARENT not in sys.path:
    sys.path.insert(0, EXTRACTOR_PARENT)

from extractor.base import (  # noqa: E402
    DEFAULT_SYMBOL_GRAPH_FILTER,
    EXCLUDED_PATH_FRAGMENTS,
    FACT_KINDS,
    Fact,
    SymbolGraphFilter,
    get_extractor,
    is_excluded_path,
    make_symbol_id,
    register_language,
    supported_languages,
)


class TestFact:
    def test_valid_fact_constructs(self):
        f = Fact(
            kind="defines",
            source_symbol_id="python:m::<module>",
            target_symbol_id="python:m::foo",
            file_path="m.py",
            line=1,
            language="python",
        )
        assert f.kind == "defines"
        assert f.line == 1

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValueError):
            Fact(
                kind="bogus",
                source_symbol_id="a",
                target_symbol_id="b",
                file_path="x.py",
                line=1,
                language="python",
            )

    def test_zero_line_rejected(self):
        with pytest.raises(ValueError):
            Fact(
                kind="defines",
                source_symbol_id="a",
                target_symbol_id="b",
                file_path="x.py",
                line=0,
                language="python",
            )

    def test_fact_is_hashable(self):
        f1 = Fact("defines", "a", "b", "x.py", 1, "python")
        f2 = Fact("defines", "a", "b", "x.py", 1, "python")
        assert {f1, f2} == {f1}


class TestSymbolId:
    def test_no_namespace(self):
        assert make_symbol_id("python", "mod", "foo") == "python:mod::foo"

    def test_with_namespace(self):
        assert (
            make_symbol_id("python", "mod", "bar", namespace="Foo")
            == "python:mod::Foo::bar"
        )

    def test_wildcard_symbol(self):
        assert (
            make_symbol_id("typescript", "@x/y/z", "*")
            == "typescript:@x/y/z::*"
        )


class TestExclusion:
    @pytest.mark.parametrize(
        "path",
        [
            "node_modules/foo/bar.ts",
            "venv/lib/x.py",
            "lambda/.venv/lib/x.py",
            "build/output.js",
            "src/test/java/Foo.java",
            "lambda/__pycache__/cache.pyc",
            "generated/sdk/api.ts",
        ],
    )
    def test_excluded_paths(self, path):
        assert is_excluded_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "lambda/chat/handler.py",
            "ui/src/lib/api.ts",
            "src/main/java/Foo.java",
            "internal/booking/handler.go",
        ],
    )
    def test_included_paths(self, path):
        assert is_excluded_path(path) is False

    def test_exclusion_list_non_empty(self):
        assert len(EXCLUDED_PATH_FRAGMENTS) > 0

    def test_fact_kinds_known(self):
        assert "defines" in FACT_KINDS
        assert "imports" in FACT_KINDS
        assert "calls" in FACT_KINDS


class TestRegistry:
    def test_registered_languages_present(self):
        # importing the package registers every language; this test
        # depends only on the side-effect import from extractor/__init__.py
        import extractor  # noqa: F401

        langs = supported_languages()
        assert {"python", "typescript", "java", "csharp", "go", "abap"} <= langs

    def test_get_unknown_extractor_returns_none(self):
        assert get_extractor("not-a-language") is None

    def test_register_language_roundtrip(self):
        def noop(_path, _src):
            return iter(())

        register_language("__test_lang__", noop)
        try:
            assert get_extractor("__test_lang__") is noop
        finally:
            # cleanup the registry to avoid leaking state into other tests
            from extractor.base import _REGISTRY  # type: ignore

            _REGISTRY.pop("__test_lang__", None)


class TestSymbolGraphFilter:
    """Per-repo extraction filter precedence."""

    def test_default_filter_accepts_typical_file(self):
        f = DEFAULT_SYMBOL_GRAPH_FILTER
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 1024) is True
        )

    def test_disabled_short_circuits(self):
        f = SymbolGraphFilter(enabled=False)
        assert f.should_extract("lambda/chat/handler.py", "python", 100) is False

    def test_adr009_excludes_always_win(self):
        # Even with permissive include + disabled-everything-else,
        # ADR-009 base excludes are non-negotiable.
        f = SymbolGraphFilter(include=["**/*.py"])
        assert f.should_extract("node_modules/foo/bar.py", "python", 10) is False
        assert f.should_extract("venv/lib/x.py", "python", 10) is False
        assert f.should_extract("build/output.py", "python", 10) is False

    def test_exclude_beats_include(self):
        f = SymbolGraphFilter(
            include=["lambda/**/*.py"],
            exclude=["lambda/**/test_*.py"],
        )
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 100) is True
        )
        assert (
            f.should_extract("lambda/chat/test_foo.py", "python", 100) is False
        )

    def test_include_empty_means_include_all(self):
        f = SymbolGraphFilter(include=[])
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 100) is True
        )
        assert f.should_extract("ui/src/lib/api.ts", "typescript", 100) is True

    def test_include_non_match_rejected(self):
        f = SymbolGraphFilter(include=["lambda/**/*.py"])
        assert f.should_extract("ui/src/lib/api.ts", "typescript", 100) is False
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 100) is True
        )

    def test_language_allowlist(self):
        f = SymbolGraphFilter(languages=["python"])
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 100) is True
        )
        assert f.should_extract("ui/src/lib/api.ts", "typescript", 100) is False

    def test_language_none_means_all(self):
        f = SymbolGraphFilter(languages=None)
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 100) is True
        )
        assert f.should_extract("ui/src/lib/api.ts", "typescript", 100) is True

    def test_max_file_bytes_caps_size(self):
        f = SymbolGraphFilter(max_file_bytes=1024)
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 1024) is True
        )
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 1025) is False
        )

    def test_max_file_bytes_zero_disables_cap(self):
        f = SymbolGraphFilter(max_file_bytes=0)
        assert (
            f.should_extract("lambda/chat/handler.py", "python", 10**9) is True
        )

    def test_default_filter_defaults_match_pydantic_model(self):
        # Guards against drift between the Pydantic SymbolGraphConfig
        # in lambda/shared/repo_config.py and the dataclass mirror here.
        f = DEFAULT_SYMBOL_GRAPH_FILTER
        assert f.enabled is True
        assert f.include == []
        assert f.exclude == []
        assert f.max_file_bytes == 524288
        assert f.languages is None


class TestSignatureSchema:
    """plan-signature-extraction -- schema additions."""

    def test_param_defaults_type_to_none(self):
        from extractor.base import Param

        p = Param(name="x")
        assert p.name == "x"
        assert p.type is None

    def test_signature_defaults_to_empty_params_no_return(self):
        from extractor.base import Signature

        s = Signature()
        assert s.params == ()
        assert s.returns is None

    def test_signature_is_frozen_hashable(self):
        # Frozen + tuple of frozen Param dataclasses -> hashable,
        # so a Fact carrying a Signature stays hashable too.
        from extractor.base import Param, Signature

        s = Signature(params=(Param("a", "int"),), returns="bool")
        assert hash(s) == hash(s)

    def test_fact_accepts_signature_on_defines(self):
        from extractor.base import Fact, Param, Signature

        f = Fact(
            kind="defines",
            source_symbol_id="python:mod::*",
            target_symbol_id="python:mod::foo",
            file_path="mod.py",
            line=1,
            language="python",
            signature=Signature(params=(Param("a"),)),
        )
        assert f.signature is not None
        assert f.signature.params[0].name == "a"

    def test_fact_rejects_signature_on_non_defines(self):
        from extractor.base import Fact, Param, Signature

        with pytest.raises(ValueError, match="kind='defines'"):
            Fact(
                kind="calls",
                source_symbol_id="python:mod::foo",
                target_symbol_id="python:mod::bar",
                file_path="mod.py",
                line=10,
                language="python",
                signature=Signature(params=(Param("a"),)),
            )

    def test_fact_signature_is_optional(self):
        """Pre-existing Fact construction (no signature kwarg) still works."""
        from extractor.base import Fact

        f = Fact(
            kind="calls",
            source_symbol_id="python:mod::foo",
            target_symbol_id="python:mod::bar",
            file_path="mod.py",
            line=10,
            language="python",
        )
        assert f.signature is None
