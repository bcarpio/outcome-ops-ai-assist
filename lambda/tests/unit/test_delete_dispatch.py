"""Unit tests for shared.delete_dispatch (delete-bug 01/02/03)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

from shared.delete_dispatch import (  # noqa: E402
    build_repo_delete_messages,
    dispatch_repo_delete,
    region_from_sqs_url,
)


def test_build_messages_owner_prefixed():
    msgs = build_repo_delete_messages("ws1", "owner/repo")
    actions = [m["action"] for m in msgs]
    assert actions == ["delete_prefix", "delete_prefix", "delete_symbol_graph"]
    prefixes = {m["prefix"] for m in msgs if m["action"] == "delete_prefix"}
    assert prefixes == {"owner/repo/", "repo/"}  # owner + bare
    sg = [m for m in msgs if m["action"] == "delete_symbol_graph"][0]
    assert sg["repo_bare"] == "repo"
    assert all(m["workspace_id"] == "ws1" for m in msgs)


def test_build_messages_no_owner_skips_bare_duplicate():
    msgs = build_repo_delete_messages("ws1", "repo")  # no owner segment
    # Only one delete_prefix (owner==bare) + symbol graph.
    assert [m["action"] for m in msgs] == ["delete_prefix", "delete_symbol_graph"]
    assert msgs[0]["prefix"] == "repo/"
    assert msgs[1]["repo_bare"] == "repo"


def test_dispatch_single_region():
    client = MagicMock()
    sent = dispatch_repo_delete("ws1", "owner/repo", [(client, "https://q/primary")])
    assert sent == 3
    assert client.send_message.call_count == 3
    # Every message went to the primary URL.
    for c in client.send_message.call_args_list:
        assert c.kwargs["QueueUrl"] == "https://q/primary"


def test_dispatch_dual_region_sends_to_both():
    primary, secondary = MagicMock(), MagicMock()
    sent = dispatch_repo_delete(
        "ws1", "owner/repo",
        [(primary, "https://q/primary"), (secondary, "https://q/secondary")],
    )
    assert sent == 6  # 3 messages x 2 regions
    assert primary.send_message.call_count == 3
    assert secondary.send_message.call_count == 3
    # Both regions got an identical delete_symbol_graph.
    sec_bodies = [json.loads(c.kwargs["MessageBody"]) for c in secondary.send_message.call_args_list]
    assert any(b["action"] == "delete_symbol_graph" and b["repo_bare"] == "repo" for b in sec_bodies)


def test_dispatch_skips_empty_url():
    primary, secondary = MagicMock(), MagicMock()
    sent = dispatch_repo_delete(
        "ws1", "owner/repo", [(primary, "https://q/primary"), (secondary, "")]
    )
    assert sent == 3
    secondary.send_message.assert_not_called()


def test_region_from_sqs_url():
    assert region_from_sqs_url(
        "https://sqs.us-east-2.amazonaws.com/123/q"
    ) == "us-east-2"
    assert region_from_sqs_url("") == ""
    assert region_from_sqs_url("not-a-url") == ""
