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
    # Owner-prefixed name carried separately so the worker can clean
    # pending-work rows whose PK uses owner/repo (delete-cascade gap fix).
    assert sg["repo"] == "owner/repo"
    assert all(m["workspace_id"] == "ws1" for m in msgs)


def test_build_messages_no_owner_skips_bare_duplicate():
    msgs = build_repo_delete_messages("ws1", "repo")  # no owner segment
    # Only one delete_prefix (owner==bare) + symbol graph.
    assert [m["action"] for m in msgs] == ["delete_prefix", "delete_symbol_graph"]
    assert msgs[0]["prefix"] == "repo/"
    assert msgs[1]["repo_bare"] == "repo"
    assert msgs[1]["repo"] == "repo"


def test_build_messages_defaults_to_github_source():
    msgs = build_repo_delete_messages("ws1", "owner/repo")
    assert all(m["source"] == "github" for m in msgs if m["action"] == "delete_prefix")


def test_build_messages_gitlab_source():
    # ADR-052: GitLab routes through the same helper with its own source.
    msgs = build_repo_delete_messages("ws1", "group/project", source="gitlab")
    prefix_msgs = [m for m in msgs if m["action"] == "delete_prefix"]
    assert all(m["source"] == "gitlab" for m in prefix_msgs)
    assert {m["prefix"] for m in prefix_msgs} == {"group/project/", "project/"}
    sg = [m for m in msgs if m["action"] == "delete_symbol_graph"][0]
    assert sg["repo_bare"] == "project"
    assert sg["repo"] == "group/project"


def test_build_messages_gitlab_nested_subgroup_bare_is_last_segment():
    # Nested subgroups: bare name must collapse to the LAST segment so the
    # bare-name summary / code-map / symbol-graph cleanup targets the same
    # key the ingest pipeline writes (delete-bug: GitLab).
    msgs = build_repo_delete_messages(
        "ws1", "group/subgroup/project", source="gitlab"
    )
    prefix_msgs = [m for m in msgs if m["action"] == "delete_prefix"]
    assert {m["prefix"] for m in prefix_msgs} == {
        "group/subgroup/project/",  # full slug (source mirror)
        "project/",                  # bare (architectural-summary + code-map)
    }
    sg = [m for m in msgs if m["action"] == "delete_symbol_graph"][0]
    assert sg["repo_bare"] == "project"


def test_build_messages_azure_devops_org_project_repo():
    # ADO slug is org/project/repo; bare is the final repo segment.
    msgs = build_repo_delete_messages(
        "ws1", "org/project/repo", source="azure_devops"
    )
    prefix_msgs = [m for m in msgs if m["action"] == "delete_prefix"]
    assert all(m["source"] == "azure_devops" for m in prefix_msgs)
    assert {m["prefix"] for m in prefix_msgs} == {"org/project/repo/", "repo/"}
    assert [m for m in msgs if m["action"] == "delete_symbol_graph"][0][
        "repo_bare"
    ] == "repo"


def test_dispatch_passes_source_through():
    client = MagicMock()
    dispatch_repo_delete(
        "ws1", "group/project", [(client, "https://q/primary")], source="gitlab"
    )
    bodies = [json.loads(c.kwargs["MessageBody"]) for c in client.send_message.call_args_list]
    assert all(
        b["source"] == "gitlab" for b in bodies if b["action"] == "delete_prefix"
    )


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
