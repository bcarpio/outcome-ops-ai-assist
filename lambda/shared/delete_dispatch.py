"""Shared repo-deletion dispatch.

A repo's data spans three shapes (KB docs/vectors under the
owner/slug prefix, bare-name KB objects, and symbol-graph facts +
code-maps pointer in DynamoDB), and under HA it lives in BOTH regions.
Repo removal must queue cleanup of all of them in every region.

This module is the single source of truth for *what* a repo-delete
queues, so every removal entry point
(``workspace-management.remove_repo`` and each code-host integration's
``_queue_delete_job`` / ``queue_delete_job``) cannot drift again — they
previously each queued a single owner-prefix delete and missed the
bare-name keys + the symbol graph (delete-bug 01/03), and only the
primary region (delete-bug 02).

It is provider-agnostic (ADR-052): GitHub, GitLab, and Azure DevOps all
route through here with their own ``source``. ``repo_name`` is the
provider's full slug — ``owner/repo`` (GitHub), ``group[/subgroup...]/
repo`` (GitLab), ``org/project/repo`` (Azure DevOps) — and the BARE
name is always the LAST path segment, matching how the
architectural-summary, the code-map/summary vectors, and the
symbol-graph rows are all keyed. Before this, GitLab and ADO each
hand-rolled a single slug ``delete_prefix`` and leaked the bare-name
summary, the code-map vectors, and the symbol graph (and ADO doubled
the ``azure_devops/`` segment so its delete cleaned nothing).

The delete-worker exists in both regions consuming its own delete queue;
sending the same messages to each region's queue lets each worker clean
its own stores uniformly (no cross-region resource access needed).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def build_repo_delete_messages(
    workspace_id: str, repo_name: str, source: str = "github"
) -> List[Dict[str, Any]]:
    """The complete set of delete-worker messages for removing one repo.

    ``repo_name`` is the provider's full slug (``owner/repo`` for GitHub,
    ``group[/subgroup...]/repo`` for GitLab, ``org/project/repo`` for
    Azure DevOps). ``source`` is the KB source segment
    (``github`` / ``gitlab`` / ``azure_devops``). The BARE name is the
    LAST path segment. Returns:
    - ``delete_prefix`` for the full-slug KB prefix (source mirror +
      per-doc chunk vectors)
    - ``delete_prefix`` for the bare-name KB prefix (the
      architectural-summary, written under {source}/{repo}/ with no
      owner/group; and the bare-keyed code-map/summary vectors) —
      omitted when the slug has no owner/group segment
    - ``delete_symbol_graph`` for the DynamoDB facts + code-maps pointer
      (keyed by the BARE repo name)
    """
    repo_bare = repo_name.rsplit("/", 1)[-1]
    messages: List[Dict[str, Any]] = [
        {
            "action": "delete_prefix",
            "workspace_id": workspace_id,
            "source": source,
            "prefix": repo_name + "/",
        }
    ]
    if repo_bare != repo_name:
        messages.append(
            {
                "action": "delete_prefix",
                "workspace_id": workspace_id,
                "source": source,
                "prefix": repo_bare + "/",
            }
        )
    messages.append(
        {
            "action": "delete_symbol_graph",
            "workspace_id": workspace_id,
            "repo_bare": repo_bare,
            "repo": repo_name,
        }
    )
    return messages


def dispatch_repo_delete(
    workspace_id: str,
    repo_name: str,
    targets: List[Tuple[Any, str]],
    source: str = "github",
) -> int:
    """Send every repo-delete message to every target queue.

    ``targets`` is a list of ``(sqs_client, queue_url)`` — typically the
    primary queue and, under HA, the secondary-region queue. Each
    region's delete-worker then cleans its own region's stores. Targets
    with an empty ``queue_url`` are skipped (single-region mode).
    ``source`` selects the code host (``github`` / ``gitlab`` /
    ``azure_devops``). Returns the number of messages sent.
    """
    messages = build_repo_delete_messages(workspace_id, repo_name, source)
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
