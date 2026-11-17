#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""ha-kms-migrate — Re-encrypt OAuth tokens + database credentials
under a new multi-region KMS key.

WHY THIS EXISTS
---------------
The HA story requires that ciphertext written in either region be
decryptable in the other after failover. The encryption keys
(``oauth_tokens``, ``db_credentials``) are therefore declared as
multi-region keys (MRKs) with a replica in the secondary region.

But ``multi_region = true`` is a CREATE-TIME flag — a single-region
KMS key cannot be retrofitted to multi-region. Applying the HA KMS
change against an existing single-region deployment therefore
DESTROYS the old key and creates a new MRK with the same alias.

Anything encrypted under the old key becomes undecryptable the moment
the old key is destroyed. This script makes the migration safe by
re-encrypting all affected ciphertext under a freshly-created MRK
BEFORE the old key is destroyed.

OPERATOR FLOW
-------------
For an existing single-region deployment adopting HA:

  1. DO NOT run ``terraform apply`` yet.

  2. Run this script in ``--plan`` mode to inventory affected rows:

       AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \\
           PRIMARY=us-west-2 \\
           ./scripts/ha-kms-migrate.py --plan

  3. Run with ``--execute`` to:
       a. Create a new MRK in PRIMARY (description tagged as the
          pending migration target).
       b. Read each affected row, decrypt under the OLD key,
          re-encrypt under the NEW MRK, write the row back.
       c. Print the new key ARN.

  4. Update the SSM parameter to point at the new key:

       aws ssm put-parameter --overwrite --type String \\
           --name /${ENV}/${APP}/kms/oauth-key-arn \\
           --value "<new_mrk_arn>" \\
           --region "$PRIMARY"

       (and the same for /kms/db-credential-key-arn if you migrated
       database credentials too)

  5. Roll the affected Lambdas so they pick up the new SSM value.

  6. NOW run ``terraform apply``. Terraform will see the new key as
     pre-existing (because of the alias swap), reconcile the
     ``aws_kms_key.oauth_tokens`` resource against the actual key
     via ``terraform import``, and create the secondary
     ``aws_kms_replica_key``.

  7. Once ``ha-status`` reports clean replication, the old key can
     be deleted via the AWS console (with the standard 7-day window).

For a DAY-0 HA deployment (HA enabled from first apply), no migration
is needed — there are no existing tokens to re-encrypt.

WHAT THIS SCRIPT TOUCHES
------------------------
- ``WORKSPACES`` DynamoDB table rows that carry any ``encrypted_*``
  attribute. The OAuth sweep matches on that attribute's PRESENCE, not a
  per-provider PK allowlist, so every integration that encrypts under the
  OAuth key is covered automatically -- GitHub, Confluence, Jira, Outlook,
  Teams, SharePoint, OneDrive, OneNote, GitLab, Salesforce, ServiceNow,
  Azure DevOps, and the MCP catalog rows -- plus any future one. Each
  ``encrypted_*`` attribute is decrypted under the old key and re-encrypted
  under the new key; other attributes are untouched.
