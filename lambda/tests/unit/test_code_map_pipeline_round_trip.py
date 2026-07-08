"""Round-trip integration test for the code-map batch splitting migration
(chunks 01-06).

Unit tests in test_process_repo_summary_coverage.py and
test_process_batch_summary.py cover each side of the message-shape
contract in isolation. This test connects them: it invokes the actual
splitter to emit SQS messages, then feeds those exact message bodies
into the consumer's process_batch_record. Any drift in the message
shape between the two Lambdas breaks this test.

The moto-based end-to-end test the plan originally called for isn't
written here because moto doesn't have s3vectors support; the surface
that matters (message-shape contract) is exercised through the direct
handler invocation below.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Producer-side handler (process-repo-summary)
# ============================================================================

_producer_dir = os.path.join(
    os.path.dirname(__file__), "../../process-repo-summary"
)
if _producer_dir not in sys.path:
    sys.path.insert(0, _producer_dir)
_lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _lambda_root not in sys.path:
    sys.path.insert(0, _lambda_root)

import handler as rsh  # noqa: E402  process-repo-summary/handler.py


# ============================================================================
# Consumer-side handler (process-batch-summary)
# ============================================================================

_consumer_dir = os.path.join(
    os.path.dirname(__file__), "../../process-batch-summary"
)
_consumer_path = os.path.join(_consumer_dir, "handler.py")
_spec = importlib.util.spec_from_file_location(
    "process_batch_summary_handler", _consumer_path,
)
_bsm = importlib.util.module_from_spec(_spec)
sys.modules["process_batch_summary_handler"] = _bsm
_spec.loader.exec_module(_bsm)


# ============================================================================
# Round-trip test
# ============================================================================


class TestSplitterOutputFlowsIntoConsumer:
    """Emit real SQS messages via the splitter, then invoke the consumer
    on each. Failures here mean the interface between the two Lambdas
    has drifted."""

    def _oversized_code_unit(self):
        # 15 files x 40k bytes = ~200k tokens > TARGET_SHARD_TOKENS (120k)
        # Guarantees the splitter shards this unit.
        return {
            "batch_type": "handler-group",
            "group_name": "tests",
            "storage_key": "summary#tests",
            "file_paths": [f"lambda/tests/unit/test_x{i}.py" for i in range(15)],
        }

    def _all_files_dict_shape(self, code_unit):
        return [
            {"path": p, "type": "blob", "size": 40_000}
            for p in code_unit["file_paths"]
        ]

    def _small_code_unit(self):
        return {
            "batch_type": "handler-group",
            "group_name": "ingest-docs",
            "storage_key": "summary#ingest-docs",
            "file_paths": ["lambda/ingest-docs/handler.py"],
        }

    @patch.object(rsh, "sqs_client")
    @patch.object(rsh, "CODE_MAPS_QUEUE_URL", "https://sqs.example.com/queue")
    def test_splitter_sqs_messages_parse_cleanly_on_consumer_side(
        self, mock_sqs,
    ):
        """The splitter emits N shard messages + 1 merge message. Each
        message body must have the fields the consumer's
        process_batch_record reads: message_type, batch_type,
        group_name, file_paths, storage_key, parent_storage_key,
        shard_index, shard_count. Consumer's chunk 02 defaults kick in
        for any absent field."""
        code_unit = self._oversized_code_unit()
        rsh.send_code_units_to_sqs(
            [code_unit],
            "repo",
            "owner/repo",
            workspace_id="ws-1",
            commit_sha="abc",
            all_files=self._all_files_dict_shape(code_unit),
        )
        bodies = [
            json.loads(call.kwargs["MessageBody"])
            for call in mock_sqs.send_message.call_args_list
        ]
        shard_bodies = [b for b in bodies if b["message_type"] == "shard"]
        merge_bodies = [b for b in bodies if b["message_type"] == "merge"]
        assert len(shard_bodies) >= 2
        assert len(merge_bodies) == 1

        # For every shard message, the consumer's read path must not KeyError.
        # We patch the heavy downstream work (fetch/summarize/store) so the
        # test doesn't need Bedrock or S3 Vectors; the goal is verifying the
        # parse layer.
        with patch.object(_bsm, "generate_batch_summary", return_value="summary"), \
             patch.object(_bsm, "generate_embedding", return_value=[0.0] * 1024), \
             patch.object(_bsm, "store_batch_summary", return_value=True), \
             patch.object(_bsm, "_mark_code_map_unit_done"):
            for body in shard_bodies:
                record = {"body": json.dumps(body)}
                _bsm.process_batch_record(record)

    @patch.object(rsh, "sqs_client")
    @patch.object(rsh, "CODE_MAPS_QUEUE_URL", "https://sqs.example.com/queue")
    def test_splitter_merge_message_routes_to_handle_merge_in_consumer(
        self, mock_sqs,
    ):
        """Round-trip: splitter emits a merge message, consumer's dispatcher
        routes it to handle_merge_message rather than the shard summarize
        path."""
        code_unit = self._oversized_code_unit()
        rsh.send_code_units_to_sqs(
            [code_unit],
            "repo",
            "owner/repo",
            workspace_id="ws-1",
            commit_sha="abc",
            all_files=self._all_files_dict_shape(code_unit),
        )
        merge_body = next(
            json.loads(call.kwargs["MessageBody"])
            for call in mock_sqs.send_message.call_args_list
            if json.loads(call.kwargs["MessageBody"])["message_type"] == "merge"
        )
        with patch.object(_bsm, "handle_merge_message") as mock_merge, \
             patch.object(_bsm, "generate_batch_summary") as mock_summary:
            _bsm.process_batch_record({"body": json.dumps(merge_body)})
        mock_merge.assert_called_once()
        mock_summary.assert_not_called()

    @patch.object(rsh, "sqs_client")
    @patch.object(rsh, "CODE_MAPS_QUEUE_URL", "https://sqs.example.com/queue")
    def test_single_shard_unit_flows_through_consumer_unchanged(
        self, mock_sqs,
    ):
        """A small unit (no sharding) emits ONE shard message with
        shard_count=1, storage_key == parent_storage_key. Consumer
        processes it via the summarize path (not merge)."""
        code_unit = self._small_code_unit()
        rsh.send_code_units_to_sqs(
            [code_unit],
            "repo",
            "owner/repo",
            workspace_id="ws-1",
            commit_sha="abc",
            all_files=[{"path": code_unit["file_paths"][0], "type": "blob", "size": 5000}],
        )
        assert mock_sqs.send_message.call_count == 1
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["shard_count"] == 1
        assert body["storage_key"] == body["parent_storage_key"]

        with patch.object(_bsm, "generate_batch_summary", return_value="summary"), \
             patch.object(_bsm, "generate_embedding", return_value=[0.0] * 1024), \
             patch.object(_bsm, "store_batch_summary", return_value=True), \
             patch.object(_bsm, "_mark_code_map_unit_done"):
            _bsm.process_batch_record({"body": json.dumps(body)})

    @patch.object(rsh, "sqs_client")
    @patch.object(rsh, "CODE_MAPS_QUEUE_URL", "https://sqs.example.com/queue")
    def test_ledger_expected_count_matches_actual_sqs_emissions(
        self, mock_sqs,
    ):
        """count_expected_shard_messages must return the same number as
        send_code_units_to_sqs actually emits. Chunk 03's follow-up
        fixed a bug where these two diverged for sharded units, causing
        try_advance_sha to misfire."""
        code_units = [
            self._oversized_code_unit(),
            self._small_code_unit(),
        ]
        all_files = self._all_files_dict_shape(code_units[0]) + [
            {"path": code_units[1]["file_paths"][0], "type": "blob", "size": 5000}
        ]

        # Predicted count from the ledger-opener
        predicted = rsh.count_expected_shard_messages(code_units, all_files)

        # Actual messages emitted by the splitter
        rsh.send_code_units_to_sqs(
            code_units, "repo", "owner/repo",
            workspace_id="ws-1", commit_sha="abc",
            all_files=all_files,
        )
        actual = mock_sqs.send_message.call_count
        assert predicted == actual, (
            f"Ledger accounting drift: expected_count={predicted} but "
            f"splitter emitted {actual} messages. Chunk 03's follow-up "
            f"exists to keep these in sync."
        )
