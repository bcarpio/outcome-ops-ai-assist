"""Unit tests for shared.delete_verify.check_repo_clean (delete-bug 04)."""
from __future__ import annotations

import sys
from pathlib import Path

import boto3
from moto import mock_aws

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

from shared.delete_verify import check_repo_clean  # noqa: E402

WS = "ws1"


def _tables(ddb):
    for name in ("sg", "cm"):
        ddb.create_table(
            TableName=name,
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


def test_check_repo_clean_all_empty():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        _tables(ddb)
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(
            Bucket="kb-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        r = check_repo_clean(
            workspace_id=WS, repo_bare="repo", repo_name="owner/repo",
            dynamodb=ddb, s3_client=s3,
            symbol_graph_table="sg", code_maps_table="cm", kb_bucket="kb-bucket",
        )
        assert r["clean"] is True
        assert r["stores"] == {
            "symbol_graph_facts": 0, "code_maps_pointer": False,
            "kb_owner_objects": 0, "kb_bare_objects": 0,
        }


def test_check_repo_clean_flags_each_leaked_store():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        _tables(ddb)
        ddb.Table("sg").put_item(Item={"PK": f"{WS}#repo", "SK": "calls#1"})
        ddb.Table("cm").put_item(
            Item={"PK": f"SYMBOL_GRAPH#{WS}#repo", "SK": "POINTER"}
        )
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(
            Bucket="kb-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        s3.put_object(Bucket="kb-bucket", Key=f"workspaces/{WS}/github/owner/repo/a.md", Body=b"x")
        s3.put_object(Bucket="kb-bucket", Key=f"workspaces/{WS}/github/repo/arch.txt", Body=b"x")

        r = check_repo_clean(
            workspace_id=WS, repo_bare="repo", repo_name="owner/repo",
            dynamodb=ddb, s3_client=s3,
            symbol_graph_table="sg", code_maps_table="cm", kb_bucket="kb-bucket",
        )
        assert r["clean"] is False
        assert r["stores"]["symbol_graph_facts"] == 1
        assert r["stores"]["code_maps_pointer"] is True
        assert r["stores"]["kb_owner_objects"] == 1
        assert r["stores"]["kb_bare_objects"] == 1


def test_check_repo_clean_skips_unconfigured_stores():
    # No table/bucket names -> those stores reported clean (skipped).
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        s3 = boto3.client("s3", region_name="us-west-2")
        r = check_repo_clean(
            workspace_id=WS, repo_bare="repo", repo_name="owner/repo",
            dynamodb=ddb, s3_client=s3,
        )
        assert r["clean"] is True
