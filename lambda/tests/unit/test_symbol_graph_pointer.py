"""Unit tests for ``shared.symbol_graph_pointer`` (plan-gkg chunk 03).

Covers the atomic begin -> commit / abandon protocol that owns the
``graph_sha`` / ``graph_pending_sha`` state machine. The concurrent-
regen scenario in particular is the safety-load-bearing case for the
plan: two workers racing for the same (workspace, repo) must not be
able to flip-flop the pointer.

Test isolation mirrors ``test_symbol_graph_writer.py``:

- ``mock_aws`` autouse so no test can touch real AWS endpoints.
- ``_set_resource_for_tests`` autouse injection so the pointer module's
  module-level boto3 reference can't resolve to the MagicMock that
  ``test_check_handlers_init`` plants in ``sys.modules`` under cross-
  suite ordering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from shared.symbol_graph_pointer import ...`` works
# without invoking the rest of the lambda/process-batch-summary handler.
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CODE_MAPS_TABLE = "test-code-maps"
WORKSPACE_ID = "ws-pointer-test"
REPO = "owner/pointer-repo"
SHA_A = "aaa111"
SHA_B = "bbb222"
SHA_C = "ccc333"


# ---------------------------------------------------------------------------
# Autouse fixtures — same pattern as test_symbol_graph_writer.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_pointer_clients():
    """Inject this file's real boto3.resource into the pointer module
    via ``_set_resource_for_tests`` so the writer's documented cross-
    suite contamination (test_check_handlers_init swaps
    ``sys.modules['boto3']`` with a MagicMock) cannot reach this
    module's lazy resource lookup.
    """
    from shared import symbol_graph_pointer  # type: ignore

    symbol_graph_pointer._reset_clients_for_tests()
    symbol_graph_pointer._set_resource_for_tests(
        boto3.resource("dynamodb", region_name="us-west-2")
    )
    yield
    symbol_graph_pointer._reset_clients_for_tests()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("CODE_MAPS_TABLE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield


# ---------------------------------------------------------------------------
# Table helper
# ---------------------------------------------------------------------------


def _create_code_maps_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=CODE_MAPS_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def code_maps_table():
    _create_code_maps_table()
    return boto3.resource("dynamodb", region_name="us-west-2").Table(
        CODE_MAPS_TABLE
    )


# ===========================================================================
# read_pointer
# ===========================================================================


def test_read_pointer_returns_none_for_first_time(code_maps_table):
    """No row, no attributes — both fields are None."""
    from shared.symbol_graph_pointer import read_pointer  # type: ignore

    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha is None
    assert state.graph_pending_sha is None


def test_read_pointer_returns_none_when_only_other_attrs_present(
    code_maps_table,
):
    """A row that exists for an unrelated reason (e.g. the writer's
    legacy ``current_commit_sha``) but has neither ``graph_sha`` nor
    ``graph_pending_sha`` still returns ``(None, None)`` — read_pointer
    is a clean accessor for THIS feature's attributes only."""
    from shared.symbol_graph_pointer import read_pointer  # type: ignore

    code_maps_table.put_item(
        Item={
            "PK": f"SYMBOL_GRAPH#{WORKSPACE_ID}#{REPO}",
            "SK": "POINTER",
            "current_commit_sha": "legacy-sha-from-writer",
        }
    )
    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha is None
    assert state.graph_pending_sha is None


# ===========================================================================
# begin_swap
# ===========================================================================


def test_begin_swap_sets_pending_and_leaves_graph_sha_untouched(
    code_maps_table,
):
    """First-time begin_swap puts graph_pending_sha; graph_sha stays None."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        read_pointer,
    )

    ok = begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert ok is True

    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha is None
    assert state.graph_pending_sha == SHA_A


def test_begin_swap_idempotent_for_same_sha(code_maps_table):
    """Re-claiming the same pending sha is a successful no-op (idempotent
    retry semantics — the Lambda may invoke twice on transient errors)."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        read_pointer,
    )

    assert begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    ) is True
    assert begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    ) is True

    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_pending_sha == SHA_A


# ===========================================================================
# commit_swap
# ===========================================================================


