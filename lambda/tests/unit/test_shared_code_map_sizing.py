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


class TestPackFilesIntoShards:
    def test_empty_input_returns_empty(self):
        sizing = _reload_module()
        assert sizing.pack_files_into_shards([], 100_000) == []

    def test_single_file_fits_in_one_shard(self):
        sizing = _reload_module()
        shards = sizing.pack_files_into_shards([("a.py", 3000)], 100_000)
        assert shards == [["a.py"]]

    def test_multiple_small_files_pack_into_one_shard(self):
        sizing = _reload_module()
        files = [(f"f{i}.py", 3000) for i in range(10)]
        shards = sizing.pack_files_into_shards(files, 100_000)
        assert len(shards) == 1
        assert len(shards[0]) == 10

    def test_files_exceeding_target_split_into_multiple_shards(self):
        sizing = _reload_module()
        # 20 files @ 30k bytes each = ~200k tokens total, target 100k
        # tokens per shard -> expect ~2-3 shards.
        files = [(f"f{i}.py", 30_000) for i in range(20)]
        shards = sizing.pack_files_into_shards(files, 100_000)
        assert len(shards) >= 2
        # Every file present exactly once
        packed = [p for shard in shards for p in shard]
        assert sorted(packed) == sorted(f"f{i}.py" for i in range(20))

    def test_single_huge_file_gets_own_shard(self):
        sizing = _reload_module()
        # 500k bytes ~ 166k tokens > target 120k
        shards = sizing.pack_files_into_shards(
            [("small.py", 3000), ("huge.py", 500_000), ("tiny.py", 500)],
            120_000,
        )
        # huge.py should be alone in its shard
        huge_shard = next(s for s in shards if "huge.py" in s)
        assert huge_shard == ["huge.py"]

    def test_first_fit_decreasing_is_deterministic(self):
        sizing = _reload_module()
        files = [("a.py", 30_000), ("b.py", 30_000), ("c.py", 30_000)]
        s1 = sizing.pack_files_into_shards(files, 100_000)
        s2 = sizing.pack_files_into_shards(files, 100_000)
        assert s1 == s2


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
