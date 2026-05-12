#!/bin/bash
set -e

# Build a Lambda layer containing the Databao Context Engine + the
# Postgres / MySQL / MS SQL Server database drivers needed by the
# database-sync Lambda.
#
# Output: lambda/databao-layer/python/ directory with installed packages
# in the Lambda Python layer structure (`python/` is auto-added to
# PYTHONPATH at runtime by Lambda).
#
# Drivers add native components (asyncpg, mssql-python) so we build
# inside the Amazon Linux 2 / Python 3.12 image for ABI compatibility
# with the Lambda execution environment.

LAYER_DIR="lambda/databao-layer"
PYTHON_DIR="$LAYER_DIR/python"

echo "Building Databao Lambda layer..."
echo "Output directory: $PYTHON_DIR"
echo ""

# Clean and create layer directory structure
sudo rm -rf "$PYTHON_DIR" 2>/dev/null || rm -rf "$PYTHON_DIR" 2>/dev/null || true
mkdir -p "$PYTHON_DIR"

# Use the AWS-published Python 3.12 Lambda base image so installed
# wheels resolve to manylinux compatible with the Lambda runtime.
docker run --rm --entrypoint /bin/bash \
  -v "$(pwd)/$LAYER_DIR:/output" \
  -v "$(pwd)/$LAYER_DIR/requirements.txt:/requirements.txt:ro" \
  --platform linux/amd64 \
  public.ecr.aws/lambda/python:3.12 \
  -c '
    set -e

    # Make sure pip is current; some old base images ship behind.
    python -m pip install --upgrade pip setuptools wheel

    # Install into /output/python so the layer maps to /opt/python at
    # runtime (Lambda auto-adds /opt/python to PYTHONPATH).
    pip install --no-cache-dir --target /output/python -r /requirements.txt

    # Trim non-essentials to keep the layer under Lambda'\''s 250 MB
    # unzipped limit. Tests / docs / dist-info are not needed at runtime.
    find /output/python -name "tests" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find /output/python -name "test"  -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find /output/python -name "*.pyc" -delete 2>/dev/null || true
    find /output/python -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

    # Change ownership to host user (UID/GID from mounted volume)
    chown -R $(stat -c "%u:%g" /output) /output/python 2>/dev/null || true

    echo "Layer build complete!"
  '

# Calculate layer size
LAYER_SIZE=$(du -sh "$PYTHON_DIR" | cut -f1)
echo ""
echo "Layer build complete!"
echo "Layer size: $LAYER_SIZE"
echo "Location: $PYTHON_DIR"
echo ""
echo "Lambda layers have a 250 MB unzipped size limit. If this layer"
echo "exceeds that, consider splitting per-engine (postgres / mysql /"
echo "mssql) into separate sub-layers and attaching only the engines"
echo "a given Lambda needs."
echo ""
echo "The layer will be automatically packaged by Terraform."
echo "Run 'terraform plan' to see the changes."