- Database credentials (PK ``DB_CREDENTIAL#*``) live under a SEPARATE key
  and are handled by their own branch (``--skip-db-credentials`` opts out).

- NEW KMS key created in PRIMARY region via boto3 (NOT terraform).
  Terraform will reconcile on the next apply via state import.

The script does NOT touch the secondary region — the new MRK replica
is created later by ``terraform apply`` once the new primary MRK
exists.

EXIT CODES
----------
0  Success / no rows to migrate
1  Configuration or AWS error
2  Some rows failed migration (others may have succeeded)
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Tuple

import boto3
from botocore.exceptions import ClientError

# OAuth-key ciphertext is identified by the PRESENCE of an ``encrypted_*``
# attribute, NOT by a per-provider PK allowlist. A hardcoded allowlist here
# previously skipped GitLab / Salesforce / ServiceNow / Azure DevOps and the
# MCP catalog rows -- all of which encrypt under the OAuth key
# (OAUTH_KMS_KEY_ARN, see workspace-management _encrypt_credentials) -- which
# orphaned exactly those tokens on a re-key and forced a disconnect/reconnect
# (destroying their vectors). DB credentials are the one carve-out: they live
# under a SEPARATE key, so they are excluded from the OAuth sweep and handled
# by their own branch.
DB_CRED_PK_PREFIX = "DB_CREDENTIAL#"


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: {name} env var is required", file=sys.stderr)
        sys.exit(1)
    return val


def _scan_affected_rows(
    table_name: str, region: str, prefixes: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """Scan the workspaces table once and return every item whose PK
    starts with one of the supplied prefixes. Paginated to handle
    enterprise-scale workspace counts (no truncation)."""
    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(table_name)
    affected: List[Dict[str, Any]] = []
    last_key = None
    while True:
        kwargs: Dict[str, Any] = {}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            pk = item.get("PK", "")
            if any(pk.startswith(p) for p in prefixes):
                affected.append(item)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return affected


def _encrypted_attrs(item: Dict[str, Any]) -> List[str]:
    return [k for k in item.keys() if k.startswith("encrypted_")]


def _scan_encrypted_rows(
    table_name: str, region: str, exclude_prefixes: Tuple[str, ...] = ()
) -> List[Dict[str, Any]]:
    """Scan the workspaces table once and return every item that carries at
    least one ``encrypted_*`` attribute, except rows whose PK starts with one
    of ``exclude_prefixes``.

    Self-maintaining by design: any current or future integration that stores
    an ``encrypted_*`` ciphertext under the OAuth key is picked up
    automatically -- there is no per-provider allowlist to keep in sync (the
    old allowlist silently skipped GitLab/Salesforce/ServiceNow/Azure DevOps
    and the MCP catalog rows). Paginated -- no truncation at enterprise
    scale."""
    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(table_name)
    affected: List[Dict[str, Any]] = []
    last_key = None
    while True:
        kwargs: Dict[str, Any] = {}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            pk = item.get("PK", "")
            if any(pk.startswith(p) for p in exclude_prefixes):
                continue
            if _encrypted_attrs(item):
                affected.append(item)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return affected


def _ensure_new_mrk(
    kms_client: Any, env: str, app: str, key_purpose: str
) -> str:
    """Create a new multi-region KMS key. Returns the new KeyId.

    Idempotency: if a key already exists with a tag indicating
    ``ha-kms-migrate`` ownership for ``key_purpose``, reuse it. This
    lets the operator re-run the script after a partial failure."""
    paginator = kms_client.get_paginator("list_keys")
    for page in paginator.paginate():
        for entry in page.get("Keys", []):
            try:
                tags = kms_client.list_resource_tags(KeyId=entry["KeyId"]).get(
                    "Tags", []
                )
            except ClientError:
                continue
            tag_map = {t["TagKey"]: t["TagValue"] for t in tags}
            if (
                tag_map.get("ha-kms-migrate-env") == env
                and tag_map.get("ha-kms-migrate-app") == app
                and tag_map.get("ha-kms-migrate-purpose") == key_purpose
            ):
                desc = kms_client.describe_key(KeyId=entry["KeyId"]).get(
                    "KeyMetadata", {}
                )
                if desc.get("Enabled") and desc.get("MultiRegion"):
                    print(
                        f"  Reusing existing pending-migration MRK: "
                        f"{desc['Arn']}"
                    )
                    return desc["KeyId"]

    resp = kms_client.create_key(
        Description=(
            f"ha-kms-migrate pending replacement for "
            f"{env}-{app}-{key_purpose} (MRK)"
        ),
        MultiRegion=True,
        Tags=[
            {"TagKey": "ha-kms-migrate-env", "TagValue": env},
            {"TagKey": "ha-kms-migrate-app", "TagValue": app},
            {"TagKey": "ha-kms-migrate-purpose", "TagValue": key_purpose},
            {"TagKey": "Purpose", "TagValue": f"{key_purpose}-pending"},
        ],
    )
    return resp["KeyMetadata"]["KeyId"]


def _decrypt(kms: Any, blob_b64: str) -> bytes:
    raw = base64.b64decode(blob_b64)
    return kms.decrypt(CiphertextBlob=raw)["Plaintext"]


def _encrypt(kms: Any, key_id: str, plaintext: bytes) -> str:
    blob = kms.encrypt(KeyId=key_id, Plaintext=plaintext)["CiphertextBlob"]
    return base64.b64encode(blob).decode("utf-8")


def _migrate_rows(
    items: Iterable[Dict[str, Any]],
    table_name: str,
    region: str,
    new_key_id: str,
    dry_run: bool,
) -> Tuple[int, int]:
    """Returns (migrated_count, failed_count)."""
    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(table_name)
    kms = boto3.client("kms", region_name=region)
    migrated = 0
    failed = 0
    for item in items:
        pk = item.get("PK")
        sk = item.get("SK")
        if not pk or not sk:
            continue
        encrypted_attrs = _encrypted_attrs(item)
        if not encrypted_attrs:
            continue
        try:
            new_values: Dict[str, str] = {}
            for attr in encrypted_attrs:
                # Decrypt under the CURRENT key first, to prove the row is
                # recoverable before anything is written. In --plan there is
                # no new key yet, so stop after the decrypt-verify (the old
                # code tried to encrypt with an empty KeyId and failed every
                # row in plan mode).
                plaintext = _decrypt(kms, item[attr])
                if dry_run:
                    continue
                new_values[attr] = _encrypt(kms, new_key_id, plaintext)
            if dry_run:
                print(
                    f"  [DRY-RUN] decrypt-verified {len(encrypted_attrs)} "
                    f"attr(s) on PK={pk} (would re-encrypt on --execute)"
                )
                migrated += 1
                continue
            update_expr = "SET " + ", ".join(
                f"#a{i} = :v{i}" for i, _ in enumerate(encrypted_attrs)
            )
            expr_attr_names = {
                f"#a{i}": attr for i, attr in enumerate(encrypted_attrs)
            }
            expr_attr_values = {
                f":v{i}": new_values[attr]
                for i, attr in enumerate(encrypted_attrs)
            }
            table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_attr_names,
                ExpressionAttributeValues=expr_attr_values,
            )
            migrated += 1
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            print(f"  [FAIL] PK={pk}: {code}", file=sys.stderr)
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] PK={pk}: {exc}", file=sys.stderr)
            failed += 1
    return migrated, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Inventory + dry-run; do not create the new key, do not write rows.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create the new MRK and re-encrypt rows.",
    )
    parser.add_argument(
        "--skip-oauth", action="store_true", help="Skip OAuth-token rows."
    )
    parser.add_argument(
        "--skip-db-credentials",
        action="store_true",
        help="Skip database-credential rows.",
    )
    args = parser.parse_args()

    if not args.plan and not args.execute:
        print(
            "ERROR: pass exactly one of --plan or --execute",
            file=sys.stderr,
        )
        return 1

    env = _env("ENV")
    app = _env("APP")
    primary = _env("PRIMARY")

    table_name = f"{env}-{app}-workspaces"
    print(f"Workspaces table: {table_name} (region={primary})")
    print()

    overall_failed = 0

    if not args.skip_oauth:
        print("== OAuth tokens ==")
        rows = _scan_encrypted_rows(
            table_name, primary, exclude_prefixes=(DB_CRED_PK_PREFIX,)
        )
        print(f"Found {len(rows)} OAuth-token row(s) needing migration.")
        if rows:
            new_key_id = ""
            if args.execute:
                kms_client = boto3.client("kms", region_name=primary)
                new_key_id = _ensure_new_mrk(
                    kms_client, env, app, "oauth-tokens"
                )
                print(f"New MRK KeyId: {new_key_id}")
                # Give IAM/KMS a moment to propagate the new key
                time.sleep(2)
            migrated, failed = _migrate_rows(
                rows, table_name, primary, new_key_id, dry_run=args.plan
            )
            print(f"  -> migrated={migrated} failed={failed}")
            overall_failed += failed
        print()

    if not args.skip_db_credentials:
        print("== Database credentials ==")
        rows = _scan_affected_rows(
            table_name, primary, (DB_CRED_PK_PREFIX,)
        )
        print(f"Found {len(rows)} DB-credential row(s) needing migration.")
        if rows:
            new_key_id = ""
            if args.execute:
                kms_client = boto3.client("kms", region_name=primary)
                new_key_id = _ensure_new_mrk(
                    kms_client, env, app, "db-credentials"
                )
                print(f"New MRK KeyId: {new_key_id}")
                time.sleep(2)
            migrated, failed = _migrate_rows(
                rows, table_name, primary, new_key_id, dry_run=args.plan
            )
            print(f"  -> migrated={migrated} failed={failed}")
            overall_failed += failed
        print()

    if args.plan:
        print(
            "Plan complete. Re-run with --execute to perform the migration."
        )
        return 0
    if overall_failed > 0:
        print(
            f"WARNING: {overall_failed} row(s) failed migration. "
            f"See errors above; re-running is safe (idempotent via "
            f"the ha-kms-migrate-* tags).",
            file=sys.stderr,
        )
        return 2
    print("Migration complete.")
    print()
    print("NEXT STEPS:")
    print("  1. aws ssm put-parameter --overwrite ... (see script docstring)")
    print("  2. Roll the affected Lambdas (force ECS / Lambda redeploy)")
    print("  3. terraform apply (will reconcile the new MRK into state)")
    print("  4. Once stable, delete the old single-region KMS key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
