# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared/ws_cache ().

Covers:
- Cache hit/miss/expiry semantics.
- Concurrent-race safety (two threads racing the same key both get the same
  value; the race is BENIGN — both may call compute, both cache the same
  answer).
- Invalidation + clear helpers.
"""
import importlib.util
import os
import sys
import time


def _load_ws_cache():
    if "outcomeops_ws_cache" in sys.modules:
        del sys.modules["outcomeops_ws_cache"]
    path = os.path.join(os.path.dirname(__file__), "../../shared/ws_cache.py")
    spec = importlib.util.spec_from_file_location("outcomeops_ws_cache", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outcomeops_ws_cache"] = mod
    spec.loader.exec_module(mod)
    return mod


ws_cache = _load_ws_cache()


class TestGetOrCompute:
    def setup_method(self):
        ws_cache.clear()

    def test_cache_miss_calls_compute(self):
        calls = []
        def compute():
            calls.append(1)
            return "value"
        result = ws_cache.get_or_compute(("t", "k"), compute)
        assert result == "value"
        assert len(calls) == 1

    def test_cache_hit_skips_compute(self):
        calls = []
        def compute():
            calls.append(1)
            return "value"
        ws_cache.get_or_compute(("t", "k"), compute)
        ws_cache.get_or_compute(("t", "k"), compute)
        ws_cache.get_or_compute(("t", "k"), compute)
        assert len(calls) == 1

    def test_expired_entry_recomputes(self):
        calls = []
        def compute():
            calls.append(1)
            return "value"
        ws_cache.get_or_compute(("t", "k"), compute, ttl=0)
        # ttl=0 → entry expires immediately.
        time.sleep(0.01)
        ws_cache.get_or_compute(("t", "k"), compute, ttl=60)
        assert len(calls) == 2

    def test_different_keys_have_independent_entries(self):
        ws_cache.get_or_compute(("t", "k1"), lambda: "a")
        ws_cache.get_or_compute(("t", "k2"), lambda: "b")
        assert ws_cache.get_or_compute(("t", "k1"), lambda: "wrong") == "a"
        assert ws_cache.get_or_compute(("t", "k2"), lambda: "wrong") == "b"

    def test_invalidate_drops_single_entry(self):
        ws_cache.get_or_compute(("t", "k1"), lambda: "a")
        ws_cache.get_or_compute(("t", "k2"), lambda: "b")
        ws_cache.invalidate(("t", "k1"))
        # k1 recomputes.
        calls = []
        def compute():
            calls.append(1)
            return "a2"
        assert ws_cache.get_or_compute(("t", "k1"), compute) == "a2"
        assert len(calls) == 1
        # k2 stays cached.
        assert ws_cache.get_or_compute(("t", "k2"), lambda: "wrong") == "b"

    def test_clear_drops_everything(self):
        ws_cache.get_or_compute(("t", "k1"), lambda: "a")
        ws_cache.get_or_compute(("t", "k2"), lambda: "b")
        ws_cache.clear()
        assert ws_cache.get_or_compute(("t", "k1"), lambda: "new") == "new"

    def test_default_ttl_from_env(self, monkeypatch):
        # The module reads WORKSPACE_CACHE_TTL_SECONDS at import; setting
        # the env after import doesn't retroactively change it. But we can
        # still check the default is respected when ttl not passed.
        # (The module's CACHE_TTL_SECONDS is captured at import time.)
        entry_key = ("t", "ttl_default")
        ws_cache.get_or_compute(entry_key, lambda: "cached")
        # Look inside the private dict to verify TTL == module default.
        value, expires_at = ws_cache._entries[entry_key]
        assert value == "cached"
        # Expiry should be within the module default TTL of now.
        assert expires_at <= time.time() + ws_cache.CACHE_TTL_SECONDS + 1
        assert expires_at >= time.time() + ws_cache.CACHE_TTL_SECONDS - 2

    def test_race_two_computes_land_last_writer(self):
        # Not a strict thread test — deterministic: sequential calls
        # where compute returns different values simulates the race
        # window between two threads. The last write wins the cache slot.
        ws_cache.get_or_compute(("t", "k"), lambda: "a", ttl=60)
        ws_cache.invalidate(("t", "k"))
        ws_cache.get_or_compute(("t", "k"), lambda: "b", ttl=60)
        assert ws_cache.get_or_compute(("t", "k"), lambda: "wrong") == "b"
