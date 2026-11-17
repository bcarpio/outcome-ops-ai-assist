#!/bin/bash
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
set -e

# Build all Lambda layers
# This script builds the runtime layer for generate-code Lambda
#
# NOTE: The run-tests Lambda uses a container image instead of layers.
# To build the run-tests container, use: ./scripts/build-run-tests-image.sh --env <dev|prd>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "Building Lambda layers"
echo "========================================"
echo ""

# Build runtime layer (git, make, tar, etc.)
echo "--- Building Runtime Layer ---"
"$SCRIPT_DIR/build-runtime-layer.sh"
echo ""

# Build platform-CA trust layer (certifi + customer CA bundles via SSM).
# Required by lambda-platform-ca-layer.tf — terraform plan fails without
# build/platform-ca-layer/layer.zip.
echo "--- Building Platform-CA Layer ---"
"$SCRIPT_DIR/build-platform-ca-layer.sh"
echo ""

# Build terraform-tools layer (terraform CLI binary, used by
# generate-code Lambda for fmt/validate). terraform/lambda-layers.tf
# also carries a null_resource that rebuilds this on apply if the
# build script's md5 changes -- building here gives the operator
# faster failure feedback (docker / network failures surface at
# pre-deploy time instead of during terraform apply) AND the null_resource
# sees the layer already built and skips its rebuild on apply.
# Idempotent either way.
echo "--- Building Terraform-Tools Layer ---"
"$SCRIPT_DIR/build-terraform-layer.sh"
echo ""

echo "========================================"
echo "All layers built successfully!"
echo "========================================"
echo ""
echo "Layer sizes:"
du -sh lambda/runtime-layer 2>/dev/null || true
du -sh build/platform-ca-layer 2>/dev/null || true
du -sh lambda/terraform-layer 2>/dev/null || true
echo ""
echo "(Layers only. Container images — run-tests, UI, MCP, database — build"
echo " separately via 'make build-all-images'. 'make pre-deploy' runs both back to"
echo " back, so nothing above needs a manual follow-up step.)"
