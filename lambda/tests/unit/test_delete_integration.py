# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Full-delete integration test across stores + regions (delete-bug 05).

Ties the pieces together end-to-end against moto-backed stores:
``build_repo_delete_messages`` -> the real delete-worker dispatch
(``lambda_handler``) -> ``check_repo_clean``. Asserts a repo's entire
footprint (symbol-graph facts, code-maps pointer, owner-prefixed AND
bare-name KB objects) is gone after the delete, that a co-located
sibling repo whose name is a prefix of the target survives (the
spring-petclinic / spring-petclinic-private collision seen live), and
that the same message set cleans each region independently.

S3 Vectors has no moto backend, so the two vector-delete helpers are
patched to no-ops; every other store is exercised for real via moto.
"""
import importlib.util
import json
import os
import sys
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, LAMBDA_ROOT)

from shared.delete_dispatch import build_repo_delete_messages  # noqa: E402
from shared.delete_verify import check_repo_clean  # noqa: E402

WS = "ws_int"
TARGET = "bcarpio/spring-petclinic-private"
TARGET_BARE = "spring-petclinic-private"
SIBLING = "bcarpio/spring-petclinic"
SIBLING_BARE = "spring-petclinic"
SG_TABLE = "sg-int"
CM_TABLE = "cm-int"
PW_TABLE = "pw-int"
KB = "kb-int-bucket"


def _load_handler():
    worker_dir = os.path.join(os.path.dirname(__file__), "../../delete-worker")
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)
    path = os.path.join(worker_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("delete_worker_handler_int", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_stores(region="us-west-2"):
    ddb = boto3.resource("dynamodb", region_name=region)
    for name in (SG_TABLE, CM_TABLE, PW_TABLE):
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
    s3 = boto3.client("s3", region_name=region)
    kwargs = {"Bucket": KB}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    return ddb, s3


def _seed_repo(ddb, s3, owner_repo, bare):
    sg = ddb.Table(SG_TABLE)
    for i in range(5):
        sg.put_item(Item={"PK": f"{WS}#{bare}", "SK": f"calls#{i}", "v": i})
    cm = ddb.Table(CM_TABLE)
    cm.put_item(
        Item={"PK": f"SYMBOL_GRAPH#{WS}#{bare}", "SK": "POINTER", "graph_sha": "deadbeef"}
    )
    # Ingest-side state rows (the rows the original cascade leaked).
    cm.put_item(Item={"PK": f"repo#{bare}", "SK": "state#last-processed", "sha": "deadbeef"})
    cm.put_item(Item={"PK": f"repo#{bare}", "SK": "state#ingest-docs", "sha": "deadbeef"})
    ddb.Table(PW_TABLE).put_item(
        Item={
            "PK": f"WS#{WS}#GITHUB_REPO#{owner_repo}",
            "SK": "PENDING#code_map_dispatch#deadbeef",
            "expected": 1,
        }
    )
    s3.put_object(
        Bucket=KB, Key=f"workspaces/{WS}/github/{owner_repo}/src/Main.java", Body=b"x"
    )
    s3.put_object(
        Bucket=KB,
        Key=f"workspaces/{WS}/github/{bare}/architectural-summary.txt",
        Body=b"x",
    )


def _wire(handler, ddb, s3):
    handler.dynamodb = ddb
    handler.s3_client = s3
    handler.SYMBOL_GRAPH_TABLE = SG_TABLE
    handler.CODE_MAPS_TABLE = CM_TABLE
    handler.PENDING_WORK_TABLE = PW_TABLE
    handler.KB_BUCKET = KB


def _run_delete(handler, repo_name):
    """Feed every build_repo_delete_messages message through the real
    SQS dispatch, exactly as the delete-worker would receive them."""
    messages = build_repo_delete_messages(WS, repo_name)
    event = {
        "Records": [
            {"messageId": f"m{i}", "body": json.dumps(m)}
            for i, m in enumerate(messages)
        ]
    }
    return handler.lambda_handler(event, None), messages


def _clean(ddb, s3, owner_repo, bare):
    return check_repo_clean(
        workspace_id=WS,
        repo_bare=bare,
        repo_name=owner_repo,
        dynamodb=ddb,
        s3_client=s3,
        symbol_graph_table=SG_TABLE,
        code_maps_table=CM_TABLE,
        kb_bucket=KB,
        pending_work_table=PW_TABLE,
    )


@pytest.mark.parametrize("region", ["us-west-2", "us-east-2"])
def test_full_delete_clears_all_stores_in_region(region):
    """The delete-worker, fed the full message set for a repo, leaves
    every store clean in its own region and reports no batch failures."""
    handler = _load_handler()
    with mock_aws():
        ddb, s3 = _make_stores(region)
        _seed_repo(ddb, s3, TARGET, TARGET_BARE)
        _wire(handler, ddb, s3)

        assert _clean(ddb, s3, TARGET, TARGET_BARE)["clean"] is False  # seeded

        with patch.object(handler, "delete_vectors_for_s3_key", return_value=0), \
                patch.object(handler, "delete_vectors_by_key_prefix", return_value=0):
            result, messages = _run_delete(handler, TARGET)

        # owner delete_prefix + bare delete_prefix + delete_symbol_graph
        assert len(messages) == 3
        assert result["batchItemFailures"] == []

        report = _clean(ddb, s3, TARGET, TARGET_BARE)
        assert report["clean"] is True
        assert report["stores"] == {
            "symbol_graph_facts": 0,
            "code_maps_pointer": False,
            "code_maps_state_rows": 0,
            "kb_owner_objects": 0,
            "kb_bare_objects": 0,
            "pending_work_rows": 0,
        }


def test_full_delete_spares_prefix_colliding_sibling():
    """Deleting spring-petclinic-private must NOT touch spring-petclinic
    (its name is a prefix of the target's). Regression guard for the
    live collision case."""
    handler = _load_handler()
    with mock_aws():
        ddb, s3 = _make_stores("us-west-2")
        _seed_repo(ddb, s3, TARGET, TARGET_BARE)
        _seed_repo(ddb, s3, SIBLING, SIBLING_BARE)
        _wire(handler, ddb, s3)

        with patch.object(handler, "delete_vectors_for_s3_key", return_value=0), \
                patch.object(handler, "delete_vectors_by_key_prefix", return_value=0):
            _run_delete(handler, TARGET)

        assert _clean(ddb, s3, TARGET, TARGET_BARE)["clean"] is True

        sibling = _clean(ddb, s3, SIBLING, SIBLING_BARE)
        assert sibling["clean"] is False
        assert sibling["stores"]["symbol_graph_facts"] == 5
        assert sibling["stores"]["code_maps_pointer"] is True
        assert sibling["stores"]["code_maps_state_rows"] == 2
        assert sibling["stores"]["kb_owner_objects"] == 1
        assert sibling["stores"]["kb_bare_objects"] == 1
        assert sibling["stores"]["pending_work_rows"] == 1
