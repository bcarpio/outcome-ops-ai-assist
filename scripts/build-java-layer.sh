#!/bin/bash
set -e

# Build a Lambda layer containing Java 21 (Amazon Corretto)
# Output: lambda/java-layer/ directory with /opt structure

LAYER_DIR="lambda/java-layer"

echo "Building Lambda Java layer..."
echo "Output directory: $LAYER_DIR"
echo ""

# Clean and create layer directory structure
# Note: We only include the java/ directory, not bin/ with symlinks
# Symlinks to /opt paths cannot be resolved locally during Terraform packaging
sudo rm -rf "$LAYER_DIR"/{java,bin} 2>/dev/null || rm -rf "$LAYER_DIR"/{java,bin} 2>/dev/null || true
mkdir -p "$LAYER_DIR"/java

# Use a Docker container based on Amazon Linux 2 to build the layer
docker run --rm --entrypoint /bin/bash \
  -v "$(pwd)/$LAYER_DIR:/output" \
  public.ecr.aws/lambda/python:3.12 \
  -c '
    # Install Java 21 (Amazon Corretto headless)
    dnf install -y java-21-amazon-corretto-headless

    # Create output directories
    mkdir -p /output/java

    # Copy Java 21 (Amazon Corretto)
    # Use -L to follow symlinks and copy actual files (e.g., cacerts)
    echo "Copying Java 21..."
    JAVA_HOME=$(dirname $(dirname $(readlink -f /usr/bin/java)))
    cp -rL "$JAVA_HOME"/* /output/java/

    # Set permissions
    chmod -R 755 /output/java

    # Change ownership to host user
    chown -R $(stat -c "%u:%g" /output) /output/java 2>/dev/null || true

    echo "Java layer build complete!"
  '

# Create a README in the layer directory
cat > "$LAYER_DIR/README.md" <<'EOF'
# Java Lambda Layer

This layer provides Java 21 (Amazon Corretto headless) for Lambda functions.

**Note:** Layer contents (`java/`) are not committed to git. You must build the layer locally before running `terraform apply`.

## Contents

- `/opt/java/` - Java 21 runtime (Amazon Corretto headless)
- `/opt/java/bin/java` - Java binary

## Usage

Add this layer to your Lambda function for Java support.

Set the following environment variables in your Lambda:
- `JAVA_HOME=/opt/java`
- Add `/opt/java/bin` to PATH

## Build Layer (Required Before Terraform Apply)

```bash
# Build all layers
./scripts/build-all-layers.sh

# Or build individually
./scripts/build-java-layer.sh

# Deploy with Terraform
terraform plan && terraform apply
```

The build script uses Docker to compile binaries compatible with Amazon Linux 2 (Lambda runtime environment).
EOF

# Calculate layer size
LAYER_SIZE=$(du -sh "$LAYER_DIR" | cut -f1)
echo ""
echo "Java layer build complete!"
echo "Layer size: $LAYER_SIZE"
echo "Location: $LAYER_DIR"
echo ""
echo "The layer will be automatically packaged by Terraform."
