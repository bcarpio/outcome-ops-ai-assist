#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Delete workspaces owned by anonymous or a specific user.

Calls the workspace management Lambda directly with SigV4 signing.
Lists all workspaces, filters by created_by, and deletes matching ones.

Usage:
    # Dry run - show what would be deleted
    python scripts/cleanup-workspaces.py --dry-run

    # Delete all anonymous and scanner-owned workspaces
    python scripts/cleanup-workspaces.py

    # Delete only workspaces owned by a specific user
    python scripts/cleanup-workspaces.py --owner "security-scanner@outcomeops.local"
"""

import argparse
import datetime
import hashlib
import hmac
import json
import subprocess
import sys
import urllib.request


# ============================================================================
# SigV4 Signing (copied from create-pentest.py)
# ============================================================================


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret_key, date_stamp, region, service):
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _get_aws_credentials():
    """Get AWS credentials from environment or CLI."""
    import os

    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session_token = os.environ.get("AWS_SESSION_TOKEN")

    if access_key and secret_key:
        return access_key, secret_key, session_token

    # Fall back to boto3
    try:
        import boto3
        session = boto3.Session()
        creds = session.get_credentials().get_frozen_credentials()
        return creds.access_key, creds.secret_key, creds.token
    except Exception:
        print("ERROR: No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or configure AWS CLI.")
        sys.exit(1)


def sigv4_request(url, method, body=None, user_email="admin@example.com"):
    """Make a SigV4-signed request to a Lambda function URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname
    # Extract region from Lambda function URL hostname (e.g., xxx.lambda-url.us-west-2.on.aws)
    parts = host.split(".")
    try:
        region = parts[parts.index("lambda-url") + 1]
    except (ValueError, IndexError):
        region = "us-east-1"
    service = "lambda"

    access_key, secret_key, session_token = _get_aws_credentials()

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload = body or ""
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    headers_to_sign = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "x-user-email": user_email,
    }
    if body:
        headers_to_sign["content-type"] = "application/json"
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token

    signed_header_keys = sorted(headers_to_sign.keys())
    signed_headers = ";".join(signed_header_keys)
    canonical_headers = "".join(
        f"{k}:{headers_to_sign[k]}\n" for k in signed_header_keys
    )

    path = parsed.path or "/"
    canonical_request = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

    signing_key = _get_signature_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"

    request_headers = {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "x-user-email": user_email,
    }
    if body:
        request_headers["Content-Type"] = "application/json"
    if session_token:
        request_headers["x-amz-security-token"] = session_token

    req = urllib.request.Request(
        url,
        data=payload.encode("utf-8") if body else None,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, {"error": body_text}


def get_lambda_url():
    """Get workspace management Lambda URL from Terraform output or SSM."""
    try:
        result = subprocess.run(
            ["terraform", "output", "-raw", "workspace_management_url"],
            capture_output=True, text=True, cwd="terraform"
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass

    # Fall back to SSM
    try:
        import boto3
        ssm = boto3.client("ssm", region_name="us-east-1")
        resp = ssm.get_parameter(Name="/prd/outcome-ops-ai-assist/api/workspace-management-url")
        return resp["Parameter"]["Value"]
    except Exception:
        pass

    print("ERROR: Could not determine workspace management Lambda URL.")
    print("Provide it with --lambda-url")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Clean up junk workspaces")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--owner", action="append", help="Delete workspaces owned by this user (repeatable). Default: anonymous + security-scanner@outcomeops.local")
    parser.add_argument("--lambda-url", help="Workspace management Lambda function URL")
    parser.add_argument("--user-email", default="admin@example.com", help="User email for auth")
    args = parser.parse_args()

    owners_to_delete = set(args.owner) if args.owner else {"anonymous", "security-scanner@outcomeops.local"}
    lambda_url = args.lambda_url or get_lambda_url()
    base_url = lambda_url.rstrip("/")

    print(f"Lambda URL: {base_url}")
    print(f"Owners to delete: {', '.join(owners_to_delete)}")
    print()

    # List all workspaces
    print("Fetching workspaces...")
    status, data = sigv4_request(f"{base_url}/workspaces", "GET", user_email=args.user_email)
    if status != 200:
        print(f"ERROR: Failed to list workspaces: {status} {data}")
        sys.exit(1)

    workspaces = data.get("workspaces", [])
    print(f"Found {len(workspaces)} total workspaces")

    # Filter to target workspaces
    to_delete = [
        ws for ws in workspaces
        if ws.get("created_by", "").lower() in {o.lower() for o in owners_to_delete}
    ]

    if not to_delete:
        print("No matching workspaces to delete.")
        return

    print(f"\nWorkspaces to delete ({len(to_delete)}):")
    print("-" * 80)
    for ws in to_delete:
        print(f"  {ws.get('workspace_id', 'N/A'):30s}  owner={ws.get('created_by', 'N/A'):40s}  name={ws.get('name', 'N/A')}")
    print("-" * 80)

    if args.dry_run:
        print("\n[DRY RUN] No workspaces were deleted.")
        return

    print(f"\nDeleting {len(to_delete)} workspaces...")
    deleted = 0
    failed = 0
    for ws in to_delete:
        ws_id = ws.get("workspace_id")
        status, resp = sigv4_request(f"{base_url}/workspaces/{ws_id}", "DELETE", user_email=args.user_email)
        if status == 200:
            deleted += 1
            print(f"  Deleted: {ws_id} ({ws.get('name', 'N/A')})")
        else:
            failed += 1
            print(f"  FAILED:  {ws_id} - {status} {resp}")

    print(f"\nDone. Deleted: {deleted}, Failed: {failed}")


if __name__ == "__main__":
    main()