def test_commit_swap_advances_graph_sha_and_clears_pending(code_maps_table):
    """Happy path: begin -> commit moves pending into committed."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
        read_pointer,
    )

    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    ok = commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert ok is True

    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A
    assert state.graph_pending_sha is None


def test_commit_swap_only_succeeds_when_pending_matches(code_maps_table):
    """commit_swap is conditional on graph_pending_sha == new_sha.
    Calling with a different sha than what was claimed must fail."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
        read_pointer,
    )

    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    ok = commit_swap(
        WORKSPACE_ID, REPO, SHA_B, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert ok is False

    # Pending still SHA_A, graph_sha still None — the row is consistent.
    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha is None
    assert state.graph_pending_sha == SHA_A


def test_commit_swap_rejected_when_no_pending(code_maps_table):
    """commit without a preceding begin must fail — no row at all."""
    from shared.symbol_graph_pointer import commit_swap  # type: ignore

    ok = commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert ok is False


def test_commit_swap_idempotent_when_graph_sha_already_matches(code_maps_table):
    """Parallel-batch fallback: when a SECOND batch's commit_swap runs
    after the FIRST batch already committed (so graph_pending_sha is
    cleared but graph_sha == new_sha), commit_swap must return True
    rather than False. Without this idempotency, every late-arriving
    batch in a ~30-way concurrent regen logs a spurious WARNING and
    triggers abandon_swap in the caller, which would clear a valid
    pointer state. Live evidence: 2026-05-15 re-extract of
    outcome-ops-ai-assist-private.
    """
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
        read_pointer,
    )

    # First batch: begin + commit (the winner).
    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    first = commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert first is True

    # Second batch's commit on the SAME sha: pending is gone (the
    # first batch cleared it), but graph_sha already equals SHA_A.
    # The desired end-state is met — commit_swap returns True so the
    # caller doesn't abandon a valid pointer.
    second = commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert second is True

    # Pointer state unchanged across the idempotent no-op.
    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A
    assert state.graph_pending_sha is None


def test_commit_swap_still_rejects_when_graph_sha_is_different(code_maps_table):
    """Idempotent fallback must NOT swallow real conflicts. If
    graph_sha is committed to SHA_B but the caller tries to commit
    SHA_A (their regen lost the race to a newer regen for SHA_B),
    commit_swap returns False so the caller can abandon cleanly.
    """
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
    )

    # First regen lands at SHA_B.
    begin_swap(
        WORKSPACE_ID, REPO, SHA_B, code_maps_table_name=CODE_MAPS_TABLE
    )
    commit_swap(
        WORKSPACE_ID, REPO, SHA_B, code_maps_table_name=CODE_MAPS_TABLE
    )

    # A stale batch from an older regen tries to commit SHA_A — real
    # conflict, must return False.
    ok = commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert ok is False


# ===========================================================================
# abandon_swap
# ===========================================================================


def test_abandon_swap_clears_pending_without_affecting_graph_sha(
    code_maps_table,
):
    """abandon clears graph_pending_sha; an already-committed graph_sha
    keeps its value (the regen failure path must not strand the row)."""
    from shared.symbol_graph_pointer import (  # type: ignore
        abandon_swap,
        begin_swap,
        commit_swap,
        read_pointer,
    )

    # First successful regen: graph_sha = SHA_A.
    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )

    # Second regen begins for SHA_B but is abandoned.
    begin_swap(
        WORKSPACE_ID, REPO, SHA_B, code_maps_table_name=CODE_MAPS_TABLE
    )
    abandon_swap(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )

    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A, (
        "Abandon must not touch the committed graph_sha — readers still "
        "need to resolve the previous slice."
    )
    assert state.graph_pending_sha is None


def test_abandon_swap_is_idempotent_when_no_pending(code_maps_table):
    """Calling abandon when no pending swap exists is a successful no-op."""
    from shared.symbol_graph_pointer import abandon_swap  # type: ignore

    # No row yet — abandon should not raise.
    abandon_swap(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )

    # Now create a row with only graph_sha, no pending — abandon again.
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
    )

    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    abandon_swap(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )

    from shared.symbol_graph_pointer import read_pointer  # type: ignore
    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A
    assert state.graph_pending_sha is None


# ===========================================================================
# Concurrent-regen scenario — the load-bearing safety case.
# ===========================================================================


