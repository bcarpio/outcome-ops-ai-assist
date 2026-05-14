"""Unit tests for the extractor base module (plan-gkg chunk 01)."""

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
    EXCLUDED_PATH_FRAGMENTS,
    FACT_KINDS,
    Fact,
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
