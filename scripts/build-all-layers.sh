#!/bin/bash
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

echo "========================================"
echo "All layers built successfully!"
echo "========================================"
echo ""
echo "Layer sizes:"
du -sh lambda/runtime-layer 2>/dev/null || true
echo ""
echo "Run 'terraform plan' to see the changes."
echo ""
echo "NOTE: For run-tests Lambda, build the container image:"
echo "  ./scripts/build-run-tests-image.sh --env <dev|prd>"
