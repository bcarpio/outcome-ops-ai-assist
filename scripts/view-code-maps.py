#!/usr/bin/env python3
"""
View code maps stored in S3 Vectors.

Usage:
  ./scripts/view-code-maps.py                              # List all code maps
  ./scripts/view-code-maps.py <repo>                       # List code units for a repo
  ./scripts/view-code-maps.py <repo> <code-unit>           # View specific code unit content

Examples:
  ./scripts/view-code-maps.py
  ./scripts/view-code-maps.py outcome-ops-ai-assist
  ./scripts/view-code-maps.py outcome-ops-ai-assist ingest-docs
  ./scripts/view-code-maps.py my-repo generate-code-maps

Environment:
  ENVIRONMENT - dev or prd (default: prd)
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prd")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


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


def list_all_code_maps(config: dict) -> None:
    """List all code maps grouped by repository."""
    s3vectors = boto3.client("s3vectors")

    print(f"Listing code maps from S3 Vectors...")
    print(f"Bucket: {config['bucket']}, Index: {config['index']}\n")

    repos = defaultdict(list)
    next_token = None

    try:
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
                key = vector.get("key", "")
                # Key format: {repo}#{type}#{name}
                parts = key.split("#")
                if len(parts) >= 2:
                    repo = parts[0]
                    repos[repo].append(key)

            next_token = response.get("nextToken")
            if not next_token:
                break

    except ClientError as e:
        print(f"Error listing vectors: {e}", file=sys.stderr)
        sys.exit(1)

    if not repos:
        print("No code maps found in S3 Vectors.")
        return

    print(f"Repositories with code maps ({len(repos)}):\n")
    for repo in sorted(repos.keys()):
        count = len(repos[repo])
        print(f"  {repo:<45} ({count} code units)")

    print(f"\nUse: ./scripts/view-code-maps.py <repo> to list code units")


def list_repo_code_units(config: dict, repo: str) -> None:
    """List all code units for a specific repository."""
    s3vectors = boto3.client("s3vectors")

    print(f"Code units for repository: {repo}\n")

    code_units = []
    next_token = None

    try:
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
                key = vector.get("key", "")
                # Key format: {repo}#{type}#{name}
                if key.startswith(f"{repo}#"):
                    code_units.append(key)

            next_token = response.get("nextToken")
            if not next_token:
                break

    except ClientError as e:
        print(f"Error listing vectors: {e}", file=sys.stderr)
        sys.exit(1)

    if not code_units:
        print(f"No code units found for repository: {repo}")
        return

    print(f"{'Code Unit':<50} {'Type'}")
    print("-" * 70)

    for key in sorted(code_units):
        # Parse key: {repo}#{type}#{name}
        parts = key.split("#")
        if len(parts) >= 3:
            unit_type = parts[1]
            unit_name = parts[2]
        elif len(parts) == 2:
            unit_type = parts[1]
            unit_name = "(root)"
        else:
            unit_type = "unknown"
            unit_name = key

        print(f"  {unit_name:<48} {unit_type}")

    print(f"\nTotal: {len(code_units)} code units")
    print(f"\nUse: ./scripts/view-code-maps.py {repo} <code-unit> to view content")


def view_code_unit(config: dict, repo: str, code_unit: str) -> None:
    """View content of a specific code unit."""
    s3vectors = boto3.client("s3vectors")

    # Try different key patterns
    # Pattern: {repo}#handler-group-summary#{name}
    # Pattern: {repo}#infrastructure-summary#{name}
    # Pattern: {repo}#source-module-summary#{name}
    possible_keys = [
        f"{repo}#handler-group-summary#{code_unit}",
        f"{repo}#infrastructure-summary#{code_unit}",
        f"{repo}#source-module-summary#{code_unit}",
        f"{repo}#api-routes-summary#{code_unit}",
        f"{repo}#frontend-summary#{code_unit}",
        f"{repo}#schemas-summary#{code_unit}",
        f"{repo}#shared-summary#{code_unit}",
        f"{repo}#tests-summary#{code_unit}",
        f"{repo}#build-summary#{code_unit}",
        f"{repo}#config-summary#{code_unit}",
        f"{repo}#docs-summary#{code_unit}",
        f"{repo}#architectural-summary#METADATA",
    ]

    vector_data = None
    matched_key = None

    for key in possible_keys:
        try:
            response = s3vectors.get_vectors(
                vectorBucketName=config["bucket"],
                indexName=config["index"],
                keys=[key],
                returnMetadata=True,
            )

            vectors = response.get("vectors", [])
            if vectors:
                vector_data = vectors[0]
                matched_key = key
                break

        except ClientError:
            continue

    if not vector_data:
        print(f"Code unit '{code_unit}' not found for repository '{repo}'.", file=sys.stderr)
        print(f"\nTried these keys:")
        for key in possible_keys[:5]:
            print(f"  - {key}")
        print(f"  ... and {len(possible_keys) - 5} more")
        print(f"\nUse: ./scripts/view-code-maps.py {repo} to list available code units")
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
        view_code_unit(config, args.repo, args.code_unit)
    else:
        list_repo_code_units(config, args.repo)


if __name__ == "__main__":
    main()
