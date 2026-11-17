#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Clear all items from DynamoDB code-maps table."""
import boto3

TABLE = "prd-outcome-ops-ai-assist-code-maps"
dynamodb = boto3.client("dynamodb")

print(f"Clearing table: {TABLE}")

# Paginate through all items
paginator = dynamodb.get_paginator("scan")
deleted = 0

for page in paginator.paginate(TableName=TABLE, ProjectionExpression="PK, SK"):
    items = page.get("Items", [])
    for item in items:
        dynamodb.delete_item(TableName=TABLE, Key=item)
        deleted += 1
        if deleted % 50 == 0:
            print(f"Deleted {deleted} items...")

print(f"Done - deleted {deleted} items total")
