#!/bin/bash
set -e

# Build a Lambda layer containing git, make, terraform, and runtime tools
# Output: lambda/runtime-layer/ directory with /opt structure

TERRAFORM_VERSION="1.9.5"
LAYER_DIR="lambda/runtime-layer"

echo "Building Lambda runtime layer..."
echo "Terraform version: $TERRAFORM_VERSION"
echo "Output directory: $LAYER_DIR"
echo ""

# Clean and create layer directory structure
rm -rf "$LAYER_DIR"
mkdir -p "$LAYER_DIR"/{bin,lib}

# Use a Docker container based on Amazon Linux 2 to build the layer
# This ensures compatibility with Lambda's execution environment
docker run --rm --entrypoint /bin/bash \
  -v "$(pwd)/$LAYER_DIR:/output" \
  public.ecr.aws/lambda/python:3.12 \
  -c '
    # Install runtime tools
    dnf install -y \
      git \
      tar \
      gzip \
      unzip \
      make \
      which \
      findutils \
      procps-ng

    # Download and install Terraform
    curl -fsSL "https://releases.hashicorp.com/terraform/'$TERRAFORM_VERSION'/terraform_'$TERRAFORM_VERSION'_linux_amd64.zip" -o /tmp/terraform.zip
    unzip -q /tmp/terraform.zip -d /tmp

    # Copy binaries to layer structure
    mkdir -p /output/bin /output/lib

    # Copy git and dependencies
    cp /usr/bin/git /output/bin/
    cp /usr/bin/make /output/bin/
    cp /usr/bin/tar /output/bin/
    cp /usr/bin/gzip /output/bin/
    cp /usr/bin/unzip /output/bin/
    cp /usr/bin/which /output/bin/
    cp /usr/bin/find /output/bin/
    cp /tmp/terraform /output/bin/

    # Find and copy shared library dependencies
    echo "Copying shared library dependencies..."
    for binary in /output/bin/*; do
      if [ -f "$binary" ] && file "$binary" | grep -q "ELF"; then
        # Get library dependencies
        ldd "$binary" 2>/dev/null | grep "=> /" | awk "{print \$3}" | while read lib; do
          if [ -f "$lib" ]; then
            # Copy library, preserving directory structure under /lib
            lib_path=$(echo "$lib" | sed "s|^/usr/lib64/|/output/lib/|; s|^/lib64/|/output/lib/|")
            mkdir -p "$(dirname "$lib_path")"
            if [ ! -f "$lib_path" ]; then
              cp "$lib" "$lib_path" 2>/dev/null || true
            fi
          fi
        done
      fi
    done

    # Set permissions
    chmod -R 755 /output/bin
    chmod -R 755 /output/lib

    echo "Layer build complete!"
  '

# Create a README in the layer directory
cat > "$LAYER_DIR/README.md" <<'EOF'
# Runtime Tools Lambda Layer

This layer provides git, make, terraform, and related build tools for the run-tests Lambda.

## Contents

- `/opt/bin/git` - Git version control
- `/opt/bin/make` - GNU Make
- `/opt/bin/terraform` - Terraform CLI
- `/opt/bin/tar`, `/opt/bin/gzip`, `/opt/bin/unzip` - Archive tools
- `/opt/lib/` - Shared library dependencies

## Usage

Add this layer to your Lambda function, and the tools will be available in `/opt/bin/`.
The Lambda execution environment automatically adds `/opt/bin` to the PATH.

## Rebuild

To rebuild this layer:
```bash
./scripts/build-runtime-layer.sh
```

Then run `terraform apply` to update the layer version.
EOF

# Calculate layer size
LAYER_SIZE=$(du -sh "$LAYER_DIR" | cut -f1)
echo ""
echo "Layer build complete!"
echo "Layer size: $LAYER_SIZE"
echo "Location: $LAYER_DIR"
echo ""
echo "The layer will be automatically packaged by Terraform."
echo "Run 'terraform plan' to see the changes."
