#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
View code maps stored in S3 Vectors.

Usage:
  ./scripts/view-code-maps.py                                    # List all repos/workspaces
  ./scripts/view-code-maps.py -w <workspace_id> <repo>           # List code units for a workspace repo
  ./scripts/view-code-maps.py -w <workspace_id> <repo> <unit>    # View specific code unit
  ./scripts/view-code-maps.py <repo>                             # List code units (legacy, no workspace)
  ./scripts/view-code-maps.py <repo> <code-unit>                 # View code unit (legacy)

Key formats:
  Workspace repos: {workspace_id}#github#{repo}#{type}#{name}
  Legacy repos:    {repo}#{type}#{name}

Examples:
  ./scripts/view-code-maps.py
  ./scripts/view-code-maps.py -w m2hMZPfwpFg_tF1J-9XnxQ spring-petclinic-private
  ./scripts/view-code-maps.py -w m2hMZPfwpFg_tF1J-9XnxQ spring-petclinic-private handler-group-summary
  ./scripts/view-code-maps.py outcome-ops-ai-assist ingest-docs

Environment:
  ENVIRONMENT - dev or prd (default: prd)
"""
import argparse
import os
import sys
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prd")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Summary types used in code map keys
SUMMARY_TYPES = [
    "handler-group-summary",
    "infrastructure-summary",
    "source-module-summary",
    "api-routes-summary",
    "frontend-summary",
    "schemas-summary",
    "shared-summary",
    "tests-summary",
    "build-summary",
    "config-summary",
    "docs-summary",
    "architectural-summary",
]


def get_s3_vectors_config() -> dict:
    """Get S3 Vectors bucket and index from SSM Parameter Store."""
    ssm = boto3.client("ssm")
    config = {}

    params = [
        (f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/bucket-name", "bucket"),
        (f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/index-name", "index"),
    ]

    for param_name, key in params:
        try:
            response = ssm.get_parameter(Name=param_name)
            config[key] = response["Parameter"]["Value"]
        except ClientError as e:
            print(f"Error: Failed to get {param_name} from SSM: {e}", file=sys.stderr)
            sys.exit(1)

    return config


def _list_all_vectors(s3vectors, config: dict) -> list:
    """Paginate through all vectors and return their keys."""
    keys = []
    next_token = None

    while True:
        params = {
            "vectorBucketName": config["bucket"],
            "indexName": config["index"],
            "maxResults": 100,
        }
        if next_token:
            params["nextToken"] = next_token

        response = s3vectors.list_vectors(**params)

        for vector in response.get("vectors", []):
            keys.append(vector.get("key", ""))

        next_token = response.get("nextToken")
        if not next_token:
            break

    return keys


def _parse_key(key: str) -> dict:
    """Parse a vector key into its components.

    Workspace keys: {workspace_id}#github#{repo}#{type}#{name}
    Legacy keys:    {repo}#{type}#{name}
    """
    parts = key.split("#")

    # Workspace key: at least 5 parts with 'github' as second
    if len(parts) >= 5 and parts[1] == "github":
        return {
            "workspace_id": parts[0],
            "source": parts[1],
            "repo": parts[2],
            "type": parts[3],
            "name": "#".join(parts[4:]),  # name may contain #
            "key": key,
        }

    # Legacy key: {repo}#{type}#{name}
    if len(parts) >= 3:
        return {
            "workspace_id": None,
            "source": None,
            "repo": parts[0],
            "type": parts[1],
            "name": "#".join(parts[2:]),
            "key": key,
        }

    # Minimal key
    if len(parts) == 2:
        return {
            "workspace_id": None,
            "source": None,
            "repo": parts[0],
            "type": parts[1],
            "name": "(root)",
            "key": key,
        }

    return {
        "workspace_id": None,
        "source": None,
        "repo": key,
        "type": "unknown",
        "name": key,
        "key": key,
    }


def list_all_code_maps(config: dict) -> None:
    """List all code maps grouped by workspace/repository."""
    s3vectors = boto3.client("s3vectors")

    print(f"Listing code maps from S3 Vectors...")
    print(f"Bucket: {config['bucket']}, Index: {config['index']}\n")

    try:
        keys = _list_all_vectors(s3vectors, config)
    except ClientError as e:
        print(f"Error listing vectors: {e}", file=sys.stderr)
        sys.exit(1)

    if not keys:
        print("No code maps found in S3 Vectors.")
        return

    # Group by (workspace_id, repo)
    groups = defaultdict(list)
    for key in keys:
        parsed = _parse_key(key)
        group_key = (parsed["workspace_id"], parsed["repo"])
        groups[group_key].append(parsed)

    # Display workspace repos
    ws_groups = {k: v for k, v in groups.items() if k[0] is not None}
    legacy_groups = {k: v for k, v in groups.items() if k[0] is None}

    if ws_groups:
        print(f"Workspace repositories ({len(ws_groups)}):\n")
        print(f"  {'Workspace ID':<30} {'Repository':<35} {'Units'}")
        print(f"  {'-' * 30} {'-' * 35} {'-' * 5}")
        for (ws_id, repo), entries in sorted(ws_groups.items()):
            print(f"  {ws_id:<30} {repo:<35} {len(entries)}")
        print(f"\n  Use: ./scripts/view-code-maps.py -w <workspace_id> <repo>")

    if legacy_groups:
        if ws_groups:
            print()
        print(f"Legacy repositories ({len(legacy_groups)}):\n")
        for (_, repo), entries in sorted(legacy_groups.items()):
            print(f"  {repo:<45} ({len(entries)} code units)")
        print(f"\n  Use: ./scripts/view-code-maps.py <repo>")


def list_repo_code_units(config: dict, repo: str, workspace_id: str = None) -> None:
    """List all code units for a specific repository."""
    s3vectors = boto3.client("s3vectors")

    if workspace_id:
        prefix = f"{workspace_id}#github#{repo}#"
        print(f"Code units for workspace {workspace_id}, repo: {repo}\n")
    else:
        prefix = f"{repo}#"
        print(f"Code units for repository: {repo}\n")

    try:
        keys = _list_all_vectors(s3vectors, config)
    except ClientError as e:
        print(f"Error listing vectors: {e}", file=sys.stderr)
        sys.exit(1)

    matching = [k for k in keys if k.startswith(prefix)]

    # For workspace repos, filter out keys that match a different format
    # (e.g., legacy keys that happen to start with the same repo name)
    if workspace_id:
        matching = [k for k in matching if _parse_key(k)["workspace_id"] == workspace_id]

    if not matching:
        print(f"No code units found.")
        if not workspace_id:
            print(f"\nIf this is a workspace repo, use: ./scripts/view-code-maps.py -w <workspace_id> {repo}")
        return

    print(f"  {'Name':<48} {'Type'}")
    print(f"  {'-' * 48} {'-' * 30}")

    for key in sorted(matching):
        parsed = _parse_key(key)
        print(f"  {parsed['name']:<48} {parsed['type']}")

    print(f"\nTotal: {len(matching)} code units")

    if workspace_id:
        print(f"\nUse: ./scripts/view-code-maps.py -w {workspace_id} {repo} <code-unit>")
    else:
        print(f"\nUse: ./scripts/view-code-maps.py {repo} <code-unit>")


def view_code_unit(config: dict, repo: str, code_unit: str, workspace_id: str = None) -> None:
    """View content of a specific code unit."""
    s3vectors = boto3.client("s3vectors")

    # Find the matching key by listing vectors and matching by parsed name
    if workspace_id:
        prefix = f"{workspace_id}#github#{repo}#"
    else:
        prefix = f"{repo}#"

    try:
        all_keys = _list_all_vectors(s3vectors, config)
    except ClientError as e:
        print(f"Error listing vectors: {e}", file=sys.stderr)
        sys.exit(1)

    matching = [k for k in all_keys if k.startswith(prefix)]

    # Find the key whose parsed name matches the code_unit
    matched_key = None
    for key in matching:
        parsed = _parse_key(key)
        if parsed["name"] == code_unit:
            matched_key = key
            break

    if not matched_key:
        print(f"Code unit '{code_unit}' not found for repo '{repo}'.", file=sys.stderr)
        available = sorted(set(_parse_key(k)["name"] for k in matching))
        if available:
            print(f"\nAvailable code units:")
            for name in available:
                print(f"  - {name}")
        if workspace_id:
            print(f"\nUse: ./scripts/view-code-maps.py -w {workspace_id} {repo} to list all")
        else:
            print(f"\nUse: ./scripts/view-code-maps.py {repo} to list all")
        sys.exit(1)

    # Fetch the full vector with metadata
    try:
        response = s3vectors.get_vectors(
            vectorBucketName=config["bucket"],
            indexName=config["index"],
            keys=[matched_key],
            returnMetadata=True,
        )
        vectors = response.get("vectors", [])
        if not vectors:
            print(f"Error: Key found but vector data empty: {matched_key}", file=sys.stderr)
            sys.exit(1)
        vector_data = vectors[0]
    except ClientError as e:
        print(f"Error fetching vector: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract metadata
    metadata = vector_data.get("metadata", {})
    content = metadata.get("content", "")
    batch_type = metadata.get("batch_type", "")
    group_name = metadata.get("group_name", "")
    file_count = metadata.get("file_count", "0")
    timestamp = metadata.get("timestamp", "")
    doc_type = metadata.get("type", "")

    print(f"Code Unit: {code_unit}")
    print(f"Repository: {repo}")
    if workspace_id:
        print(f"Workspace: {workspace_id}")
    print(f"Type: {doc_type or batch_type}")
    print(f"Group: {group_name}")
    print(f"File Count: {file_count}")
    print(f"Last Updated: {timestamp}")
    print(f"Vector Key: {matched_key}")

    print(f"\n{'=' * 80}")
    print("CONTENT:")
    print("=" * 80)
    print(content if content else "(no content)")


def main():
    parser = argparse.ArgumentParser(
        description="View code maps stored in S3 Vectors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("repo", nargs="?", help="Repository name")
    parser.add_argument("code_unit", nargs="?", help="Code unit name (e.g., ingest-docs)")
    parser.add_argument(
        "-w", "--workspace",
        help="Workspace ID (required for workspace-scoped repos)",
    )
    parser.add_argument(
        "--env",
        default=os.environ.get("ENVIRONMENT", "prd"),
        help="Environment (default: prd or ENVIRONMENT env var)",
    )

    args = parser.parse_args()

    # Override environment if specified
    global ENVIRONMENT
    ENVIRONMENT = args.env

    config = get_s3_vectors_config()

    if not args.repo:
        list_all_code_maps(config)
    elif args.code_unit:
        view_code_unit(config, args.repo, args.code_unit, workspace_id=args.workspace)
    else:
        list_repo_code_units(config, args.repo, workspace_id=args.workspace)


if __name__ == "__main__":
    main()
