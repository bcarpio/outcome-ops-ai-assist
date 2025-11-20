#!/bin/bash
set -e

# Build a Lambda layer containing just terraform
# Output: lambda/terraform-layer/ directory with /opt structure

TERRAFORM_VERSION="1.9.5"
LAYER_DIR="lambda/terraform-layer"

echo "Building Terraform Lambda layer..."
echo "Terraform version: $TERRAFORM_VERSION"
echo "Output directory: $LAYER_DIR"
echo ""

# Clean and create layer directory structure
sudo rm -rf "$LAYER_DIR"/{bin,lib} 2>/dev/null || rm -rf "$LAYER_DIR"/{bin,lib} 2>/dev/null || true
mkdir -p "$LAYER_DIR"/bin

# Use a Docker container based on Amazon Linux 2 to build the layer
docker run --rm --entrypoint /bin/bash \
  -v "$(pwd)/$LAYER_DIR:/output" \
  public.ecr.aws/lambda/python:3.12 \
  -c '
    # Download and install Terraform
    curl -fsSL "https://releases.hashicorp.com/terraform/'$TERRAFORM_VERSION'/terraform_'$TERRAFORM_VERSION'_linux_amd64.zip" -o /tmp/terraform.zip
    unzip -q /tmp/terraform.zip -d /tmp

    # Copy terraform to layer structure
    mkdir -p /output/bin
    cp /tmp/terraform /output/bin/

    # Set permissions and fix ownership to match host user
    chmod 755 /output/bin/terraform
    chown -R $(stat -c "%u:%g" /output) /output/bin 2>/dev/null || true

    echo "Terraform layer build complete!"
  '

# Create a README in the layer directory
cat > "$LAYER_DIR/README.md" <<'EOF'
# Terraform Lambda Layer

This layer provides the Terraform CLI for Lambda functions that need to format or validate Terraform files.

**Note:** Layer binaries are not committed to git. You must build the layer before running `terraform apply`.

## Contents

- `/opt/bin/terraform` - Terraform CLI v1.9.5

## Usage

Add this layer to your Lambda function, and terraform will be available in `/opt/bin/`.
The Lambda execution environment automatically adds `/opt/bin` to the PATH.

## Build Layer

```bash
./scripts/build-terraform-layer.sh
```

Then run `terraform apply` to update the layer version.
EOF

# Calculate layer size
LAYER_SIZE=$(du -sh "$LAYER_DIR" | cut -f1)
echo ""
echo "Terraform layer build complete!"
echo "Layer size: $LAYER_SIZE"
echo "Location: $LAYER_DIR"
echo ""
echo "The layer will be automatically packaged by Terraform."
