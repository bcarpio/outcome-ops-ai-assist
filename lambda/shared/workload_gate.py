"""Per-workload Bedrock concurrency gate (fable-scale chunk 05).

All four workloads (chat, code-maps, code-gen, pr-analyzer) share the same
per-model Bedrock on-demand quota. Without fairness controls, a batch burst
(200 PR checks, a re-ingestion run) can consume every Bedrock inference slot
and starve the interactive chat surface.

This module provides a per-workload semaphore gate. Each Lambda instance /
Fargate task holds N slots per workload. Callers acquire before an invoke
and release on exit. If the gate is saturated, the caller either waits up
to `timeout` seconds or raises `WorkloadBackpressureError` so the surface
can degrade gracefully (chat streams a `busy_retrying` event instead of a
500).

Gate slots are per-instance, not global — enforcing a strict org-wide cap
would need DDB atomic counters or a rate-limit sidecar. Per-instance is
enough at the concurrency levels we see in practice: chat Lambda concurrency
* per-instance chat slots >> single-model TPM quota.

Defaults are conservative (chat gets the fattest slice); operators can tune
via env vars:
    BEDROCK_GATE_CHAT          (default 200)
    BEDROCK_GATE_CODE_MAPS     (default 50)
    BEDROCK_GATE_CODE_GEN      (default 30)
    BEDROCK_GATE_PR_ANALYZER   (default 50)
    BEDROCK_GATE_ACQUIRE_TIMEOUT_SECONDS (default 5.0)
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


class WorkloadBackpressureError(Exception):
    """Raised when the workload gate can't be acquired within its timeout.

    Distinct from ThrottlingException — this fires BEFORE the Bedrock call,
    i.e., we shed load rather than adding it to a saturated model quota.
    Chat treats this as retryable with backoff (streams busy_retrying);
    batch callers can log + requeue.
    """


_WORKLOAD_DEFAULTS = {
    "chat": 200,
    "code-maps": 50,
    "code-gen": 30,
    "pr-analyzer": 50,
}


_GATE_ACQUIRE_TIMEOUT = float(
    os.environ.get("BEDROCK_GATE_ACQUIRE_TIMEOUT_SECONDS", "5.0")
)


def _capacity_for(workload: str) -> int:
    env_key = "BEDROCK_GATE_" + workload.upper().replace("-", "_")
    default = _WORKLOAD_DEFAULTS.get(workload, 50)
    try:
        return int(os.environ.get(env_key, str(default)))
    except ValueError:
        return default


# Semaphores are created lazily on first acquire — this keeps import-time
# side effects minimal (unit tests don't need to set env before import).
_gates: dict[str, threading.BoundedSemaphore] = {}
_gates_lock = threading.Lock()


def _get_gate(workload: str) -> threading.BoundedSemaphore:
    with _gates_lock:
        gate = _gates.get(workload)
        if gate is None:
            capacity = _capacity_for(workload)
            gate = threading.BoundedSemaphore(capacity)
            _gates[workload] = gate
        return gate


@contextmanager
def acquire(
    workload: str,
    timeout: Optional[float] = None,
) -> Iterator[None]:
    """Acquire the gate for `workload`, blocking up to `timeout` seconds.

    Raises `WorkloadBackpressureError` on acquisition failure.

    Usage:
        with workload_gate.acquire("chat"):
            response = bedrock.converse(...)
    """
    effective_timeout = timeout if timeout is not None else _GATE_ACQUIRE_TIMEOUT
    gate = _get_gate(workload)
    acquired = gate.acquire(timeout=effective_timeout)
    if not acquired:
        logger.warning(
            "workload gate saturated: workload=%s timeout=%.1fs capacity=%d",
            workload, effective_timeout, _capacity_for(workload),
        )
        raise WorkloadBackpressureError(
            f"workload gate '{workload}' saturated (capacity {_capacity_for(workload)})"
        )
    try:
        yield
    finally:
        gate.release()


def reset_for_test() -> None:
    """Test helper: drop all gate instances so the next acquire builds fresh
    ones from the current env."""
    with _gates_lock:
        _gates.clear()
