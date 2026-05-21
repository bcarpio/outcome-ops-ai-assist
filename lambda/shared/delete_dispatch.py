"""Shared repo-deletion dispatch.

A repo's data spans three shapes (KB docs/vectors under the
owner-prefix, legacy bare-name KB objects, and symbol-graph facts +
code-maps pointer in DynamoDB), and under HA it lives in BOTH regions.
Repo removal must queue cleanup of all of them in every region.

This module is the single source of truth for *what* a repo-delete
queues, so the two removal entry points
(``workspace-management.remove_repo`` and
``github-integration.queue_delete_job``) cannot drift again — they
previously each queued a single owner-prefix delete and missed the
bare-name keys + the symbol graph (delete-bug 01/03), and only the
primary region (delete-bug 02).

The delete-worker exists in both regions consuming its own delete queue;
sending the same messages to each region's queue lets each worker clean
its own stores uniformly (no cross-region resource access needed).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def build_repo_delete_messages(workspace_id: str, repo_name: str) -> List[Dict[str, Any]]:
    """The complete set of delete-worker messages for removing one repo.

    ``repo_name`` is owner-prefixed ("owner/repo"). Returns:
    - ``delete_prefix`` for the owner-prefixed KB prefix
    - ``delete_prefix`` for the legacy bare-name KB prefix (e.g. the
      architectural-summary, written under github/{repo}/ with no owner)
      — omitted when there is no owner segment
    - ``delete_symbol_graph`` for the DynamoDB facts + code-maps pointer
      (keyed by the BARE repo name)
    """
    repo_bare = repo_name.split("/", 1)[1] if "/" in repo_name else repo_name
    messages: List[Dict[str, Any]] = [
        {
            "action": "delete_prefix",
            "workspace_id": workspace_id,
            "source": "github",
            "prefix": repo_name + "/",
        }
    ]
    if repo_bare != repo_name:
        messages.append(
            {
                "action": "delete_prefix",
                "workspace_id": workspace_id,
                "source": "github",
                "prefix": repo_bare + "/",
            }
        )
    messages.append(
        {
            "action": "delete_symbol_graph",
            "workspace_id": workspace_id,
            "repo_bare": repo_bare,
        }
    )
    return messages


def dispatch_repo_delete(
    workspace_id: str,
    repo_name: str,
    targets: List[Tuple[Any, str]],
) -> int:
    """Send every repo-delete message to every target queue.

    ``targets`` is a list of ``(sqs_client, queue_url)`` — typically the
    primary queue and, under HA, the secondary-region queue. Each
    region's delete-worker then cleans its own region's stores. Targets
    with an empty ``queue_url`` are skipped (single-region mode). Returns
    the number of messages sent.
    """
    messages = build_repo_delete_messages(workspace_id, repo_name)
    sent = 0
    for client, queue_url in targets:
        if not queue_url:
            continue
        for message in messages:
            client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
            sent += 1
    return sent


def region_from_sqs_url(queue_url: str) -> str:
    """Best-effort region from an SQS URL (https://sqs.{region}.amazonaws.com/...).

    Lets a producer build a cross-region SQS client for the secondary
    queue without a separate region env var. Empty string when it can't
    be parsed (caller should then skip the secondary send).
    """
    if not queue_url:
        return ""
    try:
        host = queue_url.split("//", 1)[1].split("/", 1)[0]
        parts = host.split(".")
        if len(parts) >= 2 and parts[0] == "sqs":
            return parts[1]
    except (IndexError, ValueError):
        pass
    return ""
