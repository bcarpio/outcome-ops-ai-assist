#!/bin/bash
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# Clear all items from DynamoDB code-maps table

TABLE="prd-outcome-ops-ai-assist-code-maps"
echo "Clearing table: $TABLE"

# Get all items and delete them
aws dynamodb scan --table-name "$TABLE" --output json | jq -c '.Items[]' | while read -r item; do
  pk=$(echo "$item" | jq -r '.PK')
  sk=$(echo "$item" | jq -r '.SK')
  aws dynamodb delete-item --table-name "$TABLE" --key "$item" 2>/dev/null
  echo "Deleted: $pk / $sk"
done

echo "Done"
