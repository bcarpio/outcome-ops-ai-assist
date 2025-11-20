#!/bin/bash
set -e

# Build a Lambda layer containing git, make, terraform, and runtime tools
# Output: lambda/runtime-layer/ directory with /opt structure

LAYER_DIR="lambda/runtime-layer"

echo "Building Lambda runtime layer..."
echo "Output directory: $LAYER_DIR"
echo ""

# Clean and create layer directory structure
# Use sudo to remove any root-owned files from previous Docker builds
sudo rm -rf "$LAYER_DIR"/{bin,lib,libexec} 2>/dev/null || rm -rf "$LAYER_DIR"/{bin,lib,libexec} 2>/dev/null || true
mkdir -p "$LAYER_DIR"/{bin,lib,libexec/git-core}

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
      procps-ng \
      file

    # Copy binaries to layer structure
    mkdir -p /output/bin /output/lib /output/libexec/git-core

    # Copy git and dependencies
    cp /usr/bin/git /output/bin/
    cp /usr/bin/make /output/bin/
    cp /usr/bin/tar /output/bin/
    cp /usr/bin/gzip /output/bin/
    cp /usr/bin/unzip /output/bin/
    cp /usr/bin/which /output/bin/
    cp /usr/bin/find /output/bin/

    # Copy only essential git helper programs (to keep layer size small)
    echo "Copying essential git helper programs..."
    # Only copy the remote helpers needed for cloning (not all 180+ files)
    for helper in git-remote-https git-remote-http git-remote-ftp git-remote-ftps git-sh-setup; do
      if [ -f "/usr/libexec/git-core/$helper" ]; then
        cp "/usr/libexec/git-core/$helper" /output/libexec/git-core/
      fi
    done

    # Find and copy shared library dependencies
    echo "Copying shared library dependencies..."
    for binary in /output/bin/* /output/libexec/git-core/*; do
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

    # Set permissions and fix ownership to match host user
    chmod -R 755 /output/bin
    chmod -R 755 /output/lib
    chmod -R 755 /output/libexec

    # Change ownership to host user (UID/GID from mounted volume)
    chown -R $(stat -c "%u:%g" /output) /output/bin /output/lib /output/libexec 2>/dev/null || true

    echo "Layer build complete!"
  '

# Create a README in the layer directory
cat > "$LAYER_DIR/README.md" <<'EOF'
# Runtime Tools Lambda Layer

This layer provides git, make, terraform, and related build tools for the run-tests Lambda.

**Note:** Layer binaries (`bin/` and `lib/`) are not committed to git. You must build the layer locally before running `terraform apply`.

## Contents

- `/opt/bin/git` - Git version control
- `/opt/bin/make` - GNU Make
- `/opt/bin/tar`, `/opt/bin/gzip`, `/opt/bin/unzip` - Archive tools
- `/opt/lib/` - Shared library dependencies
- `/opt/libexec/git-core/` - Git helper programs (git-remote-https, etc.)

## Usage

Add this layer to your Lambda function, and the tools will be available in `/opt/bin/`.
The Lambda execution environment automatically adds `/opt/bin` to the PATH.

## Build Layer (Required Before Terraform Apply)

The layer binaries are built locally and not checked into git:

```bash
# Build the layer (creates bin/ and lib/ directories)
./scripts/build-runtime-layer.sh

# Deploy with Terraform
terraform apply
```

The build script uses Docker to compile binaries compatible with Amazon Linux 2 (Lambda runtime environment).
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