def test_concurrent_regen_second_begin_swap_fails(code_maps_table):
    """Two workers race to begin_swap for the same (workspace, repo)
    with DIFFERENT new SHAs. The first wins; the second is rejected by
    the conditional check so it cannot stomp the pending sha."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
        read_pointer,
    )

    # Worker 1 begins for SHA_A.
    assert begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    ) is True

    # Worker 2 begins for SHA_B while Worker 1 is mid-write.
    assert begin_swap(
        WORKSPACE_ID, REPO, SHA_B, code_maps_table_name=CODE_MAPS_TABLE
    ) is False

    # Worker 1 commits cleanly.
    assert commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    ) is True

    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A
    assert state.graph_pending_sha is None


# ===========================================================================
# Failure scenario — begin succeeds, commit fails, abandon resets.
# ===========================================================================


def test_failure_scenario_commit_fails_abandon_resets(code_maps_table):
    """Simulate the Lambda crashing between begin and commit (mid-write
    DDB throttle, OOM, etc.). The pointer stays consistent: graph_sha
    unchanged, and abandon_swap clears the pending sha so the next
    regen can claim the pointer."""
    from shared.symbol_graph_pointer import (  # type: ignore
        abandon_swap,
        begin_swap,
        commit_swap,
        read_pointer,
    )

    # First successful regen sets graph_sha = SHA_A.
    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )

    # Second regen claims SHA_B but the simulated commit attempt is
    # rejected by patching commit_swap to behave as if the underlying
    # conditional failed mid-flight (caller would see ok=False).
    begin_swap(
        WORKSPACE_ID, REPO, SHA_B, code_maps_table_name=CODE_MAPS_TABLE
    )

    from shared import symbol_graph_pointer  # type: ignore

    with patch.object(
        symbol_graph_pointer, "commit_swap", return_value=False
    ):
        ok = symbol_graph_pointer.commit_swap(
            WORKSPACE_ID, REPO, SHA_B,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert ok is False

    # graph_sha must still be SHA_A — the previous-slice readers must
    # not see a half-flipped pointer.
    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A
    assert state.graph_pending_sha == SHA_B

    # Recovery: abandon resets pending; next regen can proceed.
    abandon_swap(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    state = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert state.graph_sha == SHA_A
    assert state.graph_pending_sha is None

    # New regen for SHA_C succeeds end-to-end.
    assert begin_swap(
        WORKSPACE_ID, REPO, SHA_C, code_maps_table_name=CODE_MAPS_TABLE
    ) is True
    assert commit_swap(
        WORKSPACE_ID, REPO, SHA_C, code_maps_table_name=CODE_MAPS_TABLE
    ) is True
    final = read_pointer(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert final.graph_sha == SHA_C
    assert final.graph_pending_sha is None


# ===========================================================================
# Env-var fallback + negative cases.
# ===========================================================================


def test_table_name_from_env(code_maps_table, monkeypatch):
    """CODE_MAPS_TABLE env var is honored when the caller omits the kwarg."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        read_pointer,
    )

    monkeypatch.setenv("CODE_MAPS_TABLE", CODE_MAPS_TABLE)

    assert begin_swap(WORKSPACE_ID, REPO, SHA_A) is True
    state = read_pointer(WORKSPACE_ID, REPO)
    assert state.graph_pending_sha == SHA_A


def test_get_previous_graph_sha_returns_committed_value(code_maps_table):
    """get_previous_graph_sha is the writer-facing accessor for the
    delete-then-write pass — must surface the COMMITTED graph_sha
    only, not a stale or pending value."""
    from shared.symbol_graph_pointer import (  # type: ignore
        begin_swap,
        commit_swap,
        get_previous_graph_sha,
    )

    assert get_previous_graph_sha(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    ) is None

    begin_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    # Pending only — previous committed is still None.
    assert get_previous_graph_sha(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    ) is None

    commit_swap(
        WORKSPACE_ID, REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE
    )
    assert get_previous_graph_sha(
        WORKSPACE_ID, REPO, code_maps_table_name=CODE_MAPS_TABLE
    ) == SHA_A


def test_begin_swap_rejects_empty_args(code_maps_table):
    from shared.symbol_graph_pointer import begin_swap  # type: ignore

    with pytest.raises(ValueError):
        begin_swap("", REPO, SHA_A, code_maps_table_name=CODE_MAPS_TABLE)
    with pytest.raises(ValueError):
        begin_swap(WORKSPACE_ID, "", SHA_A, code_maps_table_name=CODE_MAPS_TABLE)
    with pytest.raises(ValueError):
        begin_swap(WORKSPACE_ID, REPO, "", code_maps_table_name=CODE_MAPS_TABLE)


def test_missing_table_name_raises():
    from shared.symbol_graph_pointer import read_pointer  # type: ignore

    with pytest.raises(ValueError, match="code_maps_table_name"):
        read_pointer(WORKSPACE_ID, REPO)
