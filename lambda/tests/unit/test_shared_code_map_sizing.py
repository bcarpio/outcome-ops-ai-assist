"""Unit tests for shared/code_map_sizing.py (chunk 01)."""

import importlib
import os
import sys

import pytest


_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)


def _reload_module():
    """Force-reimport so env-var-driven module-scope constants pick up
    changes between tests."""
    if "shared.code_map_sizing" in sys.modules:
        return importlib.reload(sys.modules["shared.code_map_sizing"])
    import shared.code_map_sizing  # noqa: F401
    return sys.modules["shared.code_map_sizing"]


class TestEstimateTokens:
    def test_uses_char_over_three_heuristic(self):
        sizing = _reload_module()
        assert sizing.estimate_tokens("abc") == 1
        assert sizing.estimate_tokens("a" * 300) == 100

    def test_empty_string(self):
        sizing = _reload_module()
        assert sizing.estimate_tokens("") == 0


class TestEstimateBatchTokens:
    def test_template_overhead_only_on_empty_batch(self):
        sizing = _reload_module()
        # Empty batch is just the fixed template overhead.
        assert sizing.estimate_batch_tokens([]) == sizing._TEMPLATE_OVERHEAD_TOKENS

    def test_sums_per_file_content_plus_overhead(self):
        sizing = _reload_module()
        files = [
            ("a.py", "x" * 300),   # 100 content tokens
            ("b.py", "y" * 600),   # 200 content tokens
        ]
        est = sizing.estimate_batch_tokens(files)
        # 500 template + 2*30 per-file + 2*~1 (path) + 100 + 200
        assert est >= 500 + 60 + 300  # >=860
        assert est <= 500 + 60 + 300 + 10  # slack for path token estimates

    def test_accepts_generator(self):
        sizing = _reload_module()
        gen = (("f.py", "code") for _ in range(3))
        # Should not raise on iterables
        est = sizing.estimate_batch_tokens(gen)
        assert est > sizing._TEMPLATE_OVERHEAD_TOKENS


class TestEnvOverrides:
    def test_target_override_from_env(self, monkeypatch):
        monkeypatch.setenv("CODE_MAP_TARGET_SHARD_TOKENS", "40000")
        sizing = _reload_module()
        assert sizing.TARGET_SHARD_TOKENS == 40000

    def test_max_override_from_env(self, monkeypatch):
        monkeypatch.setenv("CODE_MAP_MAX_PROMPT_TOKENS", "180000")
        sizing = _reload_module()
        assert sizing.MAX_PROMPT_TOKENS == 180000

    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("CODE_MAP_TARGET_SHARD_TOKENS", raising=False)
        monkeypatch.delenv("CODE_MAP_MAX_PROMPT_TOKENS", raising=False)
        sizing = _reload_module()
        assert sizing.TARGET_SHARD_TOKENS == 120000
        assert sizing.MAX_PROMPT_TOKENS == 150000

    def test_target_stays_under_max_at_defaults(self, monkeypatch):
        """Regression: if defaults ever get reordered the splitter
        would send messages larger than the consumer's ceiling."""
        monkeypatch.delenv("CODE_MAP_TARGET_SHARD_TOKENS", raising=False)
        monkeypatch.delenv("CODE_MAP_MAX_PROMPT_TOKENS", raising=False)
        sizing = _reload_module()
        assert sizing.TARGET_SHARD_TOKENS < sizing.MAX_PROMPT_TOKENS
