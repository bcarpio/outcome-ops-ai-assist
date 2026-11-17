# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared/workload_gate ().

Covers:
- Basic acquire/release semantics.
- Saturation raises WorkloadBackpressureError after timeout.
- Env-var capacity overrides.
- Reset helper drops singletons so env changes take effect.
- Unknown workload falls back to default capacity.
"""
import importlib
import os
import sys
import threading
import time


def _load(monkeypatch=None):
    if "outcomeops_workload_gate" in sys.modules:
        del sys.modules["outcomeops_workload_gate"]
    lambda_root = os.path.join(os.path.dirname(__file__), "../..")
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    return importlib.import_module("shared.workload_gate")


class TestBasicAcquire:
    def test_acquire_release_normal_path(self):
        mod = _load()
        mod.reset_for_test()
        with mod.acquire("chat"):
            pass  # released on exit

    def test_multiple_sequential_acquires(self):
        mod = _load()
        mod.reset_for_test()
        for _ in range(5):
            with mod.acquire("chat"):
                pass


class TestSaturation:
    def test_saturation_raises_after_timeout(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GATE_CHAT", "1")
        monkeypatch.setenv("BEDROCK_GATE_ACQUIRE_TIMEOUT_SECONDS", "0.1")
        mod = _load()
        mod.reset_for_test()

        # Hold the one slot. A second acquire must fail.
        release_evt = threading.Event()
        acquired_evt = threading.Event()

        def holder():
            with mod.acquire("chat"):
                acquired_evt.set()
                release_evt.wait(timeout=5)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        acquired_evt.wait(timeout=2)

        # Now try acquire — should raise within timeout window.
        raised = False
        try:
            with mod.acquire("chat", timeout=0.1):
                pass
        except mod.WorkloadBackpressureError:
            raised = True

        release_evt.set()
        t.join(timeout=2)
        assert raised, "expected WorkloadBackpressureError on saturated gate"

    def test_slot_released_after_error_in_with_block(self):
        mod = _load()
        mod.reset_for_test()
        try:
            with mod.acquire("chat", timeout=1.0):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # A subsequent acquire should still succeed — slot released.
        with mod.acquire("chat", timeout=1.0):
            pass


class TestEnvCapacity:
    def test_env_override_applied(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GATE_CODE_MAPS", "7")
        mod = _load()
        mod.reset_for_test()
        # Trigger lazy build.
        with mod.acquire("code-maps", timeout=0.1):
            pass
        assert mod._capacity_for("code-maps") == 7

    def test_unknown_workload_uses_default(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GATE_BATCH", raising=False)
        mod = _load()
        mod.reset_for_test()
        # No env, no default entry — falls back to the module's generic default (50).
        assert mod._capacity_for("batch") == 50

    def test_bad_env_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GATE_CHAT", "not-a-number")
        mod = _load()
        mod.reset_for_test()
        assert mod._capacity_for("chat") == 200  # module default


class TestResetForTest:
    def test_reset_drops_singletons(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GATE_CHAT", "5")
        mod = _load()
        mod.reset_for_test()
        with mod.acquire("chat"):
            pass
        # Change env after gate built.
        monkeypatch.setenv("BEDROCK_GATE_CHAT", "1")
        # Without reset, old capacity persists.
        assert mod._capacity_for("chat") == 1  # capacity_for reads env each call
        # But the semaphore was built at old capacity. Reset drops it so
        # next acquire builds fresh at capacity=1.
        mod.reset_for_test()
        with mod.acquire("chat"):
            pass
